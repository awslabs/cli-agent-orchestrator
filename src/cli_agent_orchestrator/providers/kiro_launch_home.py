"""Per-launch Kiro homes for ephemeral helpers, on Kiro's v3 engine (#1167 Phase 2).

An ephemeral Kiro helper (#1074) runs from a CAO-owned ``KIRO_HOME`` of its
own, ``realpath(CAO_HOME_DIR)/ephemeral/kiro/<terminal_id>/``, never from the
operator's ``~/.kiro`` or the shared ``KIRO_AGENTS_DIR``. The home holds:

- ``settings/cli.json``, which turns off agent auto-upgrade, the trust-all
  consent dialog and the greeting, and makes ``cao-null`` the default agent;
- ``agents/<name>.json``, Phase 1's universal agent file for the helper's
  bounded grant, from the same render core as ``cao install``, so the tool map,
  ``excludedTools`` and rules cannot drift;
- ``agents/cao-null.json``, the same render for the empty grant: a sentinel
  with no tools and every capability denied, so an omitted or mistyped
  ``--agent`` never falls back to Kiro's ``Default`` with every tool;
- two placeholders against Kiro's legacy copies from ``~/.kiro``: an empty
  ``powers/`` nobody can list (mode 0000), which Kiro's powers migration
  classifies "uninspectable" and skips without copying or writing its marker,
  and a ``{}`` ``secrets.json``, which turns the secret-store copy into a
  marker write (the TUI copies only into a missing target).

Every CAO file is created ``O_EXCL|O_NOFOLLOW`` at 0600 and hashed. Both agent
files pass Phase 1's validator from disk (``check_helper_files``) before the
home is sealed read-only and the window exists.

CAO never creates, opens, lists, edits, moves or deletes anything under
``~/.kiro``. ``check_kiro_global_home`` only stats Kiro's two sources and two
markers there, refuses a helper whose legacy copy no placeholder can stop, and
``check_global_home_unchanged`` refuses after the header when a placeholder
changed or a marker appeared.

Release stops every process whose ``KIRO_HOME`` is the home, then unseals and
removes the whole directory, Kiro's own session files included
(``remove_kiro_home``).

Provisional (#1167, #1074): every rule string here, the sealed modes, the
placeholders, and deleting Kiro's transcripts with the home.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.ephemeral import TOOL_ATOMS
from cli_agent_orchestrator.providers.kiro_v3_schema import KiroV3EngineSchema, KiroV3Refusal
from cli_agent_orchestrator.services.kiro_v3_validate import (
    SENTINEL_AGENT,
    validate_kiro_v3_agent_bytes,
)
from cli_agent_orchestrator.utils import private_home
from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config
from cli_agent_orchestrator.utils.private_files import (
    OwnedEntry,
    private_directory,
    remove_owned,
    write_exclusive,
)

logger = logging.getLogger(__name__)

KIRO_HOME_ENV = "KIRO_HOME"
KIRO_CLI = "kiro-cli"
CAO_MCP_SERVER = "cao-mcp-server"
CAO_MCP_GRANT = "@cao-mcp-server"
CAO_NULL_AGENT = SENTINEL_AGENT
SETTINGS_FILE = "settings/cli.json"
SENTINEL_FILE = f"agents/{CAO_NULL_AGENT}.json"
POWERS_DIR = "powers"
SECRETS_FILE = "secrets.json"

# Kiro's legacy copies (#1167 Phase 2): each copies a source under ~/.kiro into
# a relocated KIRO_HOME and then writes a marker under ~/.kiro. Read by stat only.
GLOBAL_KIRO_DIR = ".kiro"
POWERS_MARKER = ".powers-migrated"
SECRETS_MARKER = ".secrets-migrated"
# The staging entry Kiro's powers copy renames into place.
POWERS_MIGRATE_PREFIX = ".powers-migrate-"

_TERMINAL_ID = re.compile(r"[0-9a-f]{8}")

# K2's settings seed (kiro-cli 2.34.2, SK1), unchanged: engine-neutral. There is
# no chat.defaultModel: with no tier, Kiro uses its own default.
SETTINGS_SEED: dict[str, object] = {
    "chat.enableAutoAgentUpgrade": False,
    "chat.disableTrustAllConfirmation": True,
    "chat.greeting.enabled": False,
    "chat.defaultAgent": CAO_NULL_AGENT,
}
SENTINEL_DESCRIPTION = "CAO sentinel: no tools"
SENTINEL_PROMPT = "You have no tools. Reply exactly CAO_AGENT_NOT_LOADED and stop."
# The secret-store placeholder. Kiro may rewrite it once the helper runs.
SECRETS_PLACEHOLDER = b"{}\n"

# Process names that can own a per-launch home: kiro-cli and what it starts (the
# chat binary, bun running the TUI, node running the v3 engine, the
# cao-mcp-server Python child), and the pane's shells, whose environment
# carries the home too (Kiro's shell integration).
_KIRO_OWNER_NAMES = frozenset(
    {"kiro-cli", "kiro-cli-chat", "kiro-cli-term", "bun", "node", "cao-mcp-server"}
    | {"zsh", "bash", "sh", "fish", "dash"}
)
_HOME_PROCESS_TERM_GRACE_SECONDS = 1.0
_HOME_PROCESS_KILL_GRACE_SECONDS = 1.0


class KiroLaunchRefusal(KiroV3Refusal):
    """A Kiro helper launch CAO refused before any task was typed (#1167, #1074).

    ``rule`` (also ``code``) is the provisional machine string
    (``kiro_home_invalid:<reason>``, ``kiro_v3_global_home:<reason>`` and the
    rest). ``detail`` never carries a server path: the message reaches the API
    caller.
    """

    def __init__(self, rule: str, detail: str = "") -> None:
        self.rule = rule
        self.code = rule
        self.detail = detail
        super().__init__(f"{rule}: {detail or 'the Kiro helper launch was refused'}")


@dataclass(frozen=True)
class ResolvedKiroCli:
    """The kiro-cli the server resolved: the path typed, and what it links to."""

    path: str
    target: str


@dataclass(frozen=True)
class HomeFile:
    """One CAO-written file in a launch home, as it was created."""

    name: str
    device: int
    inode: int
    sha256: str


@dataclass(frozen=True)
class PlaceholderDir:
    """The ``powers/`` placeholder as CAO made it: an empty, unlistable directory."""

    device: int
    inode: int
    uid: int
    mode: int
    mtime_ns: int

    @classmethod
    def of(cls, info: os.stat_result) -> "PlaceholderDir":
        return cls(
            info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode), info.st_mtime_ns
        )

    def matches(self, info: os.stat_result) -> bool:
        """Still the same directory, owner and mode, with no entry added since (its mtime).

        The mtime is zero when recorded (``_make_placeholder``), so a change in the
        same clock tick as the creation still shows.
        """
        return stat.S_ISDIR(info.st_mode) and PlaceholderDir.of(info) == self

    def record(self) -> dict[str, int]:
        return {
            "device": self.device,
            "inode": self.inode,
            "uid": self.uid,
            "mode": self.mode,
            "mtime_ns": self.mtime_ns,
        }


@dataclass(frozen=True)
class KiroGlobalHome:
    """What CAO saw of the operator's ``~/.kiro`` before the launch, by ``stat`` alone.

    A source counts as present when anything is at its path, a dangling link
    included; a marker only when it resolves, as Kiro's own check reads it.
    ``placeholder_listable`` is the self-test's result: whether this server's
    user can list a mode-0000 directory (root, or an ACL).
    """

    user_home: str
    powers_source: bool
    powers_marker: bool
    secrets_source: bool
    secrets_marker: bool
    placeholder_listable: bool

    @property
    def markers(self) -> tuple[bool, bool]:
        return (self.powers_marker, self.secrets_marker)

    def record(self) -> dict[str, object]:
        return {
            "powers_source": self.powers_source,
            "powers_marker": self.powers_marker,
            "secrets_source": self.secrets_source,
            "secrets_marker": self.secrets_marker,
            "placeholder_listable": self.placeholder_listable,
        }


@dataclass(frozen=True)
class KiroLaunchHome:
    """A prepared per-launch home and the identity of every entry CAO made in it."""

    terminal_id: str
    home: Path
    agent: str
    # settings/cli.json, the sentinel and the agent file: sealed and verified.
    files: tuple[HomeFile, ...]
    # The secret-store placeholder as created. Kiro's afterwards: never verified.
    secrets: HomeFile
    powers: PlaceholderDir
    global_home: KiroGlobalHome

    @property
    def agent_file(self) -> Path:
        return self.home / "agents" / f"{self.agent}.json"

    @property
    def sentinel_file(self) -> Path:
        return self.home / SENTINEL_FILE

    def hashes(self) -> dict[str, str]:
        """Every CAO file's creation hash, the secret-store placeholder's included."""
        return {entry.name: entry.sha256 for entry in (*self.files, self.secrets)}


@dataclass(frozen=True)
class KiroLaunch:
    """What an ephemeral ``KiroCliProvider`` launches, decided before the window exists."""

    home: KiroLaunchHome
    kiro_cli: ResolvedKiroCli
    # The bounded grant the per-launch file renders: allowed_tools ∪ {@cao-mcp-server}.
    grant: tuple[str, ...]
    working_directory: str
    model: Optional[str] = None
    effort: Optional[str] = None
    version: Optional[str] = None
    # The pinned engine schema the probed kiro-cli version runs under.
    schema: Optional[str] = None


# Homes this process prepared, by terminal id, so rollback can tell its own home
# from an entry someone else put at the path (a refused ``exists``).
_PREPARED: dict[str, KiroLaunchHome] = {}
_PREPARED_LOCK = threading.Lock()


def _ephemeral_dir() -> Path:
    # Read at call time: tests and a relocated CAO home patch the store's root.
    from cli_agent_orchestrator.services import ephemeral_service

    return ephemeral_service.EPHEMERAL_DIR


def kiro_homes_root() -> Path:
    """``realpath(CAO_HOME_DIR)/ephemeral/kiro``: nothing but terminal-id homes lives here."""
    return Path(os.path.realpath(_ephemeral_dir())) / "kiro"


def kiro_probe_home() -> Path:
    """The ``KIRO_HOME`` the helper's capability probe runs under, beside ``kiro/``."""
    return Path(os.path.realpath(_ephemeral_dir())) / "kiro-probe"


def valid_terminal_id(terminal_id: object) -> bool:
    return isinstance(terminal_id, str) and _TERMINAL_ID.fullmatch(terminal_id) is not None


def kiro_home_path(terminal_id: str) -> Path:
    """The one path a launch's home can have; a malformed id has none."""
    if not valid_terminal_id(terminal_id):
        raise ValueError("a Kiro home is named by an 8-hex terminal id")
    return kiro_homes_root() / terminal_id


def resolve_kiro_cli(which: Optional[Callable[[str], Optional[str]]] = None) -> ResolvedKiroCli:
    """Resolve ``kiro-cli`` on the server's PATH to an absolute, trusted executable.

    The pane's login shell can reorder PATH (Kiro's shell integration does), so
    the command typed into it names this path, and the probe runs it too. The
    path is kept unresolved (a link such as ``~/.local/bin/kiro-cli`` survives
    Kiro's bundle updates); its target must be a regular executable file that
    neither group nor others can write. A path holding ``=`` refuses:
    ``/usr/bin/env`` would read it as an assignment and exec another program,
    so the binary launched would not be the binary probed.
    """
    found = (which or shutil.which)(KIRO_CLI)
    if not found:
        raise KiroLaunchRefusal("kiro_cli_unresolved", "kiro-cli is not on the server's PATH")
    if not os.path.isabs(found):
        raise KiroLaunchRefusal(
            "kiro_cli_unresolved", "kiro-cli resolved through a relative PATH entry"
        )
    if "=" in found:
        raise KiroLaunchRefusal(
            "kiro_cli_unresolved",
            "kiro-cli's path holds '=', which /usr/bin/env would read as an assignment",
        )
    target = os.path.realpath(found)
    try:
        info = os.stat(target)
    except OSError:
        raise KiroLaunchRefusal("kiro_cli_unresolved", "kiro-cli cannot be read") from None
    if (
        not stat.S_ISREG(info.st_mode)
        or not os.access(target, os.X_OK)
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise KiroLaunchRefusal(
            "kiro_cli_unresolved",
            "kiro-cli is not a regular executable writable only by its owner",
        )
    return ResolvedKiroCli(path=found, target=target)


# --- the operator's ~/.kiro: stat only ------------------------------------------


def server_home() -> str:
    """The server's ``HOME``, which tmux and herdr give the pane and Kiro reads as ``~``.

    Kiro takes ``process.env.HOME``; a server without an absolute one has no
    ``~/.kiro`` CAO can reason about, so the helper refuses.
    """
    value = os.environ.get("HOME", "")
    if not value or not os.path.isabs(value):
        raise KiroLaunchRefusal(
            "kiro_v3_global_home:unknown",
            "the CAO server has no absolute HOME, so CAO cannot tell what Kiro would copy "
            "from ~/.kiro into a helper's home",
        )
    return value


def _global_entry(user_home: str, name: str) -> str:
    return os.path.join(user_home, GLOBAL_KIRO_DIR, name)


def _source_present(path: str) -> bool:
    """Anything at ``path``, a dangling link included; an unreadable path counts too."""
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    return True


def _marker_present(path: str) -> bool:
    """A marker counts only when it resolves, as Kiro's ``existsSync`` reads it."""
    try:
        os.stat(path)
    except OSError:
        return False
    return True


def snapshot_kiro_global_home(user_home: str, *, placeholder_listable: bool) -> KiroGlobalHome:
    """Kiro's two legacy-copy sources and two markers under ``user_home``, by stat alone."""
    return KiroGlobalHome(
        user_home=user_home,
        powers_source=_source_present(_global_entry(user_home, POWERS_DIR)),
        powers_marker=_marker_present(_global_entry(user_home, POWERS_MARKER)),
        secrets_source=_source_present(_global_entry(user_home, SECRETS_FILE)),
        secrets_marker=_marker_present(_global_entry(user_home, SECRETS_MARKER)),
        placeholder_listable=placeholder_listable,
    )


def placeholder_is_listable(path: Path) -> bool:
    """The self-test: True unless listing the mode-0000 ``path`` fails with a permission error."""
    try:
        os.listdir(path)
    except PermissionError:
        return False
    except OSError:
        return True  # Not proven unlistable.
    return True


def check_kiro_global_home(probe_home: Path) -> KiroGlobalHome:
    """Refuse a helper whose Kiro would copy operator state from ``~/.kiro`` (#1167 Phase 2).

    Stage A, before bind and before any kiro-cli process. Kiro's powers
    migration skips when ``~/.kiro/powers`` is absent or its marker exists, and
    otherwise skips the helper's unlistable placeholder too, which only holds
    when this user cannot list a mode-0000 directory: the self-test lists the
    probe home's placeholder. Its secret-store copy skips only when
    ``~/.kiro/secrets.json`` is absent or its marker exists, and no placeholder
    can stop it without a change to ``~/.kiro``. The snapshot is kept, so the
    check after the header can see a marker appear.

    Raises ``KiroLaunchRefusal``: ``kiro_v3_global_home:unknown``, ``:powers`` or
    ``:secrets``.
    """
    user_home = server_home()
    listable = placeholder_is_listable(probe_home / POWERS_DIR)
    snapshot = snapshot_kiro_global_home(user_home, placeholder_listable=listable)
    if listable and snapshot.powers_source and not snapshot.powers_marker:
        logger.warning(
            "Kiro helper refused: this server's user can list a mode-0000 directory, so "
            "Kiro would copy ~/.kiro/powers into the helper's home"
        )
        raise KiroLaunchRefusal(
            "kiro_v3_global_home:powers",
            "this server's user can list an unreadable folder (it runs as root, or an ACL "
            "grants it), so the helper's empty powers placeholder cannot stop Kiro from "
            "copying your ~/.kiro powers into it. CAO changes nothing in ~/.kiro. Use a "
            "Claude Code or Codex helper, or an installed Kiro agent",
        )
    if snapshot.secrets_source and not snapshot.secrets_marker:
        logger.warning(
            "Kiro helper refused: ~/.kiro/secrets.json exists and "
            "~/.kiro/.secrets-migrated does not"
        )
        raise KiroLaunchRefusal(
            "kiro_v3_global_home:secrets",
            "you have a Kiro secret store (~/.kiro/secrets.json) that Kiro has not marked as "
            "migrated, and Kiro would copy it into the helper's home on its first session. "
            "CAO changes nothing in ~/.kiro. Use a Claude Code or Codex helper, or an "
            "installed Kiro agent, until Kiro offers a way to turn the copy off",
        )
    return snapshot


def check_global_home_unchanged(prepared: KiroLaunchHome) -> None:
    """Refuse once Kiro's legacy copies ran for this home, before any task (#1167 Phase 2).

    Called after the header, when Kiro's migrations have run. Refuses
    ``kiro_v3_global_home:changed`` when the ``powers/`` placeholder is no
    longer the directory CAO made (its identity, owner, mode or contents), when
    a powers staging entry is in the home, or when either marker's existence
    differs from the stage-A snapshot. The sources are left out on purpose: the
    operator's own Kiro may rewrite them at any time.
    """
    reasons: list[str] = []
    try:
        if not prepared.powers.matches(os.lstat(prepared.home / POWERS_DIR)):
            reasons.append("the powers placeholder changed")
    except OSError:
        reasons.append("the powers placeholder is gone")
    try:
        if any(entry.startswith(POWERS_MIGRATE_PREFIX) for entry in os.listdir(prepared.home)):
            reasons.append("a powers migration entry is in the home")
    except OSError:
        reasons.append("the home cannot be listed")
    before = prepared.global_home
    now = snapshot_kiro_global_home(
        before.user_home, placeholder_listable=before.placeholder_listable
    )
    if now.markers != before.markers:
        reasons.append("a ~/.kiro migration marker appeared or vanished")
    if reasons:
        logger.warning(
            "Kiro home %s for terminal %s: Kiro's legacy copies ran: %s",
            prepared.home,
            prepared.terminal_id,
            "; ".join(reasons),
        )
        raise KiroLaunchRefusal(
            "kiro_v3_global_home:changed",
            "Kiro copied, or tried to copy, your ~/.kiro state into the helper's home when its "
            "session started. No task was sent, the home is removed on release, and CAO "
            "changed nothing in ~/.kiro",
        )


# --- Kiro's own KIRO_HOME rule ---------------------------------------------------


def kiro_home_ignored_reason(kiro_home: str, user_home: str) -> Optional[str]:
    """Why Kiro would ignore ``kiro_home`` and use ``~/.kiro`` instead; None when honoured.

    Kiro's own rule, in both the TUI and the engine: a ``KIRO_HOME`` that is
    not absolute (``relative``), that resolves to the root (``root``), or that
    resolves to ``user_home`` or one of its ancestors (``contains-home``) is
    ignored. Paths resolve as Kiro's do: the existing prefix by realpath, the
    rest as written.
    """
    if not os.path.isabs(kiro_home):
        return "relative"
    real = os.path.realpath(kiro_home)
    if os.path.dirname(real) == real:
        return "root"
    relative = os.path.relpath(os.path.realpath(user_home), real)
    if relative == os.curdir or (
        not relative.startswith(os.pardir) and not os.path.isabs(relative)
    ):
        return "contains-home"
    return None


#: What each of Kiro's reasons means for a helper's home, for the refusal message.
_IGNORED_CAUSES = {
    "contains-home": "the server's HOME is that folder or inside it",
    "relative": "the path is relative",
    "root": "the path is the root",
}


def require_kiro_home_honoured(kiro_home: Path) -> None:
    """Refuse ``kiro_home_invalid:ignored`` when Kiro would not use ``kiro_home`` at all.

    Judged on the path CAO will create, ``realpath(parent)/name``. A link
    someone planted at that path is never followed here: creating the home
    refuses it (``exists``, or an unsafe probe home) without touching it.
    """
    created = os.path.join(os.path.realpath(kiro_home.parent), kiro_home.name)
    if os.path.islink(created):
        return
    reason = kiro_home_ignored_reason(created, server_home())
    if reason is not None:
        logger.warning("Kiro would ignore KIRO_HOME=%s (%s)", kiro_home, reason)
        cause = _IGNORED_CAUSES.get(reason, reason)
        raise KiroLaunchRefusal(
            "kiro_home_invalid:ignored",
            f"Kiro would ignore the helper's KIRO_HOME ({cause}) and use ~/.kiro instead; "
            "set HOME, or CAO_HOME_DIR, so that HOME is not under $CAO_HOME_DIR/ephemeral/",
        )


# --- the agent files --------------------------------------------------------------


def kiro_helper_grant(allowed_tools: Sequence[str]) -> list[str]:
    """The bounded grant a helper's agent file renders: the atoms plus ``@cao-mcp-server``.

    A ``*`` or anything that is not a CAO atom refuses: this grant is the whole
    policy of the launch.
    """
    allowed = set(allowed_tools)
    if any(tool not in TOOL_ATOMS and tool != CAO_MCP_GRANT for tool in allowed):
        raise KiroLaunchRefusal(
            "kiro_agent_invalid:tools", "an ephemeral Kiro helper takes CAO tool atoms only"
        )
    return sorted(allowed | {CAO_MCP_GRANT})


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return (json.dumps(value, indent=2) + "\n").encode("utf-8")


def _cao_mcp_entry(terminal_id: str) -> dict[str, object]:
    """The runtime ``cao-mcp-server`` entry: an absolute command, this CAO's address and the child's id."""
    # A runtime entry, not a persisted one: the home belongs to this launch only,
    # so the running CAO's address and home (#1066) go into it.
    entry = resolve_mcp_server_config(
        {"command": CAO_MCP_SERVER, "args": []}, persisted=False, name=CAO_MCP_SERVER
    )
    command = entry.get("command")
    if not isinstance(command, str) or not os.path.isabs(command):
        raise KiroLaunchRefusal(
            "kiro_agent_invalid:mcp", "cao-mcp-server did not resolve to an absolute command"
        )
    entry["env"] = {**dict(entry.get("env") or {}), "CAO_TERMINAL_ID": terminal_id}
    return dict(entry)


def _render_universal(
    name: str,
    description: str,
    prompt: str,
    grant: Sequence[str],
    mcp_servers: Optional[dict[str, object]],
) -> bytes:
    # The render core ``cao install`` uses; imported here, install_service being large.
    from cli_agent_orchestrator.services.install_service import kiro_universal_agent_config

    config = kiro_universal_agent_config(
        name=name,
        description=description,
        prompt=prompt,
        allowed_tools=list(grant),
        mcp_servers=mcp_servers,
        resources=[],
        model=None,
    )
    return (config.model_dump_json(indent=2, exclude_none=True) + "\n").encode("utf-8")


def render_helper_agent(
    name: str, profile: AgentProfile, grant: Sequence[str], terminal_id: str
) -> bytes:
    """The helper's universal agent file, from the profile Step 0 verified by hash.

    ``tools``, ``excludedTools`` and ``permissions.rules`` come from the render
    core ``cao install`` uses, ``allowedTools`` is the grant, and the only MCP
    server is ``cao-mcp-server`` with this CAO's address and the terminal id.
    ``resources`` is empty and there is no model, hooks, ``toolsSettings``,
    ``toolAliases`` or ``includePowers``: the model goes on the command line.
    """
    return _render_universal(
        name,
        profile.description,
        profile.system_prompt or "",
        grant,
        {CAO_MCP_SERVER: _cao_mcp_entry(terminal_id)},
    )


def render_sentinel_agent() -> bytes:
    """``cao-null``: the same render for the empty grant, so no tool and every capability denied."""
    return _render_universal(CAO_NULL_AGENT, SENTINEL_DESCRIPTION, SENTINEL_PROMPT, [], None)


def _read_no_follow(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(fd)


def check_helper_files(
    prepared: KiroLaunchHome, grant: Sequence[str], schema: KiroV3EngineSchema
) -> None:
    """Refuse unless both agent files, read again from disk, are what this launch needs.

    Each is re-read without following a link and must pass Phase 1's
    validator against the probed engine's ``schema``: the helper file with
    this launch's grant as its allowlist, the sentinel with the empty one. That
    gives set equality of ``tools``, ``excludedTools``, the rules and the MCP
    servers with CAO's render. The helper shape is stricter than an installed
    file's: ``mcpServers`` is exactly ``cao-mcp-server`` (an empty set would
    pass the validator), with an absolute command; ``resources`` is empty; and
    there is no ``model``. The sentinel configures no server.
    """
    checks: tuple[tuple[Path, str, list[str], bool], ...] = (
        (prepared.agent_file, prepared.agent, list(grant), False),
        (prepared.sentinel_file, CAO_NULL_AGENT, [], True),
    )
    for path, name, allowlist, sentinel in checks:
        which = "sentinel" if sentinel else "agent"
        try:
            raw = _read_no_follow(path)
        except OSError:
            raise KiroLaunchRefusal(
                "kiro_agent_invalid:unreadable", f"the helper's {which} file is unreadable"
            ) from None
        findings = validate_kiro_v3_agent_bytes(
            raw,
            schema,
            expected_name=name,
            launch_allowlist=allowlist,
            fallback_sentinel=sentinel,
        )
        if findings:
            logger.warning(
                "Kiro helper %s file %s failed validation: %s",
                which,
                path,
                "; ".join(f"{f.refusal}: {f.detail}" for f in findings),
            )
            raise KiroLaunchRefusal(
                findings[0].refusal, f"the helper's {which} file failed CAO's v3 validator"
            )
        data = json.loads(raw)
        servers = data.get("mcpServers")
        if sentinel:
            if servers not in (None, {}):
                raise KiroLaunchRefusal(
                    "kiro_agent_invalid:mcp", "the sentinel configures an MCP server"
                )
        elif not isinstance(servers, dict) or set(servers) != {CAO_MCP_SERVER}:
            raise KiroLaunchRefusal(
                "kiro_agent_invalid:mcp", "mcpServers is not cao-mcp-server alone"
            )
        else:
            command = servers[CAO_MCP_SERVER].get("command")
            if not isinstance(command, str) or not os.path.isabs(command):
                raise KiroLaunchRefusal(
                    "kiro_agent_invalid:mcp", "cao-mcp-server's command is not absolute"
                )
        if data.get("resources") != [] or "model" in data:
            raise KiroLaunchRefusal(
                "kiro_agent_invalid:shape", f"the helper's {which} file carries extra configuration"
            )


def launch_agent_grant(prepared: KiroLaunchHome) -> tuple[list[str], list[str]]:
    """The grant and MCP server names of the per-launch file, read again and hash-checked.

    The attestation compares the pane with these, never with the installed
    agent store: the file must still be the bytes CAO rendered.
    """
    recorded = prepared.hashes().get(f"agents/{prepared.agent}.json")
    try:
        raw = _read_no_follow(prepared.agent_file)
        data = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        data = None
        raw = b""
    if (
        recorded is None
        or not isinstance(data, dict)
        or hashlib.sha256(raw).hexdigest() != recorded
        or not isinstance(data.get("allowedTools"), list)
        or not isinstance(data.get("mcpServers"), dict)
    ):
        raise KiroLaunchRefusal("kiro_home_invalid:modified", "the helper's agent file changed")
    return [str(entry) for entry in data["allowedTools"]], sorted(
        str(server) for server in data["mcpServers"]
    )


# --- the home ---------------------------------------------------------------------


def _set_mode_no_follow(
    path: Path, mode: int, *, directory: bool, expected: Optional[HomeFile]
) -> None:
    flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if directory else os.O_NONBLOCK)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if expected is not None and (info.st_dev, info.st_ino) != (expected.device, expected.inode):
            raise OSError("the entry was replaced")
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _make_placeholder(path: Path) -> PlaceholderDir:
    """Make the empty directory just created at ``path`` unlistable, through a no-follow fd.

    Its times are set to zero before it is recorded. A file system stamps a
    change with a coarse clock (one tick is some milliseconds on Linux), and
    ext4 hands a freed inode out again at once, so an entry added, or the
    directory made again, in the tick of its creation could leave the same
    record. Any later change stamps the current time, which is never zero.
    """
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchmod(fd, 0)
        os.utime(fd, ns=(0, 0))
        return PlaceholderDir.of(os.fstat(fd))
    finally:
        os.close(fd)


def _plain_name(name: object) -> bool:
    """One plain path component: not empty, no separator, NUL or ``..``."""
    return (
        isinstance(name, str)
        and bool(name)
        and name != "."
        and ".." not in name
        and "/" not in name
        and os.sep not in name
        and "\0" not in name
    )


def create_launch_home(
    terminal_id: str, agent: str, agent_payload: bytes, global_home: KiroGlobalHome
) -> KiroLaunchHome:
    """Create, seed and fill one launch's home; it must not exist yet.

    ``agent`` must be a plain file name (``kiro_agent_invalid:name`` otherwise),
    and Kiro must honour the path as a ``KIRO_HOME`` (``kiro_home_invalid:ignored``
    otherwise), both before anything is made. The home is made by this call or not
    at all: an entry already at the path, of any type, refuses
    ``kiro_home_invalid:exists`` and is left untouched. On any later failure
    every entry this call created is removed again.
    """
    if not _plain_name(agent):
        raise KiroLaunchRefusal(
            "kiro_agent_invalid:name", "the helper's name is not a plain file name"
        )
    home = kiro_home_path(terminal_id)
    root = home.parent
    require_kiro_home_honoured(home)
    try:
        private_directory(root.parent)
        private_directory(root)
    except OSError:
        raise KiroLaunchRefusal("kiro_home_invalid:root", "the Kiro home root is unsafe") from None
    try:
        os.mkdir(home, 0o700)
    except FileExistsError:
        raise KiroLaunchRefusal(
            "kiro_home_invalid:exists", "an entry already exists at the launch's home path"
        ) from None
    except OSError:
        raise KiroLaunchRefusal("kiro_home_invalid:create", "the home cannot be created") from None

    owned: list[OwnedEntry] = []
    made: list[Path] = [home]
    files: list[HomeFile] = []

    def created(name: str, payload: bytes) -> HomeFile:
        write_exclusive(home / name, payload, owned)
        _, device, inode = owned[-1]
        return HomeFile(name, device, inode, hashlib.sha256(payload).hexdigest())

    try:
        _set_mode_no_follow(home, 0o700, directory=True, expected=None)
        for sub in ("settings", "agents"):
            os.mkdir(home / sub, 0o700)
            made.append(home / sub)
            _set_mode_no_follow(home / sub, 0o700, directory=True, expected=None)
        for name, payload in (
            (SETTINGS_FILE, _json_bytes(SETTINGS_SEED)),
            (SENTINEL_FILE, render_sentinel_agent()),
            (f"agents/{agent}.json", agent_payload),
        ):
            files.append(created(name, payload))
        secrets = created(SECRETS_FILE, SECRETS_PLACEHOLDER)
        os.mkdir(home / POWERS_DIR, 0o700)
        made.append(home / POWERS_DIR)
        powers = _make_placeholder(home / POWERS_DIR)
    except BaseException as exc:
        remove_owned(owned)
        for directory in reversed(made):
            try:
                os.rmdir(directory)
            except OSError:
                pass
        if isinstance(exc, KiroLaunchRefusal):
            raise
        if isinstance(exc, (OSError, ValueError)):
            raise KiroLaunchRefusal(
                "kiro_home_invalid:write", "the home cannot be written"
            ) from None
        raise
    prepared = KiroLaunchHome(terminal_id, home, agent, tuple(files), secrets, powers, global_home)
    with _PREPARED_LOCK:
        _PREPARED[terminal_id] = prepared
    return prepared


def discard_launch_home(terminal_id: str) -> None:
    """Forget a prepared home this process no longer owns (it was removed)."""
    with _PREPARED_LOCK:
        _PREPARED.pop(terminal_id, None)


def prepared_launch_home(terminal_id: str) -> Optional[KiroLaunchHome]:
    with _PREPARED_LOCK:
        return _PREPARED.get(terminal_id)


def verify_launch_home(prepared: KiroLaunchHome, *, sealed: bool) -> None:
    """Refuse unless the home is still exactly what CAO prepared.

    The path is the launch's own, lexically and by realpath; the home is a
    0700 directory owned by this user, not a symlink; ``agents/`` holds exactly
    the helper file and the sentinel; ``powers/`` is still CAO's empty
    placeholder (the same directory, owner and mode 0000); and every sealed CAO
    file is the same inode with the same bytes and mode (0400 once sealed,
    with ``agents/`` at 0500). ``secrets.json``, ``sessions/`` and the rest of
    ``settings/`` are Kiro's and are not checked.
    """

    def refuse(reason: str) -> KiroLaunchRefusal:
        logger.warning(
            "Kiro home %s for terminal %s failed its check: %s",
            prepared.home,
            prepared.terminal_id,
            reason,
        )
        return KiroLaunchRefusal("kiro_home_invalid:modified", "the launch's home changed")

    home = prepared.home
    try:
        expected_path = kiro_home_path(prepared.terminal_id)
    except ValueError:
        raise refuse("terminal id") from None
    if home != expected_path or os.path.realpath(home) != str(home):
        raise refuse("path")
    euid = os.geteuid()
    try:
        for directory, mode in (
            (home, 0o700),
            (home / "settings", 0o700),
            (home / "agents", 0o500 if sealed else 0o700),
        ):
            info = os.lstat(directory)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != euid
                or stat.S_IMODE(info.st_mode) != mode
            ):
                raise refuse(f"directory {directory.name}")
        if not prepared.powers.matches(os.lstat(home / POWERS_DIR)):
            raise refuse("the powers placeholder")
        entries = set(os.listdir(home / "agents"))
        if entries != {f"{prepared.agent}.json", f"{CAO_NULL_AGENT}.json"}:
            raise refuse("agents/ entries")
        file_mode = 0o400 if sealed else 0o600
        for recorded in prepared.files:
            path = home / recorded.name
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != euid
                    or (info.st_dev, info.st_ino) != (recorded.device, recorded.inode)
                    or stat.S_IMODE(info.st_mode) != file_mode
                ):
                    raise refuse(recorded.name)
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    digest = hashlib.sha256(stream.read()).hexdigest()
            finally:
                os.close(fd)
            if digest != recorded.sha256:
                raise refuse(recorded.name)
    except OSError:
        raise refuse("unreadable") from None


