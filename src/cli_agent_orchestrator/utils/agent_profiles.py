"""Agent profile utilities."""

import hashlib
import logging
import os
import re
import stat
from dataclasses import dataclass, field
from enum import Enum
from importlib import resources
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Set

import frontmatter

from cli_agent_orchestrator.constants import CAO_HOME_DIR, LOCAL_AGENT_STORE_DIR, PROVIDERS
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.utils.env import resolve_env_vars
from cli_agent_orchestrator.utils.ephemeral_names import routes_to_ephemeral_store
from cli_agent_orchestrator.utils.paths import normalized_path

logger = logging.getLogger(__name__)

EPHEMERAL_LIVE_DIR = CAO_HOME_DIR / "ephemeral" / "live"


class ProfileSource(str, Enum):
    """The store that served a launch profile."""

    INSTALLED = "installed"
    EPHEMERAL = "ephemeral"


class EphemeralProfileUnavailable(ValueError):
    """A reserved name cannot be served by the ephemeral live store."""


def load_launch_profile(name: str) -> tuple[AgentProfile, ProfileSource]:
    """Load a launch profile; reserved names never fall back to installed stores."""
    if not routes_to_ephemeral_store(name):
        return load_agent_profile(name), ProfileSource.INSTALLED
    from cli_agent_orchestrator.clients import database

    try:
        path = _safe_join(EPHEMERAL_LIVE_DIR, f"{name}.md")
        if path is None:
            raise ValueError("live profile path escapes its store")
        # Validate containment, but open the lexical leaf so O_NOFOLLOW refuses
        # even a symlink whose target happens to be inside the live store.
        path = EPHEMERAL_LIVE_DIR / f"{name}.md"
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("ephemeral store file is unsafe")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                payload = stream.read()
            row = database.get_ephemeral_agent(name)
            if row is None or hashlib.sha256(payload).hexdigest() != row.get("profile_sha256"):
                raise ValueError("live profile integrity check failed")
            # Parse exactly the bytes verified above, never a second file read.
            profile = parse_agent_profile_text(payload.decode("utf-8"), name)
        finally:
            os.close(fd)
    except Exception as exc:
        raise EphemeralProfileUnavailable(f"Ephemeral profile unavailable: {name}") from exc
    return profile, ProfileSource.EPHEMERAL


def resolve_agent_profile_source(name: str) -> ProfileSource:
    """Report the store that actually served the file, refusing unavailable names."""
    return load_launch_profile(name)[1]


def warn_reserved_installed_profiles() -> None:
    """Warn once at startup about installed profiles shadowed by the namespace."""
    try:
        for profile in _list_installed_profiles():
            if routes_to_ephemeral_store(profile["name"]):
                logger.warning(
                    "Installed profile '%s' matches the reserved ephemeral namespace and cannot be used",
                    profile["name"],
                )
    except Exception:
        logger.warning("reserved-name startup scan failed", exc_info=True)


def _validate_agent_name(agent_name: str) -> None:
    """Reject agent names that could cause path traversal."""
    if "/" in agent_name or "\\" in agent_name or ".." in agent_name:
        raise ValueError(f"Invalid agent name '{agent_name}': must not contain '/', '\\', or '..'")


def _safe_join(root: Path, *parts: str) -> Path | None:
    """Join ``parts`` under ``root`` and return the path only if it stays inside ``root``.

    Normalises the result with ``resolve()`` and confirms containment via
    ``relative_to(root.resolve())``. Returns ``None`` when the joined path
    would escape the root (e.g., due to an absolute component, traversal
    segments, or a symlink that points outside). Callers should treat a
    ``None`` result as "not found" rather than raising, so lookups across
    multiple configured roots can fall through cleanly.

    This is defence-in-depth alongside ``_validate_agent_name``: the name
    check rejects traversal-style inputs up front, and this helper refuses
    to touch the filesystem if anything slipped through.
    """
    resolved_root = root.resolve()
    candidate = root.joinpath(*parts).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError:
        return None
    return candidate


