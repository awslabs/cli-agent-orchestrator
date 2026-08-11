"""Turn a user-supplied plugin source into a staging directory — and nothing else.

Two source kinds, both deliberately thin (decision D2). There is **no** name
resolution against an index, no version solving, no dependency graph, no
signature check, and no update-checking service. The manifest's ``version`` is
recorded for display and staleness comparison only, which is the one use §10.2
sanctions.

Staging is not an implementation detail: it is what makes validation meaningful.
A local source is **copied** rather than referenced in place, so the bytes that
get validated are the bytes that get published, and a plugin directory the
operator keeps editing cannot change under a running session.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from cli_agent_orchestrator.agent_plugins.containment import resolve_within_root
from cli_agent_orchestrator.agent_plugins.models import PluginSource

logger = logging.getLogger(__name__)

# A clone that cannot finish in this long is not going to; failing loudly beats
# a `cao plugin add` that hangs forever on an unreachable host.
GIT_TIMEOUT_S = 300

# Directory name the staged candidate plugin root is placed under.
_STAGE_DIRNAME = "source"

#: A **full** 40-hex commit id, and nothing shorter. `git clone --branch` cannot
#: take a commit, so a pinned commit takes the fetch path below — but an
#: abbreviated hash is deliberately rejected rather than resolved: abbreviations
#: are ambiguous by construction, and "install whatever object happens to match
#: this prefix today" is not a reproducible source. An install record replaying
#: its own `resolved_ref` always has the full id.
_FULL_COMMIT_RE = re.compile(r"\A[0-9a-fA-F]{40}\Z")


class ResolverError(RuntimeError):
    """Raised when a plugin source cannot be resolved into staging.

    Carries the underlying cause in its message. The installer turns this into a
    reported failure with the installed set untouched — nothing has been staged
    that could leak into the store.
    """


@dataclass(frozen=True)
class ResolvedSource:
    """A staged candidate plugin root, plus what the source resolved to."""

    root: Path
    """The candidate plugin root inside staging (``subdir`` already applied)."""

    staging: Path
    """The staging directory that owns ``root``; the caller deletes it."""

    resolved_ref: Optional[str] = None
    """Git commit SHA for a ``git`` source; ``None`` for a ``path`` source."""


def resolve(source: PluginSource, dest: Path) -> ResolvedSource:
    """Stage ``source`` under ``dest`` and return the candidate plugin root.

    Args:
        source: What the operator asked to install.
        dest: An existing, caller-owned staging directory. Everything written
            here is disposable; the installer deletes it whether or not the
            install succeeds.

    Raises:
        ResolverError: If the source is unreachable — an invalid local path, a
            failed git operation, or a ``subdir`` that does not exist or escapes
            the staged tree. The installed set is never touched on this path.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    if source.kind == "git":
        staged_root, resolved_ref = _resolve_git(source, dest)
    else:
        staged_root, resolved_ref = _resolve_path(source, dest)

    root = _apply_subdir(staged_root, source.subdir)
    return ResolvedSource(root=root, staging=dest, resolved_ref=resolved_ref)


def _resolve_path(source: PluginSource, dest: Path) -> tuple[Path, None]:
    """Copy a local directory into staging.

    ``symlinks=True`` is load-bearing, not a style choice. Following symlinks
    during the copy would materialize a link that points *outside* the source
    tree as real content inside the staged root — where containment would then
    happily accept it, because by then it genuinely is inside. Preserving links
    keeps the escape visible to ``containment.resolve_within_root``.
    """
    raw = os.path.expanduser(source.location.strip())
    if not raw:
        raise ResolverError("Plugin source path is empty")

    origin = Path(raw).resolve() if not os.path.isabs(raw) else Path(os.path.realpath(raw))
    if not origin.exists():
        raise ResolverError(f"Plugin source path does not exist: {source.location}")
    if not origin.is_dir():
        raise ResolverError(f"Plugin source path is not a directory: {source.location}")

    staged = dest / _STAGE_DIRNAME
    try:
        shutil.copytree(origin, staged, symlinks=True)
    except OSError as exc:
        raise ResolverError(f"Could not copy plugin source {source.location}: {exc}") from exc
    return staged, None