def seal_launch_home(prepared: KiroLaunchHome) -> None:
    """Make every sealed CAO file 0400 and ``agents/`` 0500, through no-follow descriptors.

    A child's ``write`` and ``code`` tools then get EACCES on the agent files,
    and ``agents/`` takes no new entry. A child with ``execute_bash`` can chmod
    them back: the seal is integrity evidence, not a boundary. ``powers/`` is
    already 0000 from creation.
    """
    try:
        for recorded in prepared.files:
            _set_mode_no_follow(
                prepared.home / recorded.name, 0o400, directory=False, expected=recorded
            )
        _set_mode_no_follow(prepared.home / "agents", 0o500, directory=True, expected=None)
    except OSError:
        raise KiroLaunchRefusal("kiro_home_invalid:seal", "the home cannot be sealed") from None


def kiro_env(home: Path) -> dict[str, str]:
    """The server environment with ``KIRO_HOME`` set to ``home``."""
    return {**os.environ, KIRO_HOME_ENV: str(home)}


def _ensure_probe_file(path: Path, payload: bytes) -> None:
    try:
        write_exclusive(path, payload, [])
    except FileExistsError:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise OSError("the probe file is not CAO's") from None


def _ensure_probe_placeholder(path: Path) -> None:
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise OSError("the probe placeholder is not CAO's") from None
        if stat.S_IMODE(info.st_mode) == 0:
            return
        # A placeholder left at 0700 (a crash before the chmod) is sealed again
        # only while it is still empty.
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if os.listdir(fd):
                raise OSError("the probe placeholder is not empty") from None
            os.fchmod(fd, 0)
        finally:
            os.close(fd)
        return
    _make_placeholder(path)