# Read-time bounds for discovery metadata, mirroring agent_profile.schema.json.
# The schema is only enforced at install/validate time; profiles can reach the
# stores without passing through it (manual copy, git checkout), so the read
# path re-enforces the limits before the values feed search corpora and the
# find_profiles MCP surface. (The full file is read to parse frontmatter, but
# the prompt body is never indexed or returned by discovery.)
_DISCOVERY_MAX_ITEMS = 32
_CAPABILITY_MAX_LEN = 128
_DESCRIPTION_MAX_LEN = 1024
_TAG_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _discovery_fields(metadata: dict) -> Dict:
    """Extract discovery metadata (description/capabilities/tags/role) from frontmatter.

    Non-string descriptions and non-list tag/capability values are coerced to
    empty, and schema limits (item counts, lengths, tag charset) are enforced
    here so downstream search code can rely on bounded, well-shaped values
    even for profiles that never went through ``cao install`` validation.
    """
    desc_raw = metadata.get("description")
    caps_raw = metadata.get("capabilities")
    tags_raw = metadata.get("tags")
    role = metadata.get("role")

    description = desc_raw[:_DESCRIPTION_MAX_LEN] if isinstance(desc_raw, str) else ""
    capabilities = (
        [str(c)[:_CAPABILITY_MAX_LEN] for c in caps_raw[:_DISCOVERY_MAX_ITEMS]]
        if isinstance(caps_raw, list)
        else []
    )
    tags = (
        [str(t) for t in tags_raw[:_DISCOVERY_MAX_ITEMS] if _TAG_PATTERN.fullmatch(str(t))]
        if isinstance(tags_raw, list)
        else []
    )
    return {
        "description": description,
        "capabilities": capabilities,
        "tags": tags,
        "role": str(role) if isinstance(role, str) else "",
    }


def _scan_profile_source(source, profile_name: str) -> "tuple[Dict, bool]":
    """Extract discovery fields and loadability for a scanned profile source.

    ``loadable`` mirrors what ``load_agent_profile()`` will accept: the text
    must read, the frontmatter must parse, and the metadata must validate
    against the ``AgentProfile`` model (with the same name/description
    defaults ``parse_agent_profile_text`` applies). Environment-variable
    resolution is intentionally not applied here: it is runtime-dependent
    and does not affect structural validity.

    ``source`` is anything with ``read_text()`` (a ``Path`` or an
    ``importlib.resources`` traversable).
    """
    try:
        data = frontmatter.loads(source.read_text())
    except Exception:
        return _discovery_fields({}), False
    discovery = _discovery_fields(data.metadata)
    try:
        meta = dict(data.metadata)
        meta["system_prompt"] = data.content.strip()
        meta.setdefault("name", profile_name)
        meta.setdefault("description", "")
        AgentProfile(**meta)
    except Exception:
        return discovery, False
    return discovery, True


def _scan_directory(
    directory: Path,
    source_label: str,
    profiles: Dict[str, Dict],
    name_sources: Dict[str, List[str]] | None = None,
    dir_profiles_loadable: bool = True,
) -> None:
    """Scan a directory for agent profiles (.md files, .json files, or subdirectories).

    ``profiles`` keeps the first-found profile per name (scan order decides the
    winner). ``name_sources``, when given, records each directory a name was
    found in (winner first, once per directory — a dir holding both
    ``<name>.md`` and ``<name>/`` counts once), so callers can surface
    same-named profiles defined in more than one enabled directory (GH #280).

    ``dir_profiles_loadable`` mirrors ``_read_agent_profile_source``'s source
    rules: directory-style profiles (``<name>/agent.md``) are only resolvable
    from provider and extra directories, not from the local store, so the
    local-store scan passes ``False`` to keep such entries listable but never
    recommendable.
    """
    if not directory.exists():
        return
    seen_here: Set[str] = set()

    def _record(profile_name: str) -> None:
        if name_sources is not None and profile_name not in seen_here:
            seen_here.add(profile_name)
            name_sources.setdefault(profile_name, []).append(source_label)

    for item in directory.iterdir():
        if item.is_dir():
            profile_name = item.name
            agent_md = item / "agent.md"
            if agent_md.exists() and dir_profiles_loadable:
                discovery, loadable = _scan_profile_source(agent_md, profile_name)
            elif agent_md.exists():
                # Content may be fine, but _read_agent_profile_source() does
                # not resolve directory-style profiles from this store, so
                # load_agent_profile() would raise FileNotFoundError.
                discovery, _ = _scan_profile_source(agent_md, profile_name)
                loadable = False
            else:
                # A directory without agent.md is listable, but
                # load_agent_profile() raises FileNotFoundError for it, so it
                # must never be recommended by search.
                discovery, loadable = _discovery_fields({}), False
            _record(profile_name)
            if profile_name not in profiles:
                profiles[profile_name] = {
                    "name": profile_name,
                    "source": source_label,
                    "loadable": loadable,
                    **discovery,
                }
        elif item.suffix == ".md" and item.is_file():
            profile_name = item.stem
            discovery, loadable = _scan_profile_source(item, profile_name)
            _record(profile_name)
            if profile_name not in profiles:
                profiles[profile_name] = {
                    "name": profile_name,
                    "source": source_label,
                    "loadable": loadable,
                    **discovery,
                }


