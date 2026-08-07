"""Git worktree provisioning for per-terminal isolation (issue #100, Phase 1).

When a supervisor spawns multiple workers via ``handoff``/``assign``, they
share the same git branch and working directory by default -- the exact
"merge conflicts, overwritten files, race conditions" gap issue #100 names.
Passing ``use_worktree=True`` on a spawn gives that one worker an isolated
``git worktree`` checkout on its own branch instead.

Scoped strictly to the maintainer's own suggested Phase 1 (this module +
``use_worktree`` on ``handoff``/``assign``) -- the ``--enable-worktrees``
global launch flag and the ``cao worktrees clean`` CLI command are Phase 2/3,
intentionally not built here to keep this PR reviewable-sized.

No new CAO-side persistence: a worktree's path and branch are both derived
deterministically from the terminal_id CAO already generates for every
terminal (``generate_terminal_id()``, unique and server-controlled, never
user-supplied), so ``create_terminal``/``delete_terminal`` can locate a
worktree at teardown time from the terminal_id alone -- git's own
``.git/worktrees`` bookkeeping is the single source of truth, matching how
this project already treats git as authoritative elsewhere.
"""

import logging
import os
import subprocess

logger = logging.getLogger(__name__)

# Kept out of the repo's own working tree root and namespaced under one
# directory so a single `git worktree list`/`rm -rf` scopes cleanly to
# everything CAO has ever provisioned here.
WORKTREE_SUBDIR = ".cao/worktrees"
BRANCH_PREFIX = "cao/"

# Local-only git operations (add/remove/list); generous but bounded so a
# hung git process cannot hang terminal creation/deletion indefinitely.
_GIT_TIMEOUT_SECONDS = 30

_WORKTREE_PATH_MARKER = f"{os.sep}{WORKTREE_SUBDIR}{os.sep}"


class WorktreeError(Exception):
    """A git-worktree operation failed (repo resolution, add, or list)."""


