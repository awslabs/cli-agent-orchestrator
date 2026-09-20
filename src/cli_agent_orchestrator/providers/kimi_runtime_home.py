"""Per-worker runtime ``KIMI_CODE_HOME`` for Kimi Code (agent-core-v2).

Kimi Code resolves its entire user-global data root from ``KIMI_CODE_HOME``
(default ``~/.kimi-code``). That is the *only* redirect the CLI exposes for MCP
membership — A0 established that no launch-scoped MCP flag and no MCP-config env
override exist (``--mcp-config`` / ``--mcp-config-file`` are absent from the
0.43.1 option table). So per-worker MCP isolation means per-worker home:

    same real project cwd
    worker A -> runtime KIMI_CODE_HOME A -> mcp.json A
    worker B -> runtime KIMI_CODE_HOME B -> mcp.json B

A single shared home is **not** a valid substitute, because two CAO profiles may
declare different ``profile.mcpServers`` surfaces (worker A: ``cao`` + ``github``;
worker B: ``cao`` + ``postgres``). One ``mcp.json`` cannot represent both.

This module owns three things and nothing else:

1. resolving the *effective source* home from the same launch environment that
   resolved the ``kimi`` binary (never ``os.environ`` of the cao-server process —
   those can differ),
2. materialising a minimal, isolated runtime home from it, and
3. merging the CAO profile's MCP servers into that home's ``mcp.json``.

It deliberately does **not** copy the source home wholesale. Only items that
carry *semantics* are brought across; runtime-generated state stays behind so N
workers cannot corrupt each other's sessions, logs, or updates. The allowlist is
a list, not a denylist: a future Kimi release that invents a new runtime-state
directory cannot silently leak it into every worker's home.

**Workspace trust (A4).** ``workspace-trust/`` is the one directory that is
neither ordinary runtime state nor ordinary user semantics. It records
*decisions the operator already made in normal Kimi*, and Kimi consults it to
decide whether to raise its workspace-trust dialog at all. Discarding it meant
every CAO worker re-asked a question the operator had already answered, which
left the server-wide ``CAO_KIMI_CODE_TRUST_WORKSPACE`` override as the only
unattended path — far coarser than "this one repository is trusted".

It is therefore **snapshot-copied**, not shared and not symlinked: the worker
inherits the operator's existing decisions, and any record Kimi writes during
the worker's life lands only in the disposable runtime home. The copy is
treated as opaque, security-sensitive Kimi state — CAO never parses or
regenerates the record schema, never follows a symlinked trust store, and never
retains an internal symlink that could reach back out of the snapshot. See
:meth:`KimiCodeRuntimeHomeBuilder._snapshot_workspace_trust`.

Preserving an existing decision is not the same as making a new one: a folder
the operator has *not* trusted still produces a dialog, and the provider still
fails closed without the explicit opt-in.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config

# Portable Agent Plugins `type` -> FastMCP `transport`. Kimi pins fastmcp and
# hands each MCP document to `fastmcp.mcp_config.MCPConfig`; `RemoteMCPServer`
# has no `type` field, so a portable `type` is an ignored extra and `transport`
# decides the protocol. With `transport` absent Kimi infers it from the URL
# *path* (`sse` only when the path matches `/sse(/|?|&|$)`, `http` otherwise), so
# an SSE server published at `/events` would start as Streamable HTTP.
#
# Both launch paths translate through this one mapping: the legacy builder writes
# it into `--mcp-config`, and `merge_mcp_servers` writes it into the Kimi Code
# runtime `mcp.json`. Only these spellings translate. An absent or unrecognised
# `type` is left alone — `_map_entry` always emits `type` for a plugin server, so
# a type-less entry came from a hand-written profile, where inventing a
# `transport` would be a behaviour change beyond this finding.
KIMI_TRANSPORTS = {
    "stdio": "stdio",
    "streamable-http": "http",
    "http": "http",
    "sse": "sse",
}

logger = logging.getLogger(__name__)

#: Name of the runtime home created inside the provider's temp directory.
RUNTIME_HOME_DIR_NAME = "kimi-home"

#: Default source home when neither ``$KIMI_CODE_HOME`` nor a captured value is
#: available. Kimi Code's own ``resolveKimiHome`` falls back to
#: ``join(homedir(), ".kimi-code")``.
DEFAULT_SOURCE_HOME_DIR_NAME = ".kimi-code"

#: Regular files at the home root that carry user semantics and are copied when
#: present. ``mcp.json`` is copied and then merged (see ``merge_mcp_servers``).
PRESERVE_FILES: Sequence[str] = ("config.toml", "tui.toml", "AGENTS.md", "mcp.json")

#: Directories that carry user semantics and are copied when present.
PRESERVE_DIRS: Sequence[str] = ("skills", "plugins", "credentials")

#: Kimi Code's workspace-trust store (A4). Snapshot-copied into every worker's
#: runtime home so a decision the operator already made in normal Kimi is
#: *recognised* rather than re-asked.
#:
#: Deliberately **not** part of :data:`PRESERVE_DIRS`. That list is copied by
#: ``_copy_tree``, which preserves symlinks verbatim (``symlinks=True``) — the
#: right call for ``skills/``, where a user may legitimately link a shared
#: skills directory, but the wrong one here: a symlink inside a trust snapshot
#: is a path back out of the snapshot, into state the worker does not own. Trust
#: state gets its own fail-closed copy routine instead.
#:
#: The record schema is undocumented and Kimi-owned. It is copied as opaque
#: bytes; CAO must never parse it, key it, or regenerate it. Only Kimi decides
#: what "trusted" means.
TRUST_DIR_NAME = "workspace-trust"

#: Defensive bound on the trust snapshot. Kimi's store is a flat set of small
#: record files, so this is far above any legitimate size; it exists so a
#: pathological or hostile source tree cannot turn the snapshot into an
#: unbounded walk.
#:
#: **A4.1 — the bound counts every traversal entry, not just copied records.**
#: It previously counted only the regular files it had copied, so a tree of
#: empty directories was walked and materialised without limit: 4160 empty
#: directories produced 4160 runtime directories, zero records and zero skips,
#: and the "cannot be unbounded" claim was simply false. Directories, regular
#: files, symlink entries and every other filesystem entry each consume one unit
#: of this budget.
#:
#: **P3 — the bound covers enumeration too.** The traversal previously read each
#: source directory in full (``sorted(scan)``) and only then applied the budget,
#: so a single 10,000-entry directory was fully enumerated and allocated even
#: though only 4,096 records were copied. Only a budget-sized prefix of a
#: directory is read now, so the one bound limits directory enumeration,
#: allocation, copying and recursive descent together.
#:
#: Reaching the limit truncates rather than fails: traversal is pruned, no
#: further entry is copied, the disposition is recorded
#: (``RuntimeHomeResult.trust_truncated``) and a warning is emitted. The launch
#: must never be aborted by a pathological store.
MAX_TRUST_ENTRIES = 4096

#: Directories that are *referenced* rather than copied. ``bin/`` holds Kimi's
#: self-managed 175 MB binary; it is neither credential-bearing nor a state
#: store, and every CAO-managed Kimi process runs with auto-update disabled, so
#: no worker writes through the link. Copying it once per worker would cost
#: ~175 MB each for no isolation benefit. Tested explicitly (see the module
#: tests): the link is created, the source is never written, and a source home
#: without ``bin/`` builds fine.
#:
#: **Known, accepted risk (A3-7).** These entries stay links, so they are the
#: one place where the runtime home can still *reach back* into the real home.
#: Two consequences are accepted rather than fixed, and are recorded here so the
#: trade-off is a decision and not an oversight:
#:
#: * a Kimi process that ignores ``KIMI_CODE_NO_AUTO_UPDATE`` could write
#:   through the link into the shared 175 MB binary;
#: * when ``bin/`` is itself a symlink, ``Path.is_dir()`` follows it and the
#:   runtime home gets a link-to-a-link. Resolution still lands on the real
#:   directory (one extra hop), so behaviour is unchanged — but the runtime
#:   home's ``bin`` is not a stable path, and a caller that inspects
#:   ``os.readlink`` sees the intermediate link rather than the final target.
LINK_DIRS: Sequence[str] = ("bin",)

#: Files whose *contents* are secrets. Always written 0600.
SECRET_FILE_NAMES: Sequence[str] = ("config.toml", "mcp.json")
SECRET_DIR_NAMES: Sequence[str] = ("credentials",)

#: Runtime-generated state that must stay isolated. Listed for documentation and
#: for tests that assert it is absent from a built home; the builder never looks
#: at these at all because the copy set is an allowlist.
#:
#: ``workspace-trust`` is deliberately **absent** from this list as of A4: it is
#: a snapshot-copied security input rather than per-worker runtime state. See
#: :data:`TRUST_DIR_NAME`. Every other entry here remains unconditionally
#: forbidden — sharing ``sessions`` would let two workers collide in one
#: conversation index, and the rest are device-local or cache state with no
#: meaning in a disposable home.
NEVER_COPY: Sequence[str] = (
    "sessions",
    "session_index.jsonl",
    "logs",
    "user-history",
    "updates",
    "cache",
    "telemetry",
    "workspaces.json",
    "device_id",
    "region",
)

#: Kimi Code rejects a timeout outside this range (``McpTimeoutMsSchema``).
MIN_MCP_TIMEOUT_MS = 1
MAX_MCP_TIMEOUT_MS = 2147483647

_KEBAB_RE = re.compile(r"[^a-z0-9]+")


class RuntimeHomeError(RuntimeError):
    """Raised when the runtime home cannot be built safely."""


@dataclass
class RuntimeHomeResult:
    """What ``build()`` produced, for diagnostics and tests."""

    home: Path
    source_home: Path
    copied_files: List[str] = field(default_factory=list)
    copied_dirs: List[str] = field(default_factory=list)
    linked_dirs: List[str] = field(default_factory=list)
    mcp_server_names: List[str] = field(default_factory=list)
    profile_overrode: List[str] = field(default_factory=list)
    #: Preserved directories whose source entry was a symlink. The entry is
    #: materialised (its resolved target's contents are copied, the link itself
    #: is never reproduced), and the resolved target is recorded here so the
    #: decision is visible rather than incidental.
    symlinked_dirs: Dict[str, str] = field(default_factory=dict)
    #: Preserved entries that were deliberately not materialised. See
    #: ``_resolve_preserve_dir`` for the two reasons an entry is skipped.
    skipped_dirs: List[str] = field(default_factory=list)
    #: A4 — the workspace-trust snapshot. ``trust_records`` holds the relative
    #: paths of the opaque Kimi trust records copied into the runtime home;
    #: ``trust_skipped`` holds entries that were deliberately not copied
    #: (symlinks, non-regular files, and any entry past
    #: :data:`MAX_TRUST_ENTRIES`).
    trust_records: List[str] = field(default_factory=list)
    trust_skipped: List[str] = field(default_factory=list)
    #: A4.1 — True when the traversal budget ran out and the snapshot is a
    #: partial prefix of the source store. The launch is unaffected; the worker
    #: simply inherits fewer records than the source holds.
    trust_truncated: bool = False
    #: How the trust snapshot was decided. One of:
    #:
    #: * ``"copied"`` — a real source directory was snapshotted;
    #: * ``"absent"`` — the source has no trust store; nothing to inherit, and
    #:   none is synthesised;
    #: * ``"skipped-symlink"`` — the source store is itself a symlink, so
    #:   inheritance fails closed rather than following it;
    #: * ``"skipped-not-a-directory"`` — the name exists but is not a directory.
    trust_source_state: str = "absent"

    @property
    def mcp_path(self) -> Path:
        return self.home / "mcp.json"


def _is_unbounded_target(target: Path, source_home: Path) -> bool:
    """True when copying ``target`` would be unbounded or self-referential.

    A preserved entry is copied *by content*, so following a top-level symlink
    to the filesystem root or to any ancestor of the source home would walk the
    whole filesystem — or pull the source home into itself. Those two shapes are
    refused; everything else is a legitimate "my skills live elsewhere" setup.
    """

    resolved_source = source_home.resolve()
    if target == resolved_source:
        return True
    # `parents` runs up to and including the filesystem root, so this single
    # membership test covers both the ancestor case and the root case.
    return target in resolved_source.parents


def kimi_agent_name(terminal_id: str) -> str:
    """Return a kebab-case agent ``name`` for a terminal id.

    Kimi Code's agent-file frontmatter requires a non-empty kebab-case ``name``;
    terminal ids are neither. Slugify, then guarantee non-empty.
    """

    slug = _KEBAB_RE.sub("-", (terminal_id or "").lower()).strip("-")
    if not slug:
        slug = "terminal"
    return f"cao-kimi-{slug[:48]}"


def resolve_source_home(captured: Optional[str], fallback: Optional[Path] = None) -> Path:
    """Resolve the effective source ``KIMI_CODE_HOME``.

    ``captured`` must be the value the *launch shell* reported, not the
    cao-server process environment: the two can differ, and only the launch
    shell's answer is guaranteed to match the Kimi process being started.

    Returns an absolute, ``~``-expanded path. A relative or empty value falls
    back to ``<home>/.kimi-code`` with a warning — never silently to the CWD,
    which would make the "source home" depend on where cao-server was started.
    """

    home = fallback or Path.home()
    candidate = (captured or "").strip()
    if candidate:
        expanded = Path(os.path.expanduser(candidate))
        if expanded.is_absolute():
            return expanded
        logger.warning(
            "KIMI_CODE_HOME %r is not absolute; falling back to %s",
            candidate,
            home / DEFAULT_SOURCE_HOME_DIR_NAME,
        )
    return home / DEFAULT_SOURCE_HOME_DIR_NAME


def read_user_mcp_servers(mcp_path: Path) -> Dict[str, Any]:
    """Read the ``mcpServers`` object from a user-level ``mcp.json``.

    Kimi Code's file shape is ``{"mcpServers": {...}}`` (``parseMcpJsonServers``).
    A missing, empty, or unparseable file contributes ``{}`` with a warning: an
    unreadable user file must degrade to "no user servers", never abort a
    terminal launch.
    """

    try:
        if not mcp_path.is_file():
            return {}
        text = mcp_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read user MCP config %s: %s", mcp_path, exc)
        return {}

    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        logger.warning("Ignoring invalid JSON in user MCP config %s: %s", mcp_path, exc)
        return {}

    if not isinstance(data, dict):
        logger.warning("Ignoring user MCP config %s: top level is not an object", mcp_path)
        return {}
    servers = data.get("mcpServers")
    if servers is None:
        return {}
    if not isinstance(servers, dict):
        logger.warning("Ignoring user MCP config %s: 'mcpServers' is not an object", mcp_path)
        return {}
    return {str(name): config for name, config in servers.items()}


def _normalise_profile_server(server: Any) -> Dict[str, Any]:
    """Coerce a profile MCP entry into a plain dict."""

    if isinstance(server, dict):
        return dict(server)
    if hasattr(server, "model_dump"):
        return dict(server.model_dump(exclude_none=True))
    raise RuntimeHomeError(f"Unsupported MCP server configuration: {type(server).__name__}")


def _clamp_timeout(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(MIN_MCP_TIMEOUT_MS, min(MAX_MCP_TIMEOUT_MS, number))


def merge_mcp_servers(
    user_servers: Mapping[str, Any],
    profile_servers: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Merge user-level and CAO-profile MCP servers into one ``mcpServers`` map.

    Frozen precedence: **a profile server overrides a user-level server of the
    same name.** The CAO profile is the explicitly selected, launch-specific
    configuration; the user file is ambient. The rule is total and
    deterministic, so the same profile always produces the same file.

    Every profile entry is passed through the existing
    :func:`resolve_mcp_server_config`, so a bundled ``cao-mcp-server`` becomes a
    PATH-independent invocation exactly as it does on the legacy path.

    ``CAO_TERMINAL_ID`` is deliberately **not** injected here. Kimi Code's stdio
    MCP children inherit the parent environment, so the launch line exports it
    once for every server. An explicit ``env`` block on a profile server is
    preserved verbatim, including an explicit ``CAO_TERMINAL_ID`` — an author who
    pins one means it.
    """

    merged: Dict[str, Any] = {str(name): config for name, config in user_servers.items()}

    for name, raw in (profile_servers or {}).items():
        config = _normalise_profile_server(raw)
        if "command" in config:
            config = resolve_mcp_server_config(config)
        # Select the declared protocol explicitly rather than letting Kimi infer
        # it from the URL path (see `KIMI_TRANSPORTS`). Applied on both launch
        # paths; without it this one emitted `type: sse` and Kimi started the
        # server as Streamable HTTP.
        declared = config.get("type")
        translated = KIMI_TRANSPORTS.get(declared) if isinstance(declared, str) else None
        if translated is not None:
            config["transport"] = translated
            del config["type"]
        # CAO's single `timeout` knob maps onto Kimi's per-server pair, which
        # override the launch-scoped KIMI_MCP_*_TIMEOUT_MS defaults. `timeout`
        # itself is dropped: it is not part of Kimi's server schema.
        timeout = _clamp_timeout(config.pop("timeout", None))
        if timeout is not None:
            config.setdefault("startupTimeoutMs", timeout)
            config.setdefault("toolTimeoutMs", timeout)
        merged[str(name)] = config

    return merged


