"""Derive a run's repository baseline (issue #583 Bolt 2, unit ``manifest-freeze``).

The one thing about a script-tier plan that the workflow source hash CANNOT capture. Everything else the
manifest records about how a run will execute is in the script itself; the surrounding repository is not.
Two runs of an identical script against different commits are genuinely different plans, and a resume onto a
different commit is exactly the drift FR-12 wants diagnosable.

TOTAL BY CONSTRUCTION: nothing here raises, and nothing blocks indefinitely. Deriving a baseline must never
be the reason a run cannot start, so every failure — not a repository, ``git`` missing from ``PATH``, an
unreadable directory, a hung process — is reported as a RECORDED ABSENCE rather than an exception. Absence is
a representable state, not an error.

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
    args: list[str], cwd: str, *, text: bool = True
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
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=text,
            timeout=_GIT_TIMEOUT_SECONDS,
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


def _worktree_state(cwd: str) -> Dict[str, str]:
    """Capture tracked and untracked changes without recording an absolute worktree path.

    Hash at most ``_UNTRACKED_HASH_BUDGET_BYTES`` of untracked regular-file content. Exhausting
    that aggregate limit, or observing a path race, records the state as unavailable rather than
    producing an identity from a partial snapshot.
    """
    tracked = _run_git(_TRACKED_DIFF_ARGS, cwd, text=False)
    untracked = _run_git(["ls-files", "--others", "--exclude-standard", "-z"], cwd, text=False)
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


def derive_baseline(cwd: str) -> Dict[str, Any]:
    """The repository baseline for a run starting in ``cwd``. Never raises.

    Returns ``{"available": False}`` when no verifiable baseline could be read, and
    ``{"available": True, "commit": <sha>, "worktree_state": <state>}`` when one could.

    A successfully read commit may accompany ``available: False`` when the worktree snapshot itself
    was unavailable. The commit alone is not a complete baseline and cannot identify an approvable plan.

    ``available`` IS AN EXPLICIT FIELD rather than an absent key or a ``None`` commit, because the manifest
    is a durable record read later by an agent diagnosing a failed run: "we could not determine the
    repository state" and "the repository state is empty" call for different conclusions, and a reader should
    not have to infer which one a missing key meant.
    """
    head = _run_git(["rev-parse", "HEAD"], cwd)
    if head is None:
        return {"available": False}

    commit = head.stdout.strip()
    if not commit:
        return {"available": False}

    worktree_state = _worktree_state(cwd)
    if worktree_state["status"] == "unavailable":
        return {"available": False, "commit": commit, "worktree_state": worktree_state}
    return {"available": True, "commit": commit, "worktree_state": worktree_state}