def _list_installed_profiles() -> List[Dict]:
    """Discover all available agent profiles from all configured directories.

    Scans built-in store, local store, and all provider agent directories
    (from settings or defaults). Returns deduplicated list sorted by name.
    """
    from cli_agent_orchestrator.services.settings_service import (
        get_disabled_agent_dirs,
        get_extra_agent_dirs,
        usable_agent_dirs,
    )

    profiles: Dict[str, Dict] = {}
    # name -> every enabled directory the name was found in (winner first), used
    # to flag same-named profiles defined in more than one dir (GH #280).
    name_sources: Dict[str, List[str]] = {}
    disabled = {normalized_path(d) for d in get_disabled_agent_dirs()}
    scanned_paths: Set[str] = set()

    # 1. Local agent store (derives from CAO_HOME_DIR, default
    # ~/.aws/cli-agent-orchestrator/agent-store/).
    # It shares a path with the claude_code/codex default, so honour the
    # disable toggle here too — otherwise disabling that default wouldn't hide
    # its profiles.
    local_norm = normalized_path(LOCAL_AGENT_STORE_DIR)
    if local_norm not in disabled:
        _scan_directory(
            LOCAL_AGENT_STORE_DIR,
            "local",
            profiles,
            name_sources,
            dir_profiles_loadable=False,
        )
        scanned_paths.add(local_norm)

    # 2. Provider-specific directories (from settings; blank or relative values
    #    fall back to their defaults so the working directory is never scanned)
    agent_dirs = usable_agent_dirs()
    provider_source_labels = {
        "kiro_cli": "kiro",
        "claude_code": "claude_code",
        "codex": "codex",
        "cao_installed": "installed",
    }
    for provider, dir_path in agent_dirs.items():
        norm = normalized_path(dir_path)
        if norm in disabled or norm in scanned_paths:
            continue
        label = provider_source_labels.get(provider, provider)
        _scan_directory(Path(dir_path), label, profiles, name_sources)
        scanned_paths.add(norm)

    # 3. Extra user-added directories
    for extra_dir in get_extra_agent_dirs():
        norm = normalized_path(extra_dir)
        if norm in disabled or norm in scanned_paths:
            continue
        _scan_directory(Path(extra_dir), "custom", profiles, name_sources)
        scanned_paths.add(norm)

    # 4. Built-in agent store — scanned LAST so on-disk copies win (matches
    # _read_agent_profile_source's lookup order).
    try:
        agent_store = resources.files("cli_agent_orchestrator.agent_store")
        for item in agent_store.iterdir():
            name = item.name
            if name.endswith(".md"):
                profile_name = name[:-3]
                name_sources.setdefault(profile_name, []).append("built-in")
                if profile_name in profiles:
                    continue
                discovery, loadable = _scan_profile_source(item, profile_name)
                profiles[profile_name] = {
                    "name": profile_name,
                    "source": "built-in",
                    "loadable": loadable,
                    **discovery,
                }
    except Exception as e:
        logger.debug(f"Could not scan built-in agent store: {e}")

    # Flag conflicts: a name found in more than one enabled directory. The
    # winner (first scanned) is what loads; ``duplicated_in`` lists the shadowed
    # sources so the UI can show "also defined in …" (GH #280 nice-to-have).
    for profile_name, profile in profiles.items():
        srcs = name_sources.get(profile_name, [])
        profile["duplicated_in"] = srcs[1:] if len(srcs) > 1 else []

    return sorted(profiles.values(), key=lambda p: p["name"])


def list_agent_profiles() -> List[Dict]:
    """List installed profiles, excluding the reserved launch-only namespace."""
    return [p for p in _list_installed_profiles() if not routes_to_ephemeral_store(p["name"])]