def ensure_probe_home() -> Path:
    """The CAO-owned home the helper's capability probe runs under.

    So that every kiro-cli process CAO starts for a helper has a CAO-owned
    absolute home; with the server's ``KIRO_HOME`` unset the probe would
    otherwise run against ``~/.kiro``. It holds the settings seed and the same
    two placeholders as a launch home; the stage-A self-test lists its
    ``powers/``. The probe runs only ``--help`` and ``--version``, so no session
    starts there and no migration runs.
    """
    probe = kiro_probe_home()
    require_kiro_home_honoured(probe)
    try:
        private_directory(probe.parent)
        private_directory(probe)
        private_directory(probe / "settings")
        _ensure_probe_file(probe / SETTINGS_FILE, _json_bytes(SETTINGS_SEED))
        _ensure_probe_file(probe / SECRETS_FILE, SECRETS_PLACEHOLDER)
        _ensure_probe_placeholder(probe / POWERS_DIR)
    except OSError:
        raise KiroLaunchRefusal("kiro_home_invalid:probe", "the probe home is unsafe") from None
    return probe


# --- release ----------------------------------------------------------------------


def _kiro_possible_owner(process_name: str) -> bool:
    return (
        process_name in _KIRO_OWNER_NAMES
        or process_name.startswith("kiro-cli")
        or process_name.startswith("python")
    )


