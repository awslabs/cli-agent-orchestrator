"""The static workspace check that gates ``--trust-workspace`` on Kiro v3 (#1167).

Without ``--trust-workspace`` v3 blocks on a "Trust this workspace?" dialog,
and trusting a folder loads its agents, MCP servers, hooks, settings and
workflows; a same-name workspace agent even wins over the installed one. CAO
passes the flag only after this check finds no such configuration in the
launch directory's ``.kiro/`` or any ancestor's, and runs it again after the
identity header, before the tools attestation.

Content-only entries are allowed (``steering/``, ``specs/``, ``skills/`` and
``AGENTS.md``; CAO's own memory steering lives in ``steering/``). Everything
else refuses, unknown entries and symlinks included: an entry this module
does not name is one nobody has measured. The user's own ``~/.kiro`` is
skipped; every launch loads it as user scope.

Provisional (#1167): the code ``kiro_v3_workspace_config:<entry>`` and the lists.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import Iterator, Optional

from cli_agent_orchestrator.providers.kiro_v3_schema import KiroV3Refusal

logger = logging.getLogger(__name__)

KIRO_WORKSPACE_DIR = ".kiro"
#: Content Kiro reads as text, never as configuration that runs or grants anything.
CONTENT_DIRS = frozenset({"steering", "specs", "skills"})
CONTENT_FILES = frozenset({"AGENTS.md"})
#: Named for the refusal message; any entry outside the content lists refuses anyway.
CONFIG_ENTRIES = frozenset({"agents", "hooks", "settings", "workflows", "extensions", "powers"})
#: What an installed v3 launch can do about it.
INSTALLED_REMEDY = "Remove it, launch from a worktree without it, or retry with engine 'v2'."
#: An ephemeral helper runs on v3 only (#1167 Phase 2), so v2 is no remedy.
HELPER_REMEDY = "Remove it, or launch the helper from a worktree without it."


class KiroWorkspaceError(KiroV3Refusal):
    """A ``.kiro/`` that trusting the workspace would load; nothing was allocated or typed."""

    def __init__(self, entry: str, path: str, remedy: str = INSTALLED_REMEDY) -> None:
        self.code = f"kiro_v3_workspace_config:{entry}"
        self.entry = entry
        self.path = path
        kind = (
            "Kiro workspace configuration"
            if entry in CONFIG_ENTRIES
            else "an entry CAO does not know"
        )
        # No absolute path in the message: it is the API's 400 body, and in
        # shared-server mode the caller is not the server's user. ``path`` is
        # logged by ``check_kiro_workspace`` (as #1074 keeps paths out).
        super().__init__(
            f"{self.code}: a .kiro/ in the launch directory or one of its parents holds "
            f"{kind} ({entry}), which Kiro v3 would load once the workspace is trusted "
            f"(the server log names the path). {remedy}"
        )


class KiroWorkspaceHomeError(KiroWorkspaceError):
    """An ephemeral helper launched from ``HOME``, or from under a ``HOME`` that is a git work tree.

    A helper runs from its own ``KIRO_HOME``, so ``~/.kiro/agents`` is no longer
    Kiro's user agent directory, and Kiro drops a workspace folder only when its
    ``.kiro/agents`` is that directory. A helper whose workspace folder is
    ``HOME`` would therefore load the operator's whole ``~/.kiro`` as trusted
    workspace configuration (#1167 Phase 2). Provisional (#1167).
    """

    def __init__(self, path: str) -> None:
        self.code = "kiro_v3_workspace_config:home"
        self.entry = "home"
        self.path = path
        KiroV3Refusal.__init__(
            self,
            f"{self.code}: an ephemeral Kiro helper cannot launch from the home folder, or "
            "from inside a home folder that is itself a git repository: Kiro would load "
            "your own ~/.kiro there as trusted workspace configuration (the server log "
            "names the path). Launch it from a repository folder, or use a worktree.",
        )


def _refuse_symlinks(root: str, entry: str) -> None:
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            path = os.path.join(directory, name)
            if os.path.islink(path):
                raise KiroWorkspaceError(f"{entry} (symlink)", path)


def _scan(kiro_dir: str) -> None:
    try:
        entries = sorted(os.scandir(kiro_dir), key=lambda e: e.name)
    except OSError:
        raise KiroWorkspaceError("unreadable", kiro_dir) from None
    for entry in entries:
        if entry.is_symlink():
            raise KiroWorkspaceError(f"{entry.name} (symlink)", entry.path)
        if entry.name in CONTENT_DIRS and entry.is_dir(follow_symlinks=False):
            _refuse_symlinks(entry.path, entry.name)
        elif entry.name in CONTENT_FILES and entry.is_file(follow_symlinks=False):
            continue
        else:
            raise KiroWorkspaceError(entry.name, entry.path)


def _workspace_dirs(cwd: str) -> Iterator[Path]:
    start = Path(os.path.realpath(cwd))
    yield start
    yield from start.parents


def check_kiro_workspace(cwd: str, home: Optional[str] = None) -> None:
    """Refuse when ``cwd`` or an ancestor has a ``.kiro/`` v3 would load as configuration.

    ``home`` is the user's home whose ``.kiro`` is skipped (default: this
    process's). Raises ``KiroWorkspaceError``.
    """
    try:
        _check_workspace_dirs(cwd, home)
    except KiroWorkspaceError as exc:
        logger.warning("Kiro v3 workspace check refused %s: %s", exc.path, exc.code)
        raise


def _check_workspace_dirs(cwd: str, home: Optional[str]) -> None:
    # The home directory itself is skipped, not whatever its ``.kiro`` resolves
    # to: a user may keep ``~/.kiro`` as a symlink, while a workspace ``.kiro``
    # that is a symlink (to ``~/.kiro`` or anywhere) still refuses.
    user_home = Path(os.path.realpath(home or str(Path.home())))
    for directory in _workspace_dirs(cwd):
        if directory == user_home:
            continue
        kiro_dir = directory / KIRO_WORKSPACE_DIR
        try:
            mode = os.lstat(kiro_dir).st_mode
        except FileNotFoundError:
            continue
        except OSError:
            raise KiroWorkspaceError("unreadable", str(kiro_dir)) from None
        if stat.S_ISLNK(mode):
            raise KiroWorkspaceError(".kiro (symlink)", str(kiro_dir))
        if not stat.S_ISDIR(mode):
            raise KiroWorkspaceError(".kiro (not a directory)", str(kiro_dir))
        _scan(str(kiro_dir))


def check_kiro_helper_workspace(cwd: str, home: str) -> None:
    """The workspace check for an ephemeral helper: ``HOME``'s own rule, then D11's.

    Refuses ``kiro_v3_workspace_config:home`` when ``cwd`` is ``home`` (by
    realpath), or lies inside it while ``home`` holds a ``.git`` entry of any
    type (v3's workspace folder is the launch directory in every capture; a
    git-root reading could only reach ``home`` that way). Otherwise ``home``'s
    own ``.kiro`` is skipped as an ancestor, as for installed agents, and every
    other ``.kiro/`` is checked as ``check_kiro_workspace`` does. Raises a
    ``KiroV3Refusal``.
    """
    real_cwd = Path(os.path.realpath(cwd))
    real_home = Path(os.path.realpath(home))
    if real_cwd == real_home or (
        real_home in real_cwd.parents and os.path.lexists(real_home / ".git")
    ):
        logger.warning("Kiro helper workspace check refused %s: launched from home", real_cwd)
        raise KiroWorkspaceHomeError(str(real_cwd))
    try:
        check_kiro_workspace(cwd, home=home)
    except KiroWorkspaceHomeError:
        raise
    except KiroWorkspaceError as exc:
        # The same code, with a remedy a helper can use.
        raise KiroWorkspaceError(exc.entry, exc.path, remedy=HELPER_REMEDY) from None


__all__ = [
    "KiroWorkspaceError",
    "KiroWorkspaceHomeError",
    "check_kiro_helper_workspace",
    "check_kiro_workspace",
]