PROFILE_SET_DIGEST_PREFIX = "sha256:"

# ``<byte-length>:<bytes>`` per component, mirroring ``plan_identifier._frame``. A profile name cannot
# contain ``:`` (``_validate_agent_name``) but the SOURCE TEXT can contain anything, so a plain
# separator would not be injective and two different profile sets could digest identically — which, for
# the caller that folds this into ``plan_id``, means one profile set executing under another's approval.
_PROFILE_FRAME_SEP = ":"

_UNLOADABLE_SENTINEL = "\x02unloadable-profile-entry"
"""Digested in place of source text for a listed entry the LAUNCH PATH refuses to serve (issue #827).

The lister and the reader disagree about some entries, by design: the lister follows symlinks and
does not check the name, while the reader applies ``_safe_join`` containment and
``_validate_agent_name``. A symlinked ``helper.md`` pointing outside the store, a ``v1..v2.md`` file, a
directory without ``agent.md`` — each is LISTED, and ``load_agent_profile`` raises for each at launch.
Such an entry is not a profile a step can run, so there is no content to bind; but its presence, its
absence and its NAME still change which name resolves to what, so it is digested as name plus this
sentinel rather than skipped.

The verdict "refused" is taken from the reader itself (:func:`read_profile_entry` calls the same
``_read_agent_profile_source`` that ``load_agent_profile`` calls), not from the lister's ``loadable``
flag, so the digest cannot classify an entry differently from the launch path. The previous shape —
trust ``loadable``, fall back to ``installed_profile_exists``, and RAISE for anything else — turned
every entry in the gap between the two rules into a digest-wide exception that NULLed every manifest
and refused every script run as ``internal`` (the P2-1 finding on PR #1000).
"""


_ABSENT_ARTIFACT_SENTINEL = b"\x02absent-installed-artifact"
"""Digested in place of an installed provider artifact that does not exist for a profile name.

Absence is part of the identity: a Kiro agent JSON appearing for a profile that had none changes what
``kiro-cli chat --agent <name>`` does, so "no file" and "a file" must digest differently, and "no file"
must differ from an empty file.
"""


@dataclass(frozen=True)
class ProfileEntry:
    """What the launch path would do with one listed profile name, read exactly once.

    ``source`` is the raw text ``load_agent_profile`` would parse, or ``None`` when the reader refuses
    the entry (not found under any root it may serve from, name rejected, containment failed, not
    UTF-8). ``None`` is a classification, not an error: it is the same answer the launch path gives.

    ``artifacts`` maps each path-free label from ``install_service.installed_artifact_paths`` (a
    provider's per-agent file, the shared OpenCode ``opencode.json``, the installed context copy) to
    that file's bytes, or ``None`` when it does not exist. These are the files a provider READS AT
    LAUNCH that CAO wrote at ``cao install`` time and does not regenerate: for the default provider
    (Kiro) they carry the tool policy — ``tools``, ``allowedTools``, ``hooks``, ``mcpServers`` — and the
    system prompt, so a plan identity that bound only the CAO profile left the default provider's actual
    behaviour outside it (PR #1000, P2-2); for OpenCode the MCP server commands and the agent's tool
    grant live in the shared ``opencode.json`` (PR #1000 round 2, P2-1).
    """

    source: Optional[str]
    artifacts: Mapping[str, Optional[bytes]] = field(default_factory=dict)


def _read_artifact_bytes(path: Path) -> Optional[bytes]:
    """Bytes of an installed artifact, ``None`` when nothing a provider could read is there.

    A missing file, or a directory where a file belongs, is "absent" — the provider cannot read it as its
    agent file either. Any other ``OSError`` (a permission fault) propagates: see
    :func:`read_profile_entry`.
    """
    try:
        return path.read_bytes()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None