KIRO_HOME_OWNERS = private_home.HomeOwners(
    env_key=KIRO_HOME_ENV, label="Kiro", possible_owner=_kiro_possible_owner
)


def _inspect_kiro_home_process(pid: int, home: Path) -> private_home.Inspection:
    # A launch's home is named by its terminal id, which every process of the
    # launch also carries as CAO_TERMINAL_ID. A process naming another terminal
    # is that terminal's, even with this KIRO_HOME (#1074).
    return private_home.inspect_home_process(pid, home, KIRO_HOME_OWNERS, terminal_id=home.name)


def _pid_uses_kiro_home(pid: int, home: Path) -> Optional[bool]:
    inspected = _inspect_kiro_home_process(pid, home)
    if inspected is None:
        return None
    return not isinstance(inspected, bool)


def stop_kiro_home_processes(home: Path) -> bool:
    """Stop every same-user process whose ``KIRO_HOME`` is exactly ``home``; True once none is left.

    The window is already killed. Kiro's children outlived it by up to about
    8 s in SK1, so they get SIGTERM, then SIGKILL, with Grok's 1 s graces. A
    candidate that cannot be inspected keeps the home (False). A candidate
    whose ``CAO_TERMINAL_ID`` is not the home's terminal id is never signalled.
    """
    return private_home.stop_home_processes(
        home,
        pids_using_home=lambda target: private_home.pids_using_home(
            target, _pid_uses_kiro_home, "Kiro"
        ),
        inspect_home_process=_inspect_kiro_home_process,
        label="Kiro",
        term_grace_seconds=_HOME_PROCESS_TERM_GRACE_SECONDS,
        kill_grace_seconds=_HOME_PROCESS_KILL_GRACE_SECONDS,
    )


