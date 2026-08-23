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
    """Capture tracked and untracked changes without recording an absolute worktree path."""
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
            entry_mode = os.lstat(contents_path).st_mode
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
            content_length = 0
            with open(contents_path, "rb") as untracked_file:
                content_length = os.fstat(untracked_file.fileno()).st_size
                digest.update(content_length.to_bytes(8, "big"))
                while chunk := untracked_file.read(_HASH_CHUNK_BYTES):
                    digest.update(chunk)
                    content_length -= len(chunk)
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

    Returns ``{"available": False}`` when no baseline could be read, and
    ``{"available": True, "commit": <sha>, "worktree_state": <state>}`` when one could.

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

    return {"available": True, "commit": commit, "worktree_state": _worktree_state(cwd)}