class KimiCodeRuntimeHomeBuilder:
    """Build and own one worker's runtime ``KIMI_CODE_HOME``.

    The runtime home lives at ``<provider_temp>/kimi-home`` and is removed with
    the provider's temp directory. Its lifetime is intentionally the provider's:
    an ephemeral home guarantees no cross-terminal state bleed.

    That ephemerality is why the trust store is *snapshotted* rather than merely
    left behind (A4). A disposable home starts with no trust records, so
    discarding the operator's store re-asked a question they had already
    answered — for every terminal. Carrying the store forward means a folder the
    operator trusted in normal Kimi is not re-prompted, while a folder they never
    trusted still is, and the provider still fails closed on it.
    """

    def __init__(self, source_home: Path, provider_temp: Path) -> None:
        self._source = Path(source_home)
        self._temp = Path(provider_temp)
        self._home = self._temp / RUNTIME_HOME_DIR_NAME
        self._result: Optional[RuntimeHomeResult] = None

    # -- introspection ----------------------------------------------------

    @property
    def source_home(self) -> Path:
        return self._source

    @property
    def home(self) -> Path:
        return self._home

    @property
    def built(self) -> bool:
        return self._result is not None

    # -- build ------------------------------------------------------------

    def build(self, profile_mcp_servers: Optional[Mapping[str, Any]] = None) -> RuntimeHomeResult:
        """Materialise the runtime home. Idempotent for a given builder."""

        if self._result is not None:
            return self._result

        self._secure_dir(self._temp)
        self._secure_dir(self._home)

        copied_files: List[str] = []
        copied_dirs: List[str] = []
        linked_dirs: List[str] = []
        symlinked_dirs: Dict[str, str] = {}
        skipped_dirs: List[str] = []

        for name in PRESERVE_FILES:
            src = self._source / name
            if src.is_file():
                self._copy_file(src, self._home / name, secret=name in SECRET_FILE_NAMES)
                copied_files.append(name)

        for name in PRESERVE_DIRS:
            src = self._source / name
            resolved, was_symlink = self._resolve_preserve_dir(src)
            if resolved is None:
                # ``was_symlink`` is True exactly for the two deliberate
                # refusals; a plain absent entry is not a "skip".
                if was_symlink:
                    skipped_dirs.append(name)
                continue
            self._copy_tree(resolved, self._home / name, secret=name in SECRET_DIR_NAMES)
            copied_dirs.append(name)
            if was_symlink:
                symlinked_dirs[name] = str(resolved)

        # A4 — snapshot the operator's existing trust decisions. This runs after
        # the preserve loop so a future Kimi release that moves the store into a
        # preserved directory cannot silently shadow this explicit handling.
        trust_records, trust_skipped, trust_state, trust_truncated = (
            self._snapshot_workspace_trust()
        )

        for name in LINK_DIRS:
            src = self._source / name
            if src.is_dir() and not (self._home / name).exists():
                try:
                    os.symlink(src, self._home / name, target_is_directory=True)
                    linked_dirs.append(name)
                except OSError as exc:  # pragma: no cover - platform dependent
                    logger.warning("Could not link %s into runtime home: %s", src, exc)

        user_servers = read_user_mcp_servers(self._home / "mcp.json")
        profile_names = {str(n) for n in (profile_mcp_servers or {})}
        merged = merge_mcp_servers(user_servers, profile_mcp_servers)
        self._write_mcp_json(self._home / "mcp.json", merged)

        result = RuntimeHomeResult(
            home=self._home,
            source_home=self._source,
            copied_files=copied_files,
            copied_dirs=copied_dirs,
            linked_dirs=linked_dirs,
            mcp_server_names=sorted(merged),
            profile_overrode=sorted(profile_names & set(user_servers)),
            symlinked_dirs=symlinked_dirs,
            skipped_dirs=skipped_dirs,
            trust_records=trust_records,
            trust_skipped=trust_skipped,
            trust_truncated=trust_truncated,
            trust_source_state=trust_state,
        )
        self._result = result
        logger.info(
            "kimi_runtime_home_built home=%s source=%s servers=%s profile_overrides=%s "
            "trust=%s records=%d truncated=%s",
            self._home,
            self._source,
            result.mcp_server_names,
            result.profile_overrode,
            trust_state,
            len(trust_records),
            trust_truncated,
        )
        return result

    # -- cleanup ----------------------------------------------------------

    def cleanup(self) -> bool:
        """Remove the runtime home. Returns True when nothing is left behind.

        The recursive delete is restricted to exactly
        ``<provider_temp>/kimi-home`` where ``<provider_temp>`` is this
        instance's own directory, so a recreated provider cannot be turned into
        a general recursive-delete primitive.
        """

        if self._home.name != RUNTIME_HOME_DIR_NAME:
            return False
        if self._home.parent != self._temp:
            return False
        if not self._temp.is_dir():
            return False
        try:
            if self._home.is_symlink():
                self._home.unlink()
            elif self._home.exists():
                shutil.rmtree(self._home)
        except OSError as exc:
            logger.warning("Could not remove runtime home %s: %s", self._home, exc)
            return False
        self._result = None
        return not self._home.exists()

    # -- filesystem helpers ----------------------------------------------

    def _resolve_preserve_dir(self, src: Path) -> Tuple[Optional[Path], bool]:
        """Decide what to copy for a preserved directory entry (A3-7).

        ``Path.is_dir()`` **follows** symlinks, so a top-level link was
        previously traversed by accident: whatever it pointed at got copied, with
        no bound at all. A link to ``/`` or to an ancestor of the source home
        would have turned the runtime home into an unbounded filesystem walk.

        The intended behaviour is explicit rather than incidental:

        * a **real directory** is copied as before;
        * a **top-level symlink** is *materialised* — its resolved target's
          contents are copied and the link itself is never reproduced, so
          nothing can be written back through it into the real location. This
          holds for the credential-bearing entry too, which is written 0600 like
          a real ``credentials/`` directory;
        * a link whose target is not a directory, or whose target would make the
          copy unbounded (:func:`_is_unbounded_target`), is **refused**: the
          entry is skipped, logged, and recorded in ``skipped_dirs``. The
          runtime home still builds, so a pathological link degrades the worker
          rather than preventing it from launching.

        Symlinks *inside* a preserved tree are handled by ``_copy_tree``, which
        copies them as links and never walks through them.

        Returns ``(directory_to_copy, was_symlink)``; ``(None, _)`` means skip.
        """

        if src.is_symlink():
            target = src.resolve()
            if not target.is_dir():
                logger.warning(
                    "kimi_runtime_home_skip name=%s reason=not-a-directory target=%s", src, target
                )
                return None, True
            if _is_unbounded_target(target, self._source):
                logger.warning(
                    "kimi_runtime_home_skip name=%s reason=unbounded-target target=%s", src, target
                )
                return None, True
            logger.info("kimi_runtime_home_symlink_materialised name=%s target=%s", src, target)
            return target, True
        if src.is_dir():
            return src, False
        return None, False

    # -- workspace trust (A4) ---------------------------------------------

    def _snapshot_workspace_trust(self) -> Tuple[List[str], List[str], str, bool]:
        """Snapshot the source trust store into the runtime home (A4).

        Kimi raises its trust dialog for any cwd whose record is absent from the
        home it is running against. Discarding the operator's real store on
        every launch therefore forced a fresh decision per worker, and the only
        unattended answer was the server-wide opt-in. Copying the store forward
        lets Kimi answer the question itself: an already-trusted folder produces
        no dialog, and an untrusted one still produces one.

        The three dispositions, all fail-closed:

        * **real directory** — snapshotted recursively as opaque bytes; the
          runtime copy is a real directory of real files, never a link, so
          Kimi's own writes during the worker's life cannot reach the source;
        * **top-level symlink** — *refused*. Following it would adopt whatever
          trust store it points at, which may not be the user's, so the worker
          inherits nothing and falls through to the normal A3 trust policy;
        * **not a directory** — refused the same way.

        Nothing is synthesised: a source home with no trust store yields no
        trust directory, and the worker behaves exactly as it did before A4.

        Returns ``(records, skipped, state, truncated)``; ``state`` is one of
        ``"copied"``, ``"absent"``, ``"skipped-symlink"`` or
        ``"skipped-not-a-directory"``, and ``truncated`` reports whether the
        traversal budget ran out.
        """

        src = self._source / TRUST_DIR_NAME

        if src.is_symlink():
            logger.warning(
                "kimi_runtime_home_trust_skip reason=top-level-symlink source=%s target=%s",
                src,
                self._readlink(src),
            )
            return [], [], "skipped-symlink", False

        if not src.exists():
            return [], [], "absent", False

        if not src.is_dir():
            logger.warning("kimi_runtime_home_trust_skip reason=not-a-directory source=%s", src)
            return [], [], "skipped-not-a-directory", False

        records, skipped, truncated = self._copy_trust_tree(src, self._home / TRUST_DIR_NAME)
        logger.info(
            "kimi_runtime_home_trust_snapshot source=%s records=%d skipped=%d truncated=%s",
            src,
            len(records),
            len(skipped),
            truncated,
        )
        return records, skipped, "copied", truncated

    @staticmethod
    def _entry_kind(entry: os.DirEntry) -> str:
        """Classify a directory entry **without following symlinks**.

        A4.1: the copy must not learn that an entry is a FIFO by handing it to
        ``shutil.copyfile`` and catching ``SpecialFileError`` — by then the
        launch has already failed. Classifying first means non-regular entries
        are an expected, skipped case rather than an exception.

        The mode is read with ``follow_symlinks=False`` (``lstat``), so a link
        is reported as ``"symlink"`` and never as whatever it points at.
        """

        try:
            mode = entry.stat(follow_symlinks=False).st_mode
        except OSError:
            return "unreadable"
        if stat.S_ISLNK(mode):
            return "symlink"
        if stat.S_ISDIR(mode):
            return "dir"
        if stat.S_ISREG(mode):
            return "file"
        if stat.S_ISFIFO(mode):
            return "fifo"
        if stat.S_ISSOCK(mode):
            return "socket"
        if stat.S_ISBLK(mode):
            return "block-device"
        if stat.S_ISCHR(mode):
            return "char-device"
        return "other"

    @classmethod
    def _copy_trust_tree(cls, src: Path, dst: Path) -> Tuple[List[str], List[str], bool]:
        """Copy a trust store as opaque bytes under a bounded traversal.

        ``_copy_tree`` cannot be reused here: it passes ``symlinks=True``, which
        is correct for ``skills/`` (a user may link a shared directory) but
        wrong for a trust snapshot, where a retained link is a path back out of
        the snapshot into state the worker does not own. Internal symlinks —
        file or directory — are therefore skipped outright with a warning
        rather than materialised, which is the conservative reading of "prove
        the result stays bounded and immutable".

        **The budget counts every traversal entry, not just copied records**
        (A4.1). Counting only regular files left the bound trivially bypassable:
        a tree of empty directories was walked and materialised without limit.
        Directories, regular files, symlink entries and every other filesystem
        entry each consume one unit of :data:`MAX_TRUST_ENTRIES`.

        **The budget also governs enumeration** (P3). ``sorted(scan)`` read a
        directory *in full* before the budget applied, so a single directory of
        10,000 entries consumed all 10,000 scandir entries and allocated 10,000
        ``DirEntry`` objects while copying only 4,096. Only a budget-sized prefix
        is now read, so enumeration, allocation, copying and recursive descent
        are bounded together. Global sort order is deliberately **not**
        preserved: it is exactly what defeated the bound. Within the bounded
        prefix entries are still sorted per directory, so a store under budget
        is processed in the same deterministic order as before.

        Only ordinary regular files are copied. Everything else — FIFO, socket,
        block device, character device, symlink, unreadable entry — is skipped
        with a warning and recorded, so a non-regular entry degrades one entry
        instead of aborting the launch.

        Exhausting the budget truncates: traversal is pruned, no further entry
        is copied, and ``truncated=True`` is returned. The snapshot is then a
        partial prefix of the source store, which is the intended degradation —
        the worker inherits fewer records, never a broken launch.

        The destination is never descended into through a link: directories are
        created by the copy itself, at 0700, and records are written as real
        files at 0600. The source is only ever read.

        Residual, accepted race: the kind is read with ``lstat`` and the copy
        then re-opens by path, so a source entry that *becomes* a symlink
        between the two would be read through. That requires write access to the
        operator's own trust store, and the worst case is reading a file into the
        disposable home — never a write back into the source. Left unguarded
        deliberately: hardening it would mean hand-rolling the copy with
        ``O_NOFOLLOW``, which is more machinery than this failure mode warrants.

        Returns ``(records, skipped, truncated)``.
        """

        records: List[str] = []
        skipped: List[str] = []
        cls._secure_dir(dst)

        budget = MAX_TRUST_ENTRIES
        truncated = False
        # (source directory, path relative to the trust root)
        pending: List[Tuple[Path, Path]] = [(src, Path())]

        while pending and budget > 0:
            root_path, rel = pending.pop(0)
            more_entries = False
            try:
                with os.scandir(root_path) as scan:
                    # Bound the *pull*, not just the copy. ``sorted(scan)``
                    # materialised the entire directory before the budget was
                    # applied, so enumeration and allocation were unbounded even
                    # though the record count was not. Read at most the
                    # remaining budget; at most one further entry is pulled,
                    # only to learn that work remains.
                    entries: List[os.DirEntry[str]] = []
                    for entry in scan:
                        if len(entries) >= budget:
                            more_entries = True
                            break
                        entries.append(entry)
            except OSError as exc:
                skipped.append(cls._rel_entry(rel, root_path.name))
                logger.warning(
                    "kimi_runtime_home_trust_skip reason=unreadable entry=%s err=%s",
                    root_path,
                    exc,
                )
                continue

            # Sort only the bounded prefix. Global sorting is deliberately not
            # preserved: it is exactly what defeated the resource bound.
            entries.sort(key=lambda e: e.name)

            for entry in entries:
                entry_rel = cls._rel_entry(rel, entry.name)
                kind = cls._entry_kind(entry)
                # Every entry consumes budget, whatever it turns out to be.
                budget -= 1

                if kind == "dir":
                    cls._secure_dir(dst / entry_rel)
                    pending.append((Path(entry.path), Path(entry_rel)))
                elif kind == "file":
                    # A record can be identified as a regular file and still be
                    # unreadable, and a source entry can vanish between the
                    # enumeration and the copy. Neither may abort the launch:
                    # the documented contract is that such an entry degrades to
                    # "this one record was not inherited", not "no Kimi Code
                    # worker can start". Any partially written destination is
                    # removed so the runtime home never holds a truncated record.
                    try:
                        shutil.copyfile(entry.path, dst / entry_rel)
                    except OSError as exc:
                        try:
                            (dst / entry_rel).unlink()
                        except OSError:  # pragma: no cover - nothing to remove
                            pass
                        skipped.append(entry_rel)
                        logger.warning(
                            "kimi_runtime_home_trust_skip reason=unreadable " "entry=%s err=%s",
                            entry.path,
                            exc,
                        )
                        continue
                    os.chmod(dst / entry_rel, 0o600)
                    records.append(entry_rel)
                else:
                    # symlink / fifo / socket / device / unreadable / other
                    skipped.append(entry_rel)
                    logger.warning(
                        "kimi_runtime_home_trust_skip reason=%s entry=%s", kind, entry.path
                    )

            # The budget is exhausted and there is still work — unread entries
            # in this directory or pending subdirectories. Both are "truncated";
            # neither is enumerated further.
            if budget <= 0 and (pending or more_entries):
                truncated = True
                break

        if truncated:
            logger.warning(
                "kimi_runtime_home_trust_truncated limit=%d source=%s records=%d "
                "— the worker inherits a partial trust store",
                MAX_TRUST_ENTRIES,
                src,
                len(records),
            )

        return records, skipped, truncated

    @staticmethod
    def _rel_entry(rel: Path, name: str) -> str:
        """A stable, relative label for a trust entry (for diagnostics)."""

        return name if str(rel) == "." else str(rel / name)

    @staticmethod
    def _readlink(path: Path) -> str:
        """Best-effort ``readlink`` for a warning message; never raises."""

        try:
            return os.readlink(path)
        except OSError:  # pragma: no cover - defensive
            return "<unreadable>"

    @staticmethod
    def _secure_dir(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(path, 0o700)
        except OSError as exc:  # pragma: no cover - platform dependent
            raise RuntimeHomeError(
                f"Could not secure runtime home directory {path}: {exc}"
            ) from exc

    @staticmethod
    def _copy_file(src: Path, dst: Path, *, secret: bool) -> None:
        """Copy a regular file, never widening its permissions.

        A symlinked source is followed for *reading* only, so the runtime home
        owns a real copy and nothing can be written back through the link.

        ``secret`` files are forced to 0600. Everything else keeps the owner's
        own bits (``mode & 0o700``) so an executable under ``skills/`` stays
        executable while group/other access is dropped.
        """

        try:
            source_mode = stat.S_IMODE(os.stat(src).st_mode)
        except OSError:
            source_mode = 0o600
        dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(src, dst, follow_symlinks=True)
        os.chmod(dst, 0o600 if secret else (source_mode & 0o700) or 0o600)

    @classmethod
    def _copy_tree(cls, src: Path, dst: Path, *, secret: bool) -> None:
        """Copy a preserved directory tree, choosing the symlink policy by kind.

        For ordinary preserved trees (``skills/``, ``plugins/``) an internal
        symlink is *user semantics*: a user may legitimately link a shared skills
        directory into their home, so links are reproduced verbatim
        (``symlinks=True``) and never walked through.

        For a **secret** tree (``credentials/``, see :data:`SECRET_DIR_NAMES`)
        that policy is wrong. A reproduced link is a *writable path from the
        disposable runtime home back into shared or source state*: a Kimi write
        through the runtime copy would mutate the operator's real credential
        file. Secret trees therefore use :meth:`_copy_secret_tree`, which
        materialises ordinary regular files as real 0600 files and *skips* every
        symlink instead of reproducing it. That is the same conservative policy
        :meth:`_copy_trust_tree` already applies to trust state, for the same
        reason.
        """

        if secret:
            cls._copy_secret_tree(src, dst)
            return

        shutil.copytree(
            src,
            dst,
            symlinks=True,
            ignore_dangling_symlinks=True,
            dirs_exist_ok=True,
        )
        for root, dirnames, filenames in os.walk(dst, followlinks=False):
            root_path = Path(root)
            try:
                os.chmod(root_path, 0o700)
            except OSError:  # pragma: no cover - platform dependent
                pass
            for dirname in dirnames:
                child = root_path / dirname
                if child.is_symlink():
                    continue
                try:
                    os.chmod(child, 0o700)
                except OSError:  # pragma: no cover
                    pass
            for filename in filenames:
                child = root_path / filename
                if child.is_symlink():
                    continue
                try:
                    source_mode = stat.S_IMODE(os.stat(child).st_mode)
                except OSError:
                    source_mode = 0o600
                try:
                    os.chmod(child, 0o600 if secret else (source_mode & 0o700) or 0o600)
                except OSError:  # pragma: no cover
                    pass

    @classmethod
    def _copy_secret_tree(cls, src: Path, dst: Path) -> None:
        """Copy a secret-bearing tree as real files, never as symlinks (P2).

        A secret directory is credential state. Reproducing an internal symlink
        here — the ordinary :meth:`_copy_tree` policy, which is correct for
        ``skills/`` — leaves the runtime home holding *a writable path back out
        of the home*: a Kimi write through the runtime copy mutates whatever the
        link points at, which may be shared state the worker does not own (the
        reproduction linked ``credentials/token.json`` at a file outside the
        source home). Only real files and real directories are safe here, so:

        * ordinary regular files are copied by content and forced to 0600;
        * real directories are recreated 0700 and recursed **only** as real
          directories. ``os.walk(followlinks=False)`` plus an explicit symlink
          filter means a directory link is neither reproduced nor descended, so
          a cyclic or unbounded walk through links is impossible;
        * every symlink (file or directory, relative, absolute or dangling) and
          every non-regular entry is **skipped** with a warning.

        Skipping degrades one credential entry, never the launch: the runtime
        home still builds. Nothing under ``dst`` is ever a symlink, so a write
        through it can only reach a file the runtime home owns. This mirrors the
        conservative policy of :meth:`_copy_trust_tree` for the same reason.
        """

        cls._secure_dir(dst)
        for root, dirnames, filenames in os.walk(src, followlinks=False):
            root_path = Path(root)
            rel = root_path.relative_to(src)
            target_root = dst / rel if str(rel) != "." else dst
            cls._secure_dir(target_root)

            # Reassign in place: ``os.walk`` reads this list to decide descent.
            # A symlinked directory must be neither recreated nor entered.
            kept_dirs: List[str] = []
            for dirname in dirnames:
                child = root_path / dirname
                if child.is_symlink():
                    logger.warning("kimi_runtime_home_secret_skip reason=symlink entry=%s", child)
                    continue
                kept_dirs.append(dirname)
            dirnames[:] = kept_dirs

            for filename in filenames:
                child = root_path / filename
                if child.is_symlink():
                    logger.warning("kimi_runtime_home_secret_skip reason=symlink entry=%s", child)
                    continue
                try:
                    source_mode = os.stat(child).st_mode
                except OSError as exc:
                    logger.warning(
                        "kimi_runtime_home_secret_skip reason=unreadable entry=%s err=%s",
                        child,
                        exc,
                    )
                    continue
                if not stat.S_ISREG(source_mode):
                    logger.warning(
                        "kimi_runtime_home_secret_skip reason=not-a-regular-file entry=%s",
                        child,
                    )
                    continue
                dest = target_root / filename
                try:
                    shutil.copyfile(child, dest, follow_symlinks=False)
                    os.chmod(dest, 0o600)
                except OSError as exc:
                    logger.warning(
                        "kimi_runtime_home_secret_skip reason=copy-failed entry=%s err=%s",
                        child,
                        exc,
                    )

    @staticmethod
    def _write_mcp_json(path: Path, servers: Mapping[str, Any]) -> None:
        """Atomically publish ``{"mcpServers": {...}}`` at 0600."""

        payload = json.dumps({"mcpServers": dict(servers)}, indent=2, sort_keys=True)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_name(path.name + ".tmp")
        try:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, payload.encode("utf-8"))
            finally:
                os.close(fd)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
            os.chmod(path, 0o600)
        except OSError as exc:
            raise RuntimeHomeError(f"Could not write runtime MCP config {path}: {exc}") from exc
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:  # pragma: no cover
                    pass


def iter_forbidden_runtime_state(home: Path) -> Iterable[str]:
    """Yield every ``NEVER_COPY`` name that leaked into ``home``.

    The name is deliberately "forbidden present state", not "missing state":
    a correct build yields **nothing**, and any yielded name is a bug — the
    runtime home must never contain session/log/update/cache state copied from
    the real user home (it is per-worker and disposable, and copying
    ``sessions`` would let two workers share a conversation index).

    ``workspace-trust`` is no longer part of this set (A4). It is a
    snapshot-copied security *input*, not per-worker runtime state, so its
    presence in a built home is expected rather than forbidden — see
    :data:`TRUST_DIR_NAME` and ``RuntimeHomeResult.trust_records`` for the
    disposition that replaced the blanket exclusion.

    Used by tests to prove the built home is free of runtime-generated state.
    """

    for name in NEVER_COPY:
        if (home / name).exists():
            yield name