def _modified(home: Path, names: Sequence[str], recorded: Mapping[str, str]) -> Optional[bool]:
    """Whether any of these CAO files changed since preparation; None without a record."""
    if not names or not all(name in recorded for name in names):
        return None
    for name in names:
        try:
            digest = hashlib.sha256(_read_no_follow(home / name)).hexdigest()
        except OSError:
            return True
        if digest != recorded[name]:
            return True
    return False


def _unseal(home: Path) -> bool:
    """Give ``agents/`` and ``powers/`` their owner bits back (0700), so ``rmtree`` can empty them.

    Both are changed by name through the home's own no-follow descriptor,
    refusing a link (``fchmodat`` with ``AT_SYMLINK_NOFOLLOW``), so nothing in
    removal works by path: ``powers/`` is mode 0000 and cannot be opened at
    all, and a home swapped for a link after its checks is never entered. Only
    a directory this user owns is changed.

    True when ``rmtree`` can then empty the home: each of the two is missing,
    is not a directory (``rmtree`` unlinks it), or is now 0700. False when
    either is still sealed: the platform cannot refuse a link that way, the
    directory is another user's, or the home cannot be opened. ``rmtree`` would
    then delete the other files before it failed, and the next removal would
    find CAO's files gone and record that as tampering, so the caller keeps
    the whole home instead.
    """
    try:
        fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return True  # Already gone: rmtree finds nothing.
    except OSError as exc:
        logger.warning("Kiro home %s could not be opened to unseal it: %s", home, exc)
        return False
    unsealed = True
    try:
        for name in ("agents", POWERS_DIR):
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode):
                    continue
                if info.st_uid != os.geteuid():
                    raise OSError(errno.EPERM, "the directory is another user's")
                os.chmod(name, 0o700, dir_fd=fd, follow_symlinks=False)
            except (OSError, NotImplementedError, ValueError) as exc:
                if isinstance(exc, OSError) and exc.errno == errno.ENOENT:
                    continue
                logger.warning("Kiro home %s: %s/ could not be unsealed: %s", home, name, exc)
                unsealed = False
    finally:
        os.close(fd)
    return unsealed


