"""Derive a run's repository baseline (issue #583 Bolt 2, unit ``manifest-freeze``).

The one thing about a script-tier plan that the workflow source hash CANNOT capture. Everything else the
manifest records about how a run will execute is in the script itself; the surrounding repository is not.
Two runs of an identical script against different commits are genuinely different plans, and a resume onto a
different commit is exactly the drift FR-12 wants diagnosable.

TOTAL BY CONSTRUCTION: nothing here raises, and nothing blocks indefinitely. A directory that is
determinately not in a repository is a representable, approvable state. Failures that leave repository
identity indeterminate — ``git`` missing, an unreadable directory, or a hung process — remain unavailable
so the approval path fails closed.

COMMIT AND WORKTREE STATE ONLY, AND THE OMISSIONS ARE DELIBERATE. No branch name, and above all NO PATH: a path
is environment-specific, so including it would make the ``plan_id`` derived from this baseline differ between
two machines running an identical plan — a spurious re-approval on every machine change, which is the same
false positive that sorting dict keys in ``plan_identifier`` exists to prevent. Normalise away what does not
affect execution.

ON THE DUPLICATION WITH ``worktree_service._run_git``. That function already has this module's exact
never-raises contract, and it is private to a service whose purpose (worktree management) is unrelated to
freezing a manifest. Promoting it would have been the THIRD Bolt-1-file promotion in a single Construction
pass, so a second wrapper is accepted here for testability and layering rather than because the sibling was
overlooked. **IF A THIRD CALLER EVER NEEDS A NEVER-RAISES GIT WRAPPER, CONSOLIDATE INTO A SHARED
``utils/git.py`` RATHER THAN ADDING A FOURTH.** Two is a bounded, documented cost; three is a pattern nobody
decided on.
"""

import hashlib
import logging
import os
import stat
import subprocess
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Bounded so a hung git process cannot delay run start. Generous relative to the work — two local
# invocations that read refs — because the point is to fail eventually, not quickly.
_GIT_TIMEOUT_SECONDS = 10
_HASH_CHUNK_BYTES = 64 * 1024
_UNTRACKED_HASH_BUDGET_BYTES = 64 * 1024 * 1024  # 64 MiB

_TRACKED_DIFF_ARGS = [
    "-c",
    "core.abbrev=40",
    "-c",
    "core.quotePath=true",
    "-c",
    "core.autocrlf=false",
    "-c",
    "color.ui=false",
    "-c",
    "color.diff=false",
    "-c",
    "diff.renames=false",
    "-c",
    "diff.algorithm=myers",
    "-c",
    "diff.indentHeuristic=false",
    "-c",
    "diff.context=3",
    "-c",
    "diff.interHunkContext=0",
    "-c",
    "diff.submodule=short",
    "diff",
    "--binary",
    "--no-ext-diff",
    "--no-textconv",
    "--no-color",
    "--no-prefix",
    "--no-renames",
    "--diff-algorithm=myers",
    "--no-indent-heuristic",
    "--unified=3",
    "--inter-hunk-context=0",
    "--ignore-submodules=none",
    "HEAD",
    "--",
]


def _run_git(
    args: list[str], cwd: str, environment: Dict[str, str], *, text: bool = True
) -> Optional[subprocess.CompletedProcess[Any]]:
    """Run ``git <args>`` in ``cwd``, returning ``None`` when it could not run or did not succeed.

    LIST-ARGV, NEVER A SHELL STRING, and no value is interpolated into the arguments — every element is an
    authored literal and the only variable is the working directory. Command injection is closed by
    construction rather than by escaping.

    An ``OSError`` (``git`` absent, ``cwd`` unreadable) and a ``TimeoutExpired`` (hung process) are reported
    the SAME way a nonzero exit code is: ``None``. The caller has one branch to write, which is what makes
    this module's "never raises" contract hold rather than being a docstring claim that an exotic
    environment quietly breaks.
    """
    try:
        completed = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *args],
            cwd=cwd,
            capture_output=True,
            text=text,
            timeout=_GIT_TIMEOUT_SECONDS,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        # Debug, not warning: a workspace outside git is entirely ordinary and this is not a fault.
        logger.debug("git_baseline: %s failed (baseline recorded absent): %s", args, e)
        return None
    if completed.returncode != 0:
        logger.debug(
            "git_baseline: %s exited %d (baseline recorded absent)", args, completed.returncode
        )
        return None
    return completed