def _resolve_git(source: PluginSource, dest: Path) -> tuple[Path, Optional[str]]:
    """Shallow-clone a repository into staging and record the resolved commit."""
    location = source.location.strip()
    if not location:
        raise ResolverError("Plugin source git URL is empty")

    staged = dest / _STAGE_DIRNAME
    args: List[str] = [
        "clone",
        "--depth",
        "1",
        # Submodules are NOT initialized for a git plugin source. This is a
        # stated non-behavior, spelled with an explicit flag rather than left to
        # `git clone`'s current default: a future default change or a
        # differently-configured git would otherwise silently pull submodule
        # content into staging, which would then have to be reasoned about under
        # containment.
        "--no-recurse-submodules",
        # Tags are never used: nothing here resolves a version from the
        # repository, and `--depth 1` already fetches only the tip. Fetching them
        # would download refs and objects the install cannot use, which on a
        # release-heavy repository is most of the transfer.
        "--no-tags",
    ]
    if source.ref:
        args += ["--branch", source.ref]
    # `--` so a location beginning with `-` is never read as an option.
    args += ["--", location, str(staged)]

    try:
        _run_git(args, what=f"clone {location}")
    except ResolverError as exc:
        # `--branch` takes a branch or tag name only, so a caller pinning an exact
        # commit lands here — and that caller is not hypothetical: it is what
        # replaying an install record's own `resolved_ref` looks like.
        if source.ref and _FULL_COMMIT_RE.match(source.ref):
            _clone_at_commit(location, source.ref, staged)
        else:
            raise

    resolved_ref: Optional[str] = None
    try:
        resolved_ref = _run_git(["rev-parse", "HEAD"], what="resolve commit", cwd=staged)
    except ResolverError as exc:
        # A clone that produced a tree but no resolvable HEAD is odd but not
        # fatal to installing: record no ref rather than refusing the plugin.
        logger.warning("Could not resolve cloned commit for %s: %s", location, exc)

    # Read the commit BEFORE this point — the metadata it comes from is about to
    # be deleted.
    _strip_vcs_metadata(staged)

    return staged, resolved_ref


def _clone_at_commit(location: str, commit: str, staged: Path) -> None:
    """Fetch exactly one commit into ``staged``, for a full-hash pin.

    ``init`` + ``fetch --depth 1 <sha>`` + ``checkout FETCH_HEAD`` rather than a
    full clone followed by a checkout: fetching the single named object keeps the
    transfer as small as the shallow-clone path it substitutes for. It requires
    the server to allow fetching a reachable object by id — GitHub and GitLab do;
    where a server does not, the failure surfaces like any other git failure.
    """
    # A failed clone may have left a partial tree behind, or removed the
    # directory entirely. Either way, start from an empty one.
    if staged.exists():
        shutil.rmtree(staged, ignore_errors=True)
    staged.mkdir(parents=True, exist_ok=True)

    _run_git(["init", "--quiet"], what="init staging", cwd=staged)
    _run_git(["remote", "add", "origin", "--", location], what="add remote", cwd=staged)
    _run_git(
        ["fetch", "--depth", "1", "--no-recurse-submodules", "--no-tags", "origin", commit],
        what=f"fetch commit {commit[:12]}",
        cwd=staged,
    )
    _run_git(["checkout", "--quiet", "FETCH_HEAD"], what="checkout commit", cwd=staged)


def _strip_vcs_metadata(staged: Path) -> None:
    """Remove ``.git`` from the staged tree.

    Version-control metadata is not package bytes, and ``PLUGIN_ROOT`` is meant to
    hold the package. Two concrete reasons beyond tidiness:

    * ``git clone`` **silently ignores ``--depth`` for a local path source**, so a
      ``file://`` or local-path git source arrives with the repository's entire
      history attached. Publishing that into the store would grow it without
      bound and for no purpose.
    * A plugin's own ``.git`` in the store invites accidental operations against
      it — a tool walking the store and finding a repository will treat it as one.

    Handles all three shapes ``.git`` can take: a directory (ordinary clone), a
    file (worktree or submodule checkout, where it points elsewhere), and a
    symlink (which must be unlinked, never followed).
    """
    git_path = staged / ".git"
    try:
        if git_path.is_symlink() or git_path.is_file():
            git_path.unlink(missing_ok=True)
        elif git_path.is_dir():
            shutil.rmtree(git_path, ignore_errors=True)
    except OSError as exc:  # pragma: no cover - best effort
        logger.warning("Could not remove VCS metadata from staged plugin: %s", exc)


