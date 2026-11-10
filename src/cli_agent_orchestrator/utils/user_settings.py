"""Merge keys into a provider's user settings file on the execution host (#745).

A provider sometimes prepares the user's own config before its CLI starts:
Claude Code's ``~/.claude/settings.json`` gets ``skipDangerousModePermissionPrompt``
so its bypass-permissions prompt does not block a headless pane. That file must
be the one on the host the CLI runs on. Under the remote terminal backend that
is a runner, so the server asks the runner to do it, and the runner does it
here, against its own home directory.

The merge is atomic (a same-directory temp file and ``os.replace``), keeps the
file's mode (0600 for a new file, which may later hold secrets), and is
fail-closed: an existing file that cannot be read or parsed as a JSON object is
left exactly as it is and nothing is written, so the user's other settings are
never replaced by a fragment (the #737 class). Only an absent file starts empty.

Only the files in :data:`USER_JSON_FILES` can be named, by id, never by path.
"""

import json
import logging
import os
import stat
import threading
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

# id -> path below the user's home directory.
USER_JSON_FILES: Dict[str, tuple] = {
    "claude_settings": (".claude", "settings.json"),
}

# One merge at a time per process: N concurrent launches would otherwise
# interleave their read-modify-writes and lose keys.
_WRITE_LOCK = threading.Lock()


class UserSettingsUnreadable(Exception):
    """The settings file exists but is not a readable JSON object; it was left untouched."""


def user_json_path(file_id: str) -> Path:
    """The path of the allowlisted settings file ``file_id`` on this host."""
    parts = USER_JSON_FILES.get(file_id)
    if parts is None:
        raise ValueError(f"unknown user settings file {file_id!r}")
    return Path.home().joinpath(*parts)


class _Unchanged(Exception):
    """Every key already holds its value: nothing to publish."""


def merge_user_json(file_id: str, updates: Dict[str, Any]) -> bool:
    """Set ``updates`` in the settings file ``file_id``; True if the file changed.

    Raises :class:`UserSettingsUnreadable` (and writes nothing) when the file
    exists but cannot be read or is not a JSON object.

    The rewrite goes through ``utils.atomic_file.locked_atomic_rewrite``: an
    inter-process lock for the whole read-modify-write, a uniquely named temp
    file created private (0600, exclusive) and given the final mode only just
    before ``os.replace``, so the content is never readable by others while it
    is written, and a pre-created path cannot redirect it.
    """
    from cli_agent_orchestrator.utils.atomic_file import locked_atomic_rewrite

    if not isinstance(updates, dict) or not all(isinstance(k, str) for k in updates):
        raise ValueError("updates must be an object with string keys")
    path = user_json_path(file_id)
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        mode = 0o600  # a new file may later hold env or apiKeyHelper secrets
    except OSError as exc:
        raise UserSettingsUnreadable(f"{path} is not readable; left untouched: {exc}")

    def merged(existing: str) -> str:
        if existing == "" and not path.exists():
            settings: Dict[str, Any] = {}
        else:
            try:
                loaded = json.loads(existing)
            except ValueError as exc:
                raise UserSettingsUnreadable(f"{path} is not valid JSON; left untouched: {exc}")
            if not isinstance(loaded, dict):
                raise UserSettingsUnreadable(f"{path} is not a JSON object; left untouched")
            settings = loaded
        if all(settings.get(key) == value for key, value in updates.items()):
            raise _Unchanged()
        settings.update(updates)
        return json.dumps(settings, indent=2)

    with _WRITE_LOCK:
        try:
            locked_atomic_rewrite(path, merged, mode=mode)
        except _Unchanged:
            return False
        except UnicodeDecodeError as exc:
            raise UserSettingsUnreadable(f"{path} is not text; left untouched: {exc}")
        except PermissionError as exc:
            raise UserSettingsUnreadable(f"{path} is not readable; left untouched: {exc}")
    logger.info(f"set {sorted(updates)} in {path}")
    return True