def read_profile_entry(name: str) -> ProfileEntry:
    """Read one listed profile the way the launch path reads it, plus the files ``cao install`` left for it.

    Catches exactly the two exceptions ``load_agent_profile`` documents as the reader's own refusals
    (``FileNotFoundError``, ``ValueError`` — which includes ``UnicodeDecodeError``) and turns them into
    ``source=None``. Anything else — ``PermissionError``, an I/O fault, on the profile or on an
    artifact — is an inability to READ the store rather than a verdict about the entry, and propagates
    so the caller can fail closed with an honest reason (``manifest_freeze`` reports
    ``profiles_unreadable``).

    The artifact set comes from ``install_service.installed_artifact_paths`` so that the installer's
    sinks and plan identity are one list; ``test_install_ownership_guard`` pins that list against what a
    real install reports. Imported lazily: ``install_service`` imports this module.
    """
    from cli_agent_orchestrator.services.install_service import installed_artifact_paths

    try:
        source: Optional[str] = _read_agent_profile_source(name)
    except (FileNotFoundError, ValueError):
        source = None
    artifacts = {
        label: _read_artifact_bytes(path) for label, path in installed_artifact_paths(name).items()
    }
    return ProfileEntry(source=source, artifacts=artifacts)


def _frame(digest: "hashlib._Hash", data: bytes) -> None:
    digest.update(f"{len(data)}{_PROFILE_FRAME_SEP}".encode("utf-8"))
    digest.update(data)


def profile_entry_digest(entry: ProfileEntry) -> str:
    """Digest of ONE entry — its source (or the refused sentinel) and every installed artifact (or the
    absent sentinel), each length-framed, artifacts in label order.

    This is the per-profile value the launch guard records and re-checks at every agent launch
    (``launch_guard``), and the per-name component :func:`profile_set_digest` folds into ``plan_id``.
    One function for both, so the guard cannot see a different profile than the identity did.
    """
    digest = hashlib.sha256()
    source = entry.source if entry.source is not None else _UNLOADABLE_SENTINEL
    _frame(digest, source.encode("utf-8"))
    for label in sorted(entry.artifacts):
        content = entry.artifacts[label]
        _frame(digest, label.encode("utf-8"))
        _frame(digest, content if content is not None else _ABSENT_ARTIFACT_SENTINEL)
    return PROFILE_SET_DIGEST_PREFIX + digest.hexdigest()


def read_profile_set() -> Dict[str, ProfileEntry]:
    """One read of the FULL installed profile set: every listed name and the launch path's verdict on it.

    This is the single read both plan identity (:func:`profile_set_digest`) and the per-run launch
    guard build from, so the two cannot describe different snapshots of the store. Raises ``OSError``
    (other than ``FileNotFoundError``) when an entry cannot be read; the entry's NAME is logged — it is
    an operator-chosen filename, not a path — because a retry cannot help and the operator has to find
    the entry to fix it.
    """
    snapshot: Dict[str, ProfileEntry] = {}
    for profile in list_agent_profiles():
        name = profile["name"]
        try:
            snapshot[name] = read_profile_entry(name)
        except OSError:
            logger.warning("agent profile entry cannot be read: name=%r", name)
            raise
    return snapshot


def profile_set_digest(profile_set: Optional[Mapping[str, ProfileEntry]] = None) -> str:
    """Digest the FULL installed agent profile set — every name and its raw source text (issue #827).

    Why the full set rather than the profiles a plan happens to name: script-tier steps are DISCOVERED
    BY EXECUTING THE PYTHON, so at the point an approval is checked there is no inventory of which
    profiles the run will launch. That is the same property that made ``plan_id`` run-level in the first
    place (see ``plan_identifier``'s module docstring). A digest scoped to "the profiles this plan uses"
    would need a list that cannot be produced when it is needed.

    EVERY ENTRY CARRIES ITS INSTALLED ARTIFACTS. The CAO profile is not the only file a step's behaviour
    comes from: ``cao install`` writes the provider's per-agent file (Kiro's JSON holds the tool policy,
    Copilot's ``.agent.md`` holds the prompt) and a context copy, and the provider reads those at launch
    without CAO regenerating them. See :class:`ProfileEntry`.

    THE NAME IS DIGESTED ALONGSIDE THE CONTENT. Content alone would make "profile ``reviewer`` deleted
    and profile ``writer`` added with its text" indistinguishable from a rename, and the profile a step
    names is resolved by name at launch.

    RAW SOURCE, NOT THE PARSED ``AgentProfile``. Parsing normalises away fields a future version may
    start honouring, and an unparseable profile still has to produce a stable identity rather than an
    exception. ``resolve_env_vars`` is deliberately NOT applied: it substitutes ``${VAR}`` placeholders
    from the managed CAO ``.env`` file (``utils/env.py``), which is where credentials live, and a digest
    of the substituted text would put a hash of each credential into an identifier that is logged,
    displayed and stored. The cost is documented in ``docs/workflows.md``: a ``cao env set`` that changes
    a referenced value does not change ``plan_id``.

    AN ENTRY THE LAUNCH PATH REFUSES digests as its name plus a fixed sentinel — see
    :data:`_UNLOADABLE_SENTINEL`. That includes a profile that vanishes between the scan and the read:
    the resulting identity differs from both "present with its text" and "absent", so it can match no
    approval granted for either state, which is the fail-closed property the previous raise bought at
    the price of refusing every run for every persistent stray entry.

    ``profile_set`` is the snapshot :func:`read_profile_set` returned, when the caller already holds
    one; otherwise the set is read here. This does not make the upstream LISTING itself fail-closed —
    ``_list_installed_profiles`` already swallows a built-in-store enumeration failure into a smaller
    valid-looking set (unchanged by this issue), so this digest can be a coherent hash of whatever that
    scan returned without being a guarantee that the scan itself never dropped anything.
    """
    if profile_set is None:
        profile_set = read_profile_set()
    digest = hashlib.sha256()
    for name in sorted(profile_set):
        _frame(digest, name.encode("utf-8"))
        _frame(digest, profile_entry_digest(profile_set[name]).encode("utf-8"))
    return PROFILE_SET_DIGEST_PREFIX + digest.hexdigest()