def _git_env() -> dict:
    """Environment for every git subprocess, with interactivity fully disabled.

    Cloning an arbitrary operator-supplied URL must never block waiting for
    input. In the CLI a prompt looks like a hang; in the API server it would pin
    a worker until the timeout. Four separate mechanisms can prompt, so all four
    are closed:

    * ``GIT_TERMINAL_PROMPT=0`` — git's own username/password prompt.
    * ``GIT_ASKPASS=""`` / ``SSH_ASKPASS=""`` — the graphical/helper prompt path,
      which ignores ``GIT_TERMINAL_PROMPT`` entirely. Empty rather than unset:
      unsetting would let git fall back to a system default helper.
    * ``GCM_INTERACTIVE=never`` — Git Credential Manager, which is its own
      process with its own UI and does not honour any of the above.
    * ``GIT_SSH_COMMAND=ssh -oBatchMode=yes`` — refuses an unknown-host-key or
      passphrase prompt for an ``ssh://`` or ``git@`` remote.

    Closing the prompts is also a small confidentiality property, not only an
    availability one: a helper that *did* answer would hand credentials for one
    host to whatever URL the plugin source named.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env["SSH_ASKPASS"] = ""
    env["GCM_INTERACTIVE"] = "never"
    env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")
    return env


def _run_git(args: List[str], *, what: str, cwd: Optional[Path] = None) -> str:
    """Run a git command with prompts disabled and a hard timeout.

    The two ``-c`` overrides apply to **every** subcommand, not just ``clone``,
    which is the point: ``fetch`` and ``checkout`` on the commit-pin path below
    would otherwise inherit the ambient configuration this exists to neutralize.

    * ``submodule.recurse=false`` — belt to ``--no-recurse-submodules``'s braces.
      An operator with ``submodule.recurse=true`` in their global config would
      otherwise have submodules pulled in by subcommands that take no such flag.
    * ``credential.helper=`` — empties the helper *chain* for this invocation, so
      no configured helper is consulted at all. ``_git_env`` stops a helper from
      blocking; this stops one from answering.
    """
    command = ["git", "-c", "submodule.recurse=false", "-c", "credential.helper=", *args]

    try:
        result = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            check=True,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            env=_git_env(),
        )
    except FileNotFoundError as exc:
        raise ResolverError(
            "git executable not found on PATH; a git plugin source needs it"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ResolverError(f"git {what} timed out after {GIT_TIMEOUT_S}s") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip().splitlines()
        message = detail[-1] if detail else f"exit {exc.returncode}"
        raise ResolverError(f"git {what} failed: {message}") from exc
    return result.stdout.strip()


def _apply_subdir(staged_root: Path, subdir: Optional[str]) -> Path:
    """Address a subdirectory of the staged tree as the candidate plugin root.

    Real-world plugins live in monorepo subdirectories — CAO's own packages live
    at ``agent-plugin/<name>/`` in this repository, which is what dogfoods this
    path rather than merely testing it.

    The subdirectory is resolved with the same realpath containment used
    everywhere else, so ``--subdir ../../etc`` (or a symlink standing in for it)
    is rejected instead of walking out of staging.
    """
    if not subdir or not subdir.strip():
        return staged_root

    candidate = resolve_within_root(staged_root, subdir.strip())
    if candidate is None:
        raise ResolverError(f"Plugin subdirectory escapes the source tree: {subdir!r}")
    if not candidate.is_dir():
        raise ResolverError(f"Plugin subdirectory does not exist in the source: {subdir!r}")
    return candidate
