"""Derive a run's repository baseline (issue #583 Bolt 2, unit ``manifest-freeze``).

The one thing about a script-tier plan that the workflow source hash CANNOT capture. Everything else the
manifest records about how a run will execute is in the script itself; the surrounding repository is not.
Two runs of an identical script against different commits are genuinely different plans, and a resume onto a
different commit is exactly the drift FR-12 wants diagnosable.

TOTAL BY CONSTRUCTION: nothing here raises, and nothing blocks indefinitely. A directory that is
determinately not in a repository is a representable, approvable state. Failures that leave repository
identity indeterminate — ``git`` missing, an unreadable directory, or a hung process — remain unavailable
so the approval path fails closed.

COMMIT AND WORKTREE STATE IDENTIFY REPOSITORIES, WITH NO PATH. Identical repository state on two
machines remains the same plan. For a directory outside Git, there is no content identity to record, so
the root is identified by its canonical path AND by the filesystem's own identity for that directory
(``st_dev``/``st_ino``, :func:`root_identity`, issue #827 criterion 2), without claiming to fingerprint
the directory's contents.

THE DEVICE/INODE PAIR IS RECORDED ONLY FOR A NON-GIT ROOT, and that asymmetry is the design rather
than an omission. A path is not an identity: a directory moved away and replaced by a different
directory at the same path keeps the path, so a path-only baseline hands the replacement the original's
approval. For a GIT root the identity is the commit and the worktree digest — content, not location —
so a different directory at the same path already produces a different baseline, and binding the inode
there would instead destroy the property this module exists to provide: two checkouts in the same state
are ONE plan, on one machine or two (pinned by
``test_git_baseline.py::test_captures_no_branch_and_no_path``). The pair is therefore added exactly
where the identity was otherwise path-only.

ON THE DUPLICATION WITH ``worktree_service._run_git``. That function already has this module's exact
never-raises contract, and it is private to a service whose purpose (worktree management) is unrelated to
freezing a manifest. Promoting it would have been the THIRD Bolt-1-file promotion in a single Construction
pass, so a second wrapper is accepted here for testability and layering rather than because the sibling was
overlooked. **IF A THIRD CALLER EVER NEEDS A NEVER-RAISES GIT WRAPPER, CONSOLIDATE INTO A SHARED
``utils/git.py`` RATHER THAN ADDING A FOURTH.** Two is a bounded, documented cost; three is a pattern nobody
decided on.
"""

import errno
import hashlib
import logging
import os
import stat
import subprocess
from typing import Any, Dict, Optional, Union

logger = logging.getLogger(__name__)

UNAVAILABLE_REASONS = frozenset(
    {
        "untracked_too_large",
        "untracked_unsupported",
        "repository_unreadable",
        "git_unavailable",
        "git_timeout",
        "changed_during_snapshot",
        "internal",
        "no_recorded_identity",
        "profiles_unreadable",
    }
)


def normalize_unavailable_reason(reason: Optional[str]) -> str:
    """Keep diagnostics in the closed vocabulary; never echo an unknown reason."""
    return reason if reason is not None and reason in UNAVAILABLE_REASONS else "internal"


ROOT_IDENTITY_UNBOUND_REASONS = frozenset(
    {
        "root_identity_unsupported",
        "root_identity_unreadable",
    }
)
"""Why a non-Git root could not be identified by device and inode. DELIBERATELY NOT IN
:data:`UNAVAILABLE_REASONS`.

That set is the vocabulary ``approval_gate`` turns into a 503 that REFUSES the run. Failing a freeze
because a network mount reports no usable inode would make every script run on such a mount
unapprovable, which is a worse outcome than the path-only identity those runs already have today. So an
unbindable root falls back to today's behaviour — and the fallback is RECORDED rather than skipped: the
unbound marker is part of the baseline, therefore part of ``plan_id``, so "identified by device and
inode" and "could not be identified" are two different plans and an operator reading the manifest can
see which one they approved.
"""