def parse_agent_profile_text(resolved_text: str, profile_name: str) -> AgentProfile:
    """Parse an AgentProfile from already-resolved markdown text."""
    profile_data = frontmatter.loads(resolved_text)
    meta = profile_data.metadata
    meta["system_prompt"] = profile_data.content.strip()
    # Fill in required fields if missing (Kiro profiles don't have frontmatter)
    if "name" not in meta:
        meta["name"] = profile_name
    if "description" not in meta:
        meta["description"] = ""
    return AgentProfile(**meta)


def _read_agent_profile_source(agent_name: str) -> str:
    """Locate an agent profile across configured stores and return the raw text.

    Search order:
    1. Local store: <CAO_HOME_DIR>/agent-store/{name}.md (default
       ~/.aws/cli-agent-orchestrator/agent-store/)
    2. Provider-specific directories (flat {name}.md or {name}/agent.md)
    3. Extra user-added directories (flat {name}.md or {name}/agent.md)
    4. Built-in store (packaged with CAO)

    Shared by ``load_agent_profile`` (which parses the text into an
    ``AgentProfile``) and the install service (which writes the raw text to
    the context file). Centralising the lookup keeps the two callers in sync.
    """
    _validate_agent_name(agent_name)
    if routes_to_ephemeral_store(agent_name):
        raise FileNotFoundError(f"Reserved ephemeral profile name: {agent_name}")
    return _read_installed_agent_profile_source(agent_name)


def installed_profile_exists(agent_name: str) -> bool:
    """Probe installed stores without reserved-name routing or environment expansion.

    Catches ``ValueError`` as well as ``FileNotFoundError``: a name containing ``..`` makes
    ``_validate_agent_name`` raise, and for a probe that is the same answer as "not installed". An
    ``OSError`` other than not-found (a permission fault, say) is NOT caught: it is an inability to
    read the store, not a verdict about the name, and the caller decides what that means.
    """
    try:
        _read_installed_agent_profile_source(agent_name)
    except (FileNotFoundError, ValueError):
        return False
    return True


