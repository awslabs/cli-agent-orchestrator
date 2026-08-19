"""Derive a run's repository baseline (issue #583 Bolt 2, unit ``manifest-freeze``).

The one thing about a script-tier plan that the workflow source hash CANNOT capture. Everything else the
manifest records about how a run will execute is in the script itself; the surrounding repository is not.
Two runs of an identical script against different commits are genuinely different plans, and a resume onto a
different commit is exactly the drift FR-12 wants diagnosable.

TOTAL BY CONSTRUCTION: nothing here raises, and nothing blocks indefinitely. Deriving a baseline must never
be the reason a run cannot start, so every failure — not a repository, ``git`` missing from ``PATH``, an
unreadable directory, a hung process — is reported as a RECORDED ABSENCE rather than an exception. Absence is
a representable state, not an error.

COMMIT AND DIRTY FLAG ONLY, AND THE OMISSIONS ARE DELIBERATE. No branch name, and above all NO PATH: a path
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

import logging
import subprocess
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Bounded so a hung git process cannot delay run start. Generous relative to the work — two local
# invocations that read refs — because the point is to fail eventually, not quickly.
_GIT_TIMEOUT_SECONDS = 10


def _run_git(args: list[str], cwd: str) -> Optional[subprocess.CompletedProcess]:
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
            text=True,
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


def derive_baseline(cwd: str) -> Dict[str, Any]:
    """The repository baseline for a run starting in ``cwd``. Never raises.

    Returns ``{"available": False}`` when no baseline could be read, and
    ``{"available": True, "commit": <sha>, "dirty": <bool>}`` when one could.

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

    # A repository with no commits yet, or any other state where the porcelain read fails, still yields a
    # usable commit-less answer rather than discarding the commit we already have.
    status = _run_git(["status", "--porcelain"], cwd)
    dirty = bool(status.stdout.strip()) if status is not None else None

    baseline: Dict[str, Any] = {"available": True, "commit": commit}
    if dirty is not None:
        baseline["dirty"] = dirty
    return baseline