def root_identity(path: str) -> Dict[str, Any]:
    """The filesystem's own identity for the directory at ``path``. Never raises (issue #827).

    ``{"bound": True, "device": "<st_dev>", "inode": "<st_ino>"}`` when the filesystem supplies a
    usable pair, otherwise ``{"bound": False, "reason": <closed reason>}``.

    DECIMAL STRINGS, NOT INTEGERS. These values are hashed into ``plan_id`` through
    ``plan_identifier._structure``, which renders an ``int`` leaf as ``str(value)`` anyway. Writing the
    strings here means the stored identity does not depend on that normalisation continuing to exist,
    and an inode above 2**53 cannot be mangled by a JSON reader that parses numbers as doubles.

    ``os.stat`` AND NOT ``os.lstat``: the baseline's root is ``os.path.realpath(cwd)``, so the identity
    must be the directory the run actually executes in, not a symlink pointing at it. The existing
    alias test (``test_non_git_roots_and_aliases_have_directory_identity``) pins that a symlinked root
    and its target are one plan.

    TWO FALLBACKS, BOTH RECORDED:

    * ``st_ino == 0`` — some network filesystems, container overlay mounts and FUSE drivers report a
      zero inode, and on Windows ``st_ino`` is 0 where the file index is unavailable. Zero is not an
      identity: every directory would share it, so comparing it would prove nothing while reading as a
      successful binding. Reason ``root_identity_unsupported``.
    * ``OSError`` — the directory cannot be stat'd at all. Reason ``root_identity_unreadable``.

    NO PLATFORM BRANCH. The module has none today and this function adds none: Python populates
    ``st_dev`` and ``st_ino`` on Windows (volume serial and file index) and the ``st_ino == 0`` arm
    covers the filesystems there that do not supply an index, so the same two arms handle Windows,
    network mounts and containers without naming any of them.

    NO PATH IN THE RETURN VALUE and none in a log line: the caller already records the root path
    separately when it is appropriate to, and this module's diagnostics stay path-free.
    """
    try:
        info = os.stat(path)
    except OSError:
        return {"bound": False, "reason": "root_identity_unreadable"}
    if info.st_ino == 0:
        return {"bound": False, "reason": "root_identity_unsupported"}
    return {"bound": True, "device": str(info.st_dev), "inode": str(info.st_ino)}


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
) -> Union[subprocess.CompletedProcess[Any], str]:
    """Run ``git <args>`` in ``cwd``, returning a closed reason when it could not run or did not succeed.

    LIST-ARGV, NEVER A SHELL STRING, and no value is interpolated into the arguments — every element is an
    authored literal and the only variable is the working directory. Command injection is closed by
    construction rather than by escaping.

    Failures return a fixed reason, distinguishing a timeout, launch failure and nonzero exit.
    No stderr or working-directory value becomes a reason.
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
    except subprocess.TimeoutExpired:
        return "git_timeout"
    except OSError:
        return "git_unavailable"
    if completed.returncode != 0:
        return "repository_unreadable"
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
    if isinstance(tracked, str) or isinstance(untracked, str):
        reason = tracked if isinstance(tracked, str) else untracked
        return {"status": "unavailable", "reason": str(reason)}

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
                return {"status": "unavailable", "reason": "untracked_unsupported"}

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
                        return {"status": "unavailable", "reason": "untracked_unsupported"}
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
                return {"status": "unavailable", "reason": "untracked_unsupported"}

            digest.update(b"F")
            nonblocking_flag = getattr(os, "O_NONBLOCK", None)
            nofollow_flag = getattr(os, "O_NOFOLLOW", None)
            if nonblocking_flag is None or nofollow_flag is None:
                logger.debug(
                    "git_baseline: required descriptor flags are unavailable "
                    "(baseline recorded absent): %r",
                    relative_path,
                )
                return {"status": "unavailable", "reason": "untracked_unsupported"}

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
                    return {"status": "unavailable", "reason": "changed_during_snapshot"}

                content_length = opened_stat.st_size
                if content_length + hashed_untracked_bytes > _UNTRACKED_HASH_BUDGET_BYTES:
                    logger.debug(
                        "git_baseline: untracked hash budget exhausted (baseline recorded absent): %r",
                        relative_path,
                    )
                    return {"status": "unavailable", "reason": "untracked_too_large"}
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
                return {"status": "unavailable", "reason": "changed_during_snapshot"}
    except OSError as e:
        logger.debug("git_baseline: untracked entry unavailable (baseline recorded absent): %s", e)
        reason = (
            "untracked_unsupported"
            if isinstance(e, PermissionError) or e.errno in {errno.EACCES, errno.EPERM}
            else "changed_during_snapshot"
        )
        return {"status": "unavailable", "reason": reason}

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


def derive_baseline(cwd: str, *, identity: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The repository baseline for a run starting in ``cwd``. Never raises.

    Returns ``{"available": False}`` when no verifiable baseline could be read,
    ``{"available": True, "repository": False, "root": <realpath>, "root_identity": <identity>}`` for
    non-repositories, and ``{"available": True, "commit": <sha>, "worktree_state": <state>}`` for
    repositories.

    ``identity`` is the :func:`root_identity` value the caller ALREADY TOOK for ``cwd``, mirroring
    ``launch_guard.capture(profile_set=...)``: ``manifest_freeze`` hands the per-run launch guard the
    same pair it folded into ``plan_id``, so the stat behind the identifier and the stat behind the
    guard are ONE stat and a root replaced between them cannot be recorded as the approved one
    (the P3-3 window, PR #1000). ``None`` takes it here, for callers with no freeze.

    A successfully read commit may accompany ``available: False`` when the worktree snapshot itself
    was unavailable. The commit alone is not a complete baseline and cannot identify an approvable plan.

    ``available`` IS AN EXPLICIT FIELD rather than an absent key or a ``None`` commit, because the manifest
    is a durable record read later by an agent diagnosing a failed run: "we could not determine the
    repository state" and "the repository state is empty" call for different conclusions, and a reader should
    not have to infer which one a missing key meant.
    """
    environment = _git_environment()
    # Taken BEFORE the git probes, so the identity describes the directory the probes then classified
    # rather than whatever is at the path after two subprocess round-trips.
    identity = root_identity(cwd) if identity is None else identity
    marker_before = _git_marker_state(cwd)
    if marker_before is None:
        return {"available": False, "reason": "git_unavailable"}
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
    except subprocess.TimeoutExpired:
        return {"available": False, "reason": "git_timeout"}
    except OSError:
        return {"available": False, "reason": "git_unavailable"}
    if probe.returncode != 0:
        marker_after = _git_marker_state(cwd)
        if marker_before is False and marker_after is False:
            return {
                "available": True,
                "repository": False,
                "root": os.path.realpath(cwd),
                "root_identity": identity,
            }
        return {
            "available": False,
            "reason": "git_unavailable" if marker_after is None else "repository_unreadable",
        }
    if probe.stdout.strip() != "true":
        return {"available": False, "reason": "repository_unreadable"}

    head = _run_git(["rev-parse", "HEAD"], cwd, environment)
    if isinstance(head, str):
        return {"available": False, "reason": head}

    commit = head.stdout.strip()
    if not commit:
        return {"available": False, "reason": "repository_unreadable"}

    worktree_state = _worktree_state(cwd, environment)
    if worktree_state["status"] == "unavailable":
        return {
            "available": False,
            "reason": worktree_state["reason"],
            "commit": commit,
            "worktree_state": worktree_state,
        }
    return {
        "available": True,
        "commit": commit,
        "worktree_state": worktree_state,
    }