def _read_installed_agent_profile_source(agent_name: str) -> str:
    """Raw installed lookup for namespace collision checks and the guarded reader."""
    _validate_agent_name(agent_name)
    from cli_agent_orchestrator.services.settings_service import (
        get_disabled_agent_dirs,
        get_extra_agent_dirs,
        usable_agent_dirs,
    )

    # Honour the disable toggle on the load path too, so disabling a directory
    # actually swaps which same-named profile wins (GH #280), not just what the
    # Settings list shows.
    disabled = {normalized_path(d) for d in get_disabled_agent_dirs()}

    # Every filesystem read below goes through _safe_join so the path is
    # normalised and verified to stay inside its configured root. This is
    # belt-and-braces on top of _validate_agent_name above — the name check
    # rejects obvious traversal inputs, and _safe_join additionally blocks
    # anything that sneaks past (e.g. symlinks resolving outside the root).
    if normalized_path(LOCAL_AGENT_STORE_DIR) not in disabled:
        local_profile = _safe_join(LOCAL_AGENT_STORE_DIR, f"{agent_name}.md")
        if local_profile is not None and local_profile.exists():
            return local_profile.read_text(encoding="utf-8")

    def _lookup_in_directory(directory: Path) -> str | None:
        if not directory.exists():
            return None
        flat = _safe_join(directory, f"{agent_name}.md")
        if flat is not None and flat.exists():
            return flat.read_text(encoding="utf-8")
        nested = _safe_join(directory, agent_name, "agent.md")
        if nested is not None and nested.exists():
            return nested.read_text(encoding="utf-8")
        return None

    for dir_path in usable_agent_dirs().values():
        if normalized_path(dir_path) in disabled:
            continue
        found = _lookup_in_directory(Path(dir_path))
        if found is not None:
            return found

    for extra_dir in get_extra_agent_dirs():
        if normalized_path(extra_dir) in disabled:
            continue
        found = _lookup_in_directory(Path(extra_dir))
        if found is not None:
            return found

    # Built-in store is inside the installed package — the traversable API
    # still concatenates agent_name as a single segment, so validate the
    # result's name before reading.
    agent_store = resources.files("cli_agent_orchestrator.agent_store")
    built_in = agent_store / f"{agent_name}.md"
    if built_in.name == f"{agent_name}.md" and built_in.is_file():
        return built_in.read_text(encoding="utf-8")

    raise FileNotFoundError(f"Agent profile not found: {agent_name}")


def load_agent_profile(agent_name: str) -> AgentProfile:
    """Load an agent profile from the configured stores."""
    try:
        raw_text = _read_agent_profile_source(agent_name)
        return parse_agent_profile_text(resolve_env_vars(raw_text), agent_name)
    except (FileNotFoundError, ValueError):
        raise
    except Exception as e:
        raise RuntimeError(f"Failed to load agent profile '{agent_name}': {e}")


def profile_declares_capability(profile_name: Optional[str], capability: str) -> bool:
    """True when ``profile_name``'s frontmatter declares ``capability``.

    The capability list comes from the profile FILE — an operator-owned
    artifact an agent cannot edit through MCP — so this is usable as an
    authorization input. It lives here rather than in the MCP server because
    the profile store is on the cao-server node: an agent process that does
    not share a filesystem with the server cannot read it, so the check has
    to run server-side.

    Fails closed when the answer is actually known to be "no": a missing
    profile (``FileNotFoundError``) or one that fails to parse (``ValueError``)
    does not declare the capability. A fault reading the profile STORE itself
    — permissions, I/O, anything ``load_agent_profile`` wraps in
    ``RuntimeError`` — is a different thing from "not authorized" and is
    deliberately NOT caught here: swallowing it into ``False`` would turn an
    unknown answer into a confident denial, the exact fault-laundering this
    check exists to avoid. Callers that need a boolean for a non-authorization
    use must catch ``RuntimeError`` themselves.
    """
    if not profile_name:
        return False
    try:
        profile = load_agent_profile(profile_name)
        return capability in (profile.capabilities or [])
    except (FileNotFoundError, ValueError) as e:
        logger.warning(f"capability lookup failed for profile {profile_name!r}: {e}")
        return False


def resolve_provider(agent_profile_name: str, fallback_provider: str) -> str:
    """Resolve the provider to use for an agent profile.

    Loads the agent profile from the CAO agent store and checks for a
    ``provider`` key.  If present and valid, returns the profile's provider.
    Otherwise returns the fallback provider (typically inherited from the
    calling terminal).

    Args:
        agent_profile_name: Name of the agent profile to look up.
        fallback_provider: Provider to use when the profile does not specify
            one or specifies an invalid value.

    Returns:
        Resolved provider type string.
    """
    try:
        profile, _source = load_launch_profile(agent_profile_name)
    except (FileNotFoundError, RuntimeError):
        # Profile not found or failed to load — provider.initialize()
        # will surface a clear error later.  Fall back for now.
        return fallback_provider

    if profile.provider:
        if profile.provider in PROVIDERS:
            return profile.provider
        else:
            logger.warning(
                "Agent profile '%s' has invalid provider '%s'. "
                "Valid providers: %s. Falling back to '%s'.",
                agent_profile_name,
                profile.provider,
                PROVIDERS,
                fallback_provider,
            )

    return fallback_provider