def _run_git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    """Run ``git <args>`` in ``cwd``, never raising -- a nonexistent ``cwd``
    (``OSError``/``FileNotFoundError``) or a hung git process
    (``subprocess.TimeoutExpired``) is reported the SAME way a nonzero exit
    code is: a synthetic failed ``CompletedProcess`` with the exception text
    in ``stderr``. Every caller below already branches on ``returncode != 0``
    for a normal git failure -- routing infra failures through that same
    path (instead of letting them escape as a raw, uncaught exception) is
    what makes ``remove_worktree``'s "never raises" contract, and
    ``find_repo_root``/``create_worktree``/``list_worktrees``'s own
    ``WorktreeError`` contract, both actually hold rather than being
    docstring claims that a missing/unreadable directory quietly breaks."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=1, stdout="", stderr=str(e)
        )


def find_repo_root(start_path: str) -> str:
    """The git repository root containing ``start_path``.

    ``git worktree add`` must run from inside a real repo's own working
    tree; ``start_path`` may be any subdirectory of it (a supervisor's own
    working directory is not necessarily the repo root).

    Raises:
        WorktreeError: ``start_path`` is not inside a git repository.
    """
    result = _run_git(["rev-parse", "--show-toplevel"], cwd=start_path)
    if result.returncode != 0:
        raise WorktreeError(
            f"{start_path!r} is not inside a git repository -- use_worktree requires "
            f"a real git repo ('git rev-parse --show-toplevel' failed: {result.stderr.strip()})"
        )
    stdout: str = result.stdout
    return stdout.strip()


def worktree_path_for(repo_root: str, terminal_id: str) -> str:
    return os.path.join(repo_root, WORKTREE_SUBDIR, terminal_id)


def branch_for(terminal_id: str) -> str:
    return f"{BRANCH_PREFIX}{terminal_id}"


def create_worktree(repo_root: str, terminal_id: str) -> str:
    """``git worktree add`` a fresh checkout on its own branch, based on the
    repo's current HEAD. Returns the new worktree's absolute path.

    ``terminal_id`` is server-generated (never user-supplied), so the
    derived path/branch need no additional sanitization beyond what CAO's
    own terminal-id generator already guarantees (a fixed-alphabet,
    fixed-length id -- see ``generate_terminal_id``).

    Raises:
        WorktreeError: ``git worktree add`` failed (e.g. a stale directory
            or branch from an earlier crashed attempt under the same id --
            unreachable in practice since terminal_id is always fresh, but
            surfaced as a clear error rather than a confusing git failure).
    """
    path = worktree_path_for(repo_root, terminal_id)
    branch = branch_for(terminal_id)
    result = _run_git(["worktree", "add", "-b", branch, path], cwd=repo_root)
    if result.returncode != 0:
        raise WorktreeError(
            f"'git worktree add' failed for terminal {terminal_id}: {result.stderr.strip()}"
        )
    _ensure_worktree_subdir_gitignored(repo_root)
    return path


def _ensure_worktree_subdir_gitignored(repo_root: str) -> None:
    """Best-effort: write ``<repo_root>/.cao/worktrees/.gitignore`` (``*``) the
    first time a worktree is created there, so the main checkout's own
    ``git status``/``git add -A`` never sees -- and never stages as an
    embedded-repo gitlink -- the worktrees CAO provisions inside it. The
    target repo is arbitrary user code; we cannot assume its own
    ``.gitignore`` already excludes ``.cao/worktrees`` (this repo's own
    ``.gitignore`` only excludes its unrelated ``.worktrees``), so CAO must
    write its own ignore file rather than rely on one being present.

    Never raises: a failure here (e.g. read-only filesystem) must not fail
    the worktree creation that already succeeded.
    """
    gitignore_path = os.path.join(repo_root, WORKTREE_SUBDIR, ".gitignore")
    try:
        if not os.path.exists(gitignore_path):
            with open(gitignore_path, "w", encoding="utf-8") as f:
                f.write("*\n")
    except OSError as e:
        logger.warning("worktree setup: failed to write %s: %s", gitignore_path, e)


def remove_worktree(repo_root: str, terminal_id: str) -> None:
    """Best-effort teardown: ``git worktree remove --force`` (agents commonly
    leave modified/untracked files behind, so a plain ``remove`` would
    refuse) followed by a SAFE branch delete (``git branch -d``, not
    ``-D``).

    Deliberately never force-deletes the branch: a worker that committed
    its results to ``cao/<terminal_id>`` before completing (exactly what
    agents are usually instructed to do) must not have that history
    destroyed just because Phase 1 has no merge-back story yet. ``-d``
    only succeeds when the branch has no commits that would be lost (i.e.
    it is unchanged, or already merged); a worker that committed real work
    fails the safe delete and the branch is left behind -- a leak for
    Phase 3's ``cao worktrees clean`` to sweep up later, not silent data
    loss. Only the (uncommitted/untracked) working-tree contents of the
    worktree itself are ever force-discarded.

    Never raises -- called from terminal-teardown paths (``delete_terminal``,
    and the failure-cleanup path in ``create_terminal``) that must not fail
    the terminal's own deletion/rollback over a worktree cleanup issue.
    Failures are logged, not swallowed silently.
    """
    path = worktree_path_for(repo_root, terminal_id)
    branch = branch_for(terminal_id)
    result = _run_git(["worktree", "remove", "--force", path], cwd=repo_root)
    if result.returncode != 0:
        logger.warning(
            "worktree cleanup: 'git worktree remove --force %s' failed: %s",
            path,
            result.stderr.strip(),
        )
    result = _run_git(["branch", "-d", branch], cwd=repo_root)
    if result.returncode != 0:
        logger.warning(
            "worktree cleanup: 'git branch -d %s' failed (left in place -- likely has "
            "unmerged commits; merge/push the work, then delete it manually): %s",
            branch,
            result.stderr.strip(),
        )


def list_worktrees(repo_root: str) -> list[dict[str, str | bool]]:
    """Parsed ``git worktree list --porcelain`` for ``repo_root`` -- the AC's
    'list' operation. No CAO-side persistence to query: git's own
    bookkeeping is authoritative, so this always reflects reality even if a
    worktree was added/removed outside CAO.

    Raises:
        WorktreeError: ``repo_root`` is not a git repository, or the list
            command otherwise failed.
    """
    result = _run_git(["worktree", "list", "--porcelain"], cwd=repo_root)
    if result.returncode != 0:
        raise WorktreeError(f"'git worktree list' failed: {result.stderr.strip()}")
    worktrees: list[dict[str, str | bool]] = []
    current: dict[str, str | bool] = {}
    for line in result.stdout.splitlines():
        if not line:
            if current:
                worktrees.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value if value else True
    if current:
        worktrees.append(current)
    return worktrees


def parse_worktree_path(path: object) -> tuple[str, str] | None:
    """If ``path`` looks like a CAO-managed worktree, or a subdirectory of
    one (``<repo_root>/.cao/worktrees/<terminal_id>[/...]``), return
    ``(repo_root, terminal_id)``; otherwise ``None``.

    Used at teardown time to recognize a worktree-provisioned terminal from
    its own live pane working directory alone -- no separate CAO-side
    tracking of "which terminals are worktree-backed" is needed, since the
    path shape itself is the marker.

    Two deliberate choices beyond a naive split:

    - Subdirectories of the worktree are accepted, not just the worktree
      root exactly. tmux reports the pane's CURRENT directory
      (``pane_current_path``): a worker that ``cd``s into a subdirectory
      (very likely) would otherwise no longer be recognized as
      worktree-backed at teardown, leaking the worktree/branch.
    - ``rfind`` (last occurrence), not ``find`` (first), so a
      worktree-backed supervisor spawning a worktree-backed worker --
      nesting ``<repo_root>/.cao/worktrees/A/.cao/worktrees/B`` -- resolves
      to B's own ``(repo_root, terminal_id)`` (repo_root = A's worktree
      root, terminal_id = B) instead of failing to parse and leaking B.

    Accepts ``object`` (not just ``str | None``) and returns ``None`` for
    anything that isn't a real string, deliberately: the caller
    (``delete_terminal``) reads this from a backend call whose real contract
    is ``str | None``, but its actual value at any given call site can be
    something else entirely under test doubles/mocks -- this must degrade to
    "not a worktree" rather than raise, since it feeds a real ``git``
    subprocess call two steps downstream.
    """
    if not isinstance(path, str) or not path:
        return None
    idx = path.rfind(_WORKTREE_PATH_MARKER)
    if idx == -1:
        return None
    repo_root = path[:idx]
    remainder = path[idx + len(_WORKTREE_PATH_MARKER) :]
    terminal_id = remainder.split(os.sep, 1)[0]
    if not repo_root or not terminal_id:
        return None
    return repo_root, terminal_id