def _git_environment() -> Dict[str, str]:
    """Return a deterministic Git environment with inherited redirects removed."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
            "LANG": "C",
        }
    )
    return environment


def _worktree_state(cwd: str, environment: Dict[str, str]) -> Dict[str, str]:
    """Capture tracked and untracked changes without recording an absolute worktree path.

    Hash at most ``_UNTRACKED_HASH_BUDGET_BYTES`` of untracked regular-file content. Exhausting
    that aggregate limit, or observing a path race, records the state as unavailable rather than
    producing an identity from a partial snapshot.
    """
    tracked = _run_git(_TRACKED_DIFF_ARGS, cwd, environment, text=False)
    untracked = _run_git(
        ["ls-files", "--others", "--exclude-standard", "-z"],
        cwd,
        environment,
        text=False,
    )
    if tracked is None or untracked is None:
        return {"status": "unavailable"}

    tracked_bytes = tracked.stdout
    untracked_paths = [path for path in untracked.stdout.split(b"\0") if path]
    if not tracked_bytes and not untracked_paths:
        return {"status": "clean"}

    digest = hashlib.sha256()
    digest.update(len(tracked_bytes).to_bytes(8, "big"))
    digest.update(tracked_bytes)
    hashed_untracked_bytes = 0
    try:
        for relative_path in sorted(untracked_paths):
            # ``git ls-files`` yields repository-relative paths. Refuse an unexpected path rather than
            # hashing outside the worktree if a repository changes beneath this snapshot operation.
            if relative_path.startswith(b"/") or b".." in relative_path.split(b"/"):
                logger.debug(
                    "git_baseline: invalid untracked path (baseline recorded absent): %r",
                    relative_path,
                )
                return {"status": "unavailable"}

            contents_path = os.fsencode(cwd)
            path_components = relative_path.split(b"/")
            for index, component in enumerate(path_components):
                contents_path = os.path.join(contents_path, component)
                if stat.S_ISLNK(os.lstat(contents_path).st_mode):
                    if index != len(path_components) - 1:
                        logger.debug(
                            "git_baseline: untracked path has a symlinked parent "
                            "(baseline recorded absent): %r",
                            relative_path,
                        )
                        return {"status": "unavailable"}
                    break

            digest.update(len(relative_path).to_bytes(8, "big"))
            digest.update(relative_path)
            entry_stat = os.lstat(contents_path)
            entry_mode = entry_stat.st_mode
            if stat.S_ISLNK(entry_mode):
                link_payload = os.readlink(contents_path)
                digest.update(b"S")
                digest.update(len(link_payload).to_bytes(8, "big"))
                digest.update(link_payload)
                continue
            if not stat.S_ISREG(entry_mode):
                logger.debug(
                    "git_baseline: invalid untracked entry (baseline recorded absent): %r",
                    relative_path,
                )
                return {"status": "unavailable"}

            digest.update(b"F")
            nonblocking_flag = getattr(os, "O_NONBLOCK", None)
            nofollow_flag = getattr(os, "O_NOFOLLOW", None)
            if nonblocking_flag is None or nofollow_flag is None:
                logger.debug(
                    "git_baseline: required descriptor flags are unavailable "
                    "(baseline recorded absent): %r",
                    relative_path,
                )
                return {"status": "unavailable"}

            descriptor = -1
            try:
                descriptor = os.open(contents_path, os.O_RDONLY | nonblocking_flag | nofollow_flag)
                opened_stat = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(opened_stat.st_mode)
                    or opened_stat.st_dev != entry_stat.st_dev
                    or opened_stat.st_ino != entry_stat.st_ino
                ):
                    logger.debug(
                        "git_baseline: untracked entry changed before descriptor open "
                        "(baseline recorded absent): %r",
                        relative_path,
                    )
                    return {"status": "unavailable"}

                content_length = opened_stat.st_size
                if content_length + hashed_untracked_bytes > _UNTRACKED_HASH_BUDGET_BYTES:
                    logger.debug(
                        "git_baseline: untracked hash budget exhausted (baseline recorded absent): %r",
                        relative_path,
                    )
                    return {"status": "unavailable"}
                hashed_untracked_bytes += content_length

                with os.fdopen(descriptor, "rb") as untracked_file:
                    descriptor = -1
                    digest.update(content_length.to_bytes(8, "big"))
                    while content_length:
                        chunk = untracked_file.read(min(_HASH_CHUNK_BYTES, content_length))
                        if not chunk:
                            break
                        digest.update(chunk)
                        content_length -= len(chunk)
                    if os.fstat(untracked_file.fileno()).st_size != opened_stat.st_size:
                        content_length = -1
            finally:
                if descriptor != -1:
                    os.close(descriptor)
            if content_length != 0:
                logger.debug(
                    "git_baseline: untracked file changed while reading "
                    "(baseline recorded absent): %r",
                    relative_path,
                )
                return {"status": "unavailable"}
    except OSError as e:
        logger.debug("git_baseline: untracked entry unavailable (baseline recorded absent): %s", e)
        return {"status": "unavailable"}

    return {"status": "dirty", "digest": f"sha256:{digest.hexdigest()}"}


def _git_marker_state(cwd: str) -> Optional[bool]:
    """Return whether a ``.git`` marker is visible on the readable filesystem walk."""
    try:
        current = os.path.realpath(cwd)
        current_stat = os.stat(current)
        if not stat.S_ISDIR(current_stat.st_mode):
            return None
        while True:
            with os.scandir(current) as entries:
                if any(entry.name == ".git" for entry in entries):
                    return True
            parent = os.path.dirname(current)
            if parent == current:
                return False
            parent_stat = os.stat(parent)
            if parent_stat.st_dev != current_stat.st_dev:
                return False
            current = parent
            current_stat = parent_stat
    except OSError:
        return None


def derive_baseline(cwd: str) -> Dict[str, Any]:
    """The repository baseline for a run starting in ``cwd``. Never raises.

    Returns ``{"available": False}`` when no verifiable baseline could be read,
    ``{"available": True, "repository": False}`` for determinate non-repositories,
    and ``{"available": True, "commit": <sha>, "worktree_state": <state>}`` for repositories.

    A successfully read commit may accompany ``available: False`` when the worktree snapshot itself
    was unavailable. The commit alone is not a complete baseline and cannot identify an approvable plan.

    ``available`` IS AN EXPLICIT FIELD rather than an absent key or a ``None`` commit, because the manifest
    is a durable record read later by an agent diagnosing a failed run: "we could not determine the
    repository state" and "the repository state is empty" call for different conclusions, and a reader should
    not have to infer which one a missing key meant.
    """
    environment = _git_environment()
    marker_before = _git_marker_state(cwd)
    if marker_before is None:
        return {"available": False}
    try:
        probe = subprocess.run(
            [
                "git",
                "--no-optional-locks",
                "-c",
                "core.fsmonitor=false",
                "rev-parse",
                "--is-inside-work-tree",
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False}
    if probe.returncode != 0:
        marker_after = _git_marker_state(cwd)
        if marker_before is False and marker_after is False:
            return {"available": True, "repository": False}
        return {"available": False}
    if probe.stdout.strip() != "true":
        return {"available": False}

    head = _run_git(["rev-parse", "HEAD"], cwd, environment)
    if head is None:
        return {"available": False}

    commit = head.stdout.strip()
    if not commit:
        return {"available": False}

    worktree_state = _worktree_state(cwd, environment)
    if worktree_state["status"] == "unavailable":
        return {"available": False, "commit": commit, "worktree_state": worktree_state}
    return {
        "available": True,
        "commit": commit,
        "worktree_state": worktree_state,
    }