@dataclass(frozen=True)
class HomeRemoval:
    """The tamper evidence recorded with a removed home; None where nothing was recorded."""

    agent_modified: Optional[bool]
    settings_modified: Optional[bool]
    # The secret-store placeholder's bytes differ from creation (Kiro may write it).
    secrets_store_changed: Optional[bool] = None
    # A ~/.kiro migration marker's existence differs from the stage-A snapshot.
    global_home_changed: Optional[bool] = None


def remove_kiro_home(
    terminal_id: str,
    recorded: Optional[Mapping[str, str]],
    *,
    global_home: Optional[KiroGlobalHome] = None,
    stop_owners: Optional[Callable[[Path], bool]] = None,
    on_removed: Optional[Callable[[HomeRemoval], None]] = None,
) -> bool:
    """Remove one launch's home once no process can write into it; True when none is left.

    ``recorded`` is the proof that CAO prepared this home, as the hashes of its
    CAO files (from the launch object, or the audit after a restart); an empty
    mapping is proof without hashes. Without proof, an entry at the path is not
    CAO's (a refused ``exists``): it is left alone, and True is returned, since
    there is nothing of CAO's to retry. The path must be ``<root>/<terminal_id>``
    with the root at its realpath, and an owned directory, not a symlink.

    Owners are stopped first (``stop_owners``, by default
    ``stop_kiro_home_processes``); False when any may remain. The hashes and,
    given ``global_home``, the ``~/.kiro`` markers are compared before removal
    and handed to ``on_removed`` once the directory is gone. Deletion is
    ``shutil.rmtree`` only where it is fd-based and symlink-safe, and only once
    ``agents/`` and ``powers/`` are unsealed; otherwise the whole home is
    retained and False is returned.
    """
    if not valid_terminal_id(terminal_id):
        return True
    home = kiro_home_path(terminal_id)
    try:
        info = os.lstat(home)
    except FileNotFoundError:
        discard_launch_home(terminal_id)
        return True
    except OSError:
        return False
    if recorded is None:
        logger.warning(
            "Leaving %s alone: CAO has no record of preparing a Kiro home for terminal %s",
            home,
            terminal_id,
        )
        return True
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.geteuid()
        or os.path.realpath(home.parent) != str(home.parent)
    ):
        logger.warning("Leaving %s alone: it is not a CAO-owned Kiro home directory", home)
        return True
    if not (stop_owners or stop_kiro_home_processes)(home):
        return False
    if not shutil.rmtree.avoids_symlink_attacks:
        logger.warning("Retaining Kiro home %s: this platform has no fd-based rmtree", home)
        return False
    agent_names = [
        name for name in recorded if name.startswith("agents/") and name != SENTINEL_FILE
    ]
    removal = HomeRemoval(
        agent_modified=_modified(home, agent_names, recorded),
        settings_modified=_modified(home, [SETTINGS_FILE, SENTINEL_FILE], recorded),
        secrets_store_changed=_modified(home, [SECRETS_FILE], recorded),
        global_home_changed=(
            None
            if global_home is None
            else snapshot_kiro_global_home(
                global_home.user_home, placeholder_listable=global_home.placeholder_listable
            ).markers
            != global_home.markers
        ),
    )
    if not _unseal(home):
        logger.warning("Retaining Kiro home %s: it could not be unsealed for removal", home)
        return False
    try:
        shutil.rmtree(home)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Failed to remove Kiro home %s: %s", home, exc)
        return False
    discard_launch_home(terminal_id)
    if on_removed is not None:
        on_removed(removal)
    return True
