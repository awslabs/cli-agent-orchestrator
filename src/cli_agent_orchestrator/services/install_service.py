"""Service helpers for installing agent profiles."""

import logging
import os
import platform
import re
import secrets
import stat
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple
from urllib.parse import urlparse

import frontmatter
import requests  # type: ignore[import-untyped]
import yaml
from pydantic import BaseModel

from cli_agent_orchestrator.constants import (
    AGENT_CONTEXT_DIR,
    COPILOT_AGENTS_DIR,
    DEFAULT_PROVIDER,
    KIRO_AGENTS_DIR,
    OPENCODE_AGENTS_DIR,
    SKILLS_DIR,
)
from cli_agent_orchestrator.models.copilot_agent import CopilotAgentConfig
from cli_agent_orchestrator.models.kiro_agent import KiroAgentConfig
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.models.opencode_agent import OpenCodeAgentConfig
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.services.profile_store import write_profile
from cli_agent_orchestrator.utils.agent_profiles import (
    _read_agent_profile_source,
    parse_agent_profile_text,
)
from cli_agent_orchestrator.utils.env import resolve_env_vars, set_env_var
from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config
from cli_agent_orchestrator.utils.opencode_config import (
    OpenCodeAgentIdCollisionError,
    ensure_skills_symlink,
    remove_agent_tools,
    to_opencode_agent_id,
    translate_mcp_server_config,
    upsert_agent_tools,
    upsert_mcp_server,
)
from cli_agent_orchestrator.utils.opencode_permissions import cao_tools_to_opencode_permission
from cli_agent_orchestrator.utils.path_validation import (
    flatten_path_separators,
    validate_path_component,
)
from cli_agent_orchestrator.utils.skill_injection import compose_agent_prompt
from cli_agent_orchestrator.utils.tool_mapping import resolve_allowed_tools

logger = logging.getLogger(__name__)


class InstallResult(BaseModel):
    """Structured result for agent profile installation."""

    success: bool
    message: str
    agent_name: Optional[str] = None
    context_file: Optional[str] = None
    agent_file: Optional[str] = None
    unresolved_vars: Optional[List[str]] = None
    source_kind: Optional[Literal["url", "file", "name"]] = None
    provider: Optional[str] = None


# Profile names are used as filesystem path segments under LOCAL_AGENT_STORE_DIR
# and provider agent dirs. Restricting to [A-Za-z0-9_-] with a 64-char cap blocks
# traversal ("../etc/passwd"), separators, and absolute paths at the boundary.
# CodeQL also recognises this regex as a path-injection sanitiser.
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Context-copy provenance marker — stamped into <context dir>/<name>.md
# frontmatter to record the original install source stem (the stem/name passed to
# `cao install`). Used by the opencode collision guard to distinguish a profile's
# own installed copy from a different profile that resolves to the same agent id.
_CONTEXT_SOURCE_STEM_KEY = "x-cao-source-stem"
_CONTEXT_SOURCE_STEM_RE = re.compile(rf"^\s*{re.escape(_CONTEXT_SOURCE_STEM_KEY)}\s*:")
_TEMP_FILE_NAME_ATTEMPTS = 100
_FRONTMATTER_DELIMITER_RE = re.compile(r"^-{3,}$")

# Per-MCP-server tool-call timeout (milliseconds) injected into cao-mcp-server
# entries in kiro agent profiles. kiro-cli's default MCP tool-call timeout
# (~120s, inherited from the Q Developer CLI) is far too short for the handoff
# tool, which blocks until a spawned worker finishes an entire task — routinely
# minutes. Without a raised timeout kiro cancels the handoff RPC client-side and
# tells the supervisor the tool failed even though CAO is still running the
# worker. 1_200_000 ms (20 min) matches CAO's default handoff/run-step budget.
# This mirrors the kimi_cli provider's tool_call_timeout_ms override.
_KIRO_MCP_TOOL_TIMEOUT_MS = 1_200_000


def _inject_kiro_mcp_timeout(
    mcp_servers: Optional[Dict[str, object]],
) -> Optional[Dict[str, object]]:
    """Return a copy of ``mcp_servers`` with a large ``timeout`` set on every
    cao-mcp-server entry that does not already specify one.

    kiro reads the per-server ``timeout`` field (milliseconds) as its tool-call
    timeout. We only touch entries whose name, command, or args reference the
    bundled orchestration server so a user's other MCP servers keep their own
    (or kiro's default) timeout. An explicit operator-set ``timeout`` is never
    overwritten. The command/args checks cover every form the entry can take:
    the bare console script, a resolved absolute path, the module entrypoint
    (``<python> -m cli_agent_orchestrator.mcp_server.server``), and the legacy
    ``uvx --from git+... cao-mcp-server`` form.
    """
    if not mcp_servers:
        return mcp_servers

    result: Dict[str, object] = {}
    for name, cfg in mcp_servers.items():
        if not isinstance(cfg, dict):
            result[name] = cfg
            continue
        command = cfg.get("command")
        args = cfg.get("args") or []
        is_cao = (
            name == "cao-mcp-server"
            or (isinstance(command, str) and "cao-mcp-server" in command)
            or any(
                isinstance(a, str)
                and ("cao-mcp-server" in a or a == "cli_agent_orchestrator.mcp_server.server")
                for a in args
            )
        )
        if is_cao and "timeout" not in cfg:
            cfg = {**cfg, "timeout": _KIRO_MCP_TOOL_TIMEOUT_MS}
        result[name] = cfg
    return result


# URL path component for allowlisted hosts. Each segment must start with an
# alphanumeric, which forbids "..", "." and hidden segments — and by extension
# any traversal sequence. Used to rebuild a safe URL from validated parts,
# which is the CodeQL-recognised SSRF sanitisation pattern.
_SAFE_URL_PATH_RE = re.compile(r"^(/[A-Za-z0-9_][A-Za-z0-9_.-]*)+\.md$")

# SSRF guard: only fetch profiles from hosts we explicitly trust. Operators can
# extend via CAO_PROFILE_ALLOWED_HOSTS (e.g. an internal profile mirror).
_DEFAULT_ALLOWED_HOSTS = frozenset(
    {
        "github.com",
        "raw.githubusercontent.com",
    }
)

# (connect, read) seconds. Tighter than a single-number timeout: 5s connect fails
# fast on a dead/hostile IP; 30s read leaves room for flaky residential networks
# without letting a slow-loris peer tie up a cao-server worker indefinitely.
_HTTP_TIMEOUT = (5, 30)


def _allowed_download_hosts() -> frozenset:
    override = os.environ.get("CAO_PROFILE_ALLOWED_HOSTS")
    if override:
        hosts = {h.strip().lower() for h in override.split(",") if h.strip()}
        if hosts:
            return frozenset(hosts)
    return _DEFAULT_ALLOWED_HOSTS


def _download_agent(source: str) -> str:
    """Download an agent profile from an https:// URL into the local store.

    File-path handling deliberately does NOT live in this module: only the CLI
    has legitimate filesystem trust, and keeping Path(user_input) out of the
    HTTP-reachable layer closes an entire class of py/path-injection alerts
    (CodeQL #49/#61 kept reopening while this lived here). The CLI entry point
    resolves the local file itself and stores it via profile_store, then calls
    install_agent() with the bare stem, which flows through the "name" branch.
    This function only ever hands profile_store a stem it has already validated,
    never a caller-supplied path.
    """
    # SSRF hardening: narrow what a caller-provided URL can reach before any
    # network I/O happens. https-only rules out http://169.254.169.254/...;
    # the host allowlist rules out arbitrary internal services; the path
    # regex rules out crafted paths that would write outside the store.
    parsed = urlparse(source)
    if parsed.scheme != "https":
        raise ValueError("Profile URL must use https://")
    host = (parsed.hostname or "").lower()
    allowed_hosts = _allowed_download_hosts()
    if host not in allowed_hosts:
        raise ValueError(
            f"Host '{host}' is not in the allowed downloader hosts. "
            "Set CAO_PROFILE_ALLOWED_HOSTS to extend the allowlist."
        )
    # Reject any URL that carries a query string, fragment, or userinfo —
    # none of them are meaningful for a static .md fetch and each is an
    # SSRF foothold (credentials encoded in @, redirect targets in ?next=).
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("Profile URL must not include query, fragment, or userinfo.")
    if not _SAFE_URL_PATH_RE.fullmatch(parsed.path):
        raise ValueError("URL path must match /segment/.../file.md with no traversal segments.")
    filename = parsed.path.rsplit("/", 1)[-1]
    if not _PROFILE_NAME_RE.fullmatch(filename[: -len(".md")]):
        raise ValueError("URL filename stem must match [A-Za-z0-9_-]{1,64}")

    # Look up the canonical host from the allowlist instead of passing the
    # parsed host back through. Belt-and-braces: even if a caller smuggled
    # an odd Unicode codepoint that normalised into a known host name,
    # `safe_host` is guaranteed to be a literal from our trust root.
    safe_host = next(h for h in allowed_hosts if h == host)
    safe_url = f"https://{safe_host}{parsed.path}"

    # allow_redirects=False + explicit is_redirect check: an allowlisted
    # host could otherwise 302 us to an internal target (IMDS, admin panel)
    # and the allowlist would never see the hop.
    response = requests.get(safe_url, timeout=_HTTP_TIMEOUT, allow_redirects=False)
    if response.is_redirect:
        raise ValueError("Redirects are not allowed for profile downloads.")
    response.raise_for_status()

    # The stem was validated against _PROFILE_NAME_RE above; profile_store owns
    # the store join and the atomic write. overwrite=True preserves the
    # pre-existing re-download behaviour of replacing the stored copy.
    stem = filename[: -len(".md")]
    write_profile(stem, response.text, overwrite=True)
    return stem


def parse_env_assignment(env_assignment: str) -> Tuple[str, str]:
    """Parse a ``KEY=VALUE`` assignment used for install-time env injection."""
    if "=" not in env_assignment:
        raise ValueError(f"Invalid env var '{env_assignment}'. Expected format KEY=VALUE.")

    key, value = env_assignment.split("=", 1)
    if not key:
        raise ValueError(f"Invalid env var '{env_assignment}'. Key must not be empty.")

    return key, value


def _line_body_and_ending(line: str) -> Tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n"):
        return line[:-1], "\n"
    if line.endswith("\r"):
        return line[:-1], "\r"
    return line, ""


def _is_frontmatter_delimiter(line_body: str, *, allow_bom: bool = False) -> bool:
    if allow_bom:
        line_body = line_body.removeprefix("\ufeff")
    # python-frontmatter's YAMLHandler accepts 3+ dashes as a delimiter
    # (`^-{3,}\s*$`); matching that here keeps this writer's notion of "where
    # the frontmatter block is" in sync with the parser CAO uses everywhere
    # else, so real frontmatter with a `----` delimiter is not demoted into
    # the body.
    return bool(_FRONTMATTER_DELIMITER_RE.match(line_body.strip(" \t")))


def _first_newline(raw_content: str) -> str:
    match = re.search(r"\r\n|\n|\r", raw_content)
    return match.group(0) if match else "\n"


def _parses_as_yaml_mapping(text: str) -> bool:
    """Return True if ``text`` is what python-frontmatter would treat as real
    frontmatter metadata: a YAML mapping, or empty (``frontmatter.parse`` only
    merges ``fm_data`` into ``metadata`` when it is a ``dict``; anything else \u2014
    a bare scalar, a list, invalid YAML \u2014 is silently NOT metadata there).
    """
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError:
        return False
    return loaded is None or isinstance(loaded, dict)


def _find_frontmatter_block(lines: List[str]) -> Optional[Tuple[int, int]]:
    """Return opening/closing line indexes for the leading frontmatter block.

    A candidate span only counts as frontmatter if the text between the
    delimiters actually parses as a YAML mapping (see
    :func:`_parses_as_yaml_mapping`) \u2014 matching what ``frontmatter.loads``
    treats as real metadata, rather than a purely lexical dash match. Without
    this, a frontmatter-less document whose body opens with a markdown
    thematic break (a line of 3+ dashes) gets mistaken for a frontmatter
    opener, the marker gets inserted into the middle of prose, and the
    document becomes invalid YAML.
    """
    opening_idx: Optional[int] = None
    for idx, line in enumerate(lines):
        body, _ = _line_body_and_ending(line)
        if body.removeprefix("\ufeff").strip(" \t") == "":
            continue
        if _is_frontmatter_delimiter(body, allow_bom=True):
            opening_idx = idx
        break

    if opening_idx is None:
        return None

    for idx in range(opening_idx + 1, len(lines)):
        body, _ = _line_body_and_ending(lines[idx])
        if _is_frontmatter_delimiter(body):
            block_text = "".join(lines[opening_idx + 1 : idx])
            if _parses_as_yaml_mapping(block_text):
                return opening_idx, idx
            return None
    return None


def _frontmatter_block_indent(lines: List[str], opening_idx: int, closing_idx: int) -> str:
    """Return the leading whitespace of the block's first real content line.

    Frontmatter keys are not required to sit at column 0 \u2014 YAML only needs
    consistent indentation. Inserting the marker at column 0 into a block
    indented some other way breaks that consistency and corrupts the YAML;
    matching the block's own indentation keeps it valid.
    """
    for idx in range(opening_idx + 1, closing_idx):
        body, _ = _line_body_and_ending(lines[idx])
        stripped = body.lstrip(" \t")
        if stripped == "" or stripped.startswith("#"):
            continue
        return body[: len(body) - len(stripped)]
    return ""


def _yaml_single_quoted(value: str) -> str:
    """Render a one-line YAML string scalar."""
    if "\n" in value or "\r" in value:
        raise ValueError("Context source stem must fit on one YAML line")
    return "'" + value.replace("'", "''") + "'"


def _context_marker_line(source_name: str, newline: str) -> str:
    return f"{_CONTEXT_SOURCE_STEM_KEY}: {_yaml_single_quoted(source_name)}{newline}"


def _context_content_with_provenance(raw_content: str, source_name: str) -> str:
    """Return context markdown annotated without reserializing frontmatter.

    If a leading frontmatter block exists, every textually-matching marker
    line is removed and a single clean one is inserted in the first matched
    line's place (or at the top of the block if none matched). Documents
    without a leading block get a minimal frontmatter block prepended,
    leaving the original content byte-for-byte intact after that inserted
    block.

    The line-regex insertion above only recognises an unquoted, column-0
    ``x-cao-source-stem:`` key. A source profile can carry a marker spelled a
    way the regex cannot see (a quoted key, a folded/multi-line value, a
    flow-mapping frontmatter document) while PyYAML's parser — the reader
    every consumer of this content actually uses — sees it as the *same* key
    and would resolve it (last-wins on duplicates) to a value CAO never
    wrote. Trusting the regex's view there would let profile content dictate
    its own provenance, defeating the guard this marker exists for. So the
    assembled content is read back through :func:`_context_source_stem` —
    the exact function the collision guard calls — and the install is
    refused unless that readback agrees with ``source_name``. This also
    catches content the textual insertion accidentally corrupted into
    invalid YAML (e.g. a folded scalar's continuation line left orphaned)
    before it is ever written to disk.
    """
    lines = raw_content.splitlines(keepends=True)
    block = _find_frontmatter_block(lines)
    if block is None:
        newline = _first_newline(raw_content)
        marker = _context_marker_line(source_name, newline)
        content = f"---{newline}{marker}---{newline}{raw_content}"
    else:
        opening_idx, closing_idx = block
        _, opening_newline = _line_body_and_ending(lines[opening_idx])
        newline = opening_newline or _first_newline(raw_content)
        indent = _frontmatter_block_indent(lines, opening_idx, closing_idx)
        marker = indent + _context_marker_line(source_name, newline)

        existing_indices = [
            idx
            for idx in range(opening_idx + 1, closing_idx)
            if _CONTEXT_SOURCE_STEM_RE.match(_line_body_and_ending(lines[idx])[0])
        ]
        insert_at = existing_indices[0] if existing_indices else opening_idx + 1
        for idx in reversed(existing_indices):
            del lines[idx]
        lines.insert(insert_at, marker)
        content = "".join(lines)

    try:
        verified_stem = _context_source_stem(content)
        verify_exc: Optional[Exception] = None
    except Exception as exc:
        verified_stem = None
        verify_exc = exc
    if verified_stem != source_name:
        if verify_exc is not None:
            cause = (
                "the assembled context copy did not parse as valid YAML "
                f"frontmatter ({verify_exc})"
            )
        elif verified_stem is None:
            cause = f"the assembled context copy has no readable '{_CONTEXT_SOURCE_STEM_KEY}' value"
        else:
            cause = (
                "the assembled context copy reads back "
                f"'{_CONTEXT_SOURCE_STEM_KEY}: {verified_stem}' instead of "
                f"'{source_name}' — the source profile's own frontmatter "
                f"likely defines a conflicting '{_CONTEXT_SOURCE_STEM_KEY}' key"
            )
        raise ValueError(
            "Refusing to write context copy: could not stamp a trustworthy "
            f"'{_CONTEXT_SOURCE_STEM_KEY}' provenance marker for install "
            f"source '{source_name}' because {cause}. Fix the source "
            "profile's frontmatter (remove or rename the conflicting key, or "
            "repair its YAML syntax), then reinstall."
        )
    return content


def _context_source_stem(raw_content: str) -> Optional[str]:
    """Read CAO source-stem provenance from generated context frontmatter."""
    post = frontmatter.loads(raw_content)
    value = post.metadata.get(_CONTEXT_SOURCE_STEM_KEY)
    if isinstance(value, str):
        return value

    lines = raw_content.splitlines(keepends=True)
    block = _find_frontmatter_block(lines)
    if block is None:
        return None

    opening_idx, closing_idx = block
    for idx in range(opening_idx + 1, closing_idx):
        body, _ = _line_body_and_ending(lines[idx])
        if not _CONTEXT_SOURCE_STEM_RE.match(body):
            continue
        marker_post = frontmatter.loads(f"---\n{body}\n---\n")
        marker_value = marker_post.metadata.get(_CONTEXT_SOURCE_STEM_KEY)
        return marker_value if isinstance(marker_value, str) else None
    return None


def _context_dir() -> Path:
    """Resolve the shared context directory the way profile discovery does.

    Discovery scans the ``cao_installed`` entry of ``agents.dirs`` (see
    ``utils/agent_profiles.py``), and the opencode collision guard reads its
    candidates from there. The writer has to deposit copies in the SAME place,
    or an operator who overrides ``cao_installed`` gets copies discovery never
    sees -- and a guard that is blind to exactly the files it protects.

    ``settings_service.installed_context_dir_override`` owns the rule (the
    setting counts only when it departs from its default); this falls back to
    ``AGENT_CONTEXT_DIR`` so the constant stays authoritative when nothing is
    configured, which is also what lets tests redirect the directory by
    patching the constant alone.
    """
    from cli_agent_orchestrator.services.settings_service import (
        installed_context_dir_override,
    )

    override = installed_context_dir_override()
    return AGENT_CONTEXT_DIR if override is None else override


def _context_lookup_dirs() -> List[Path]:
    """Every directory an existing context copy may be sitting in.

    The configured directory first, then -- only when an override is active --
    the default it superseded. Releases before the override was honoured by the
    writer deposited every copy at ``AGENT_CONTEXT_DIR`` even when
    ``cao_installed`` pointed elsewhere, so an operator who upgrades with an
    override already configured has ownership records in the legacy directory.
    Probing it keeps those records in force: the guard still sees the owner of
    an id, and the Copilot skill-injection probe still recognises the agents it
    manages. New copies are written to ``_context_dir()`` only; the legacy copy
    is left where it is.
    """
    from cli_agent_orchestrator.services.settings_service import (
        installed_context_lookup_dirs,
    )

    return installed_context_lookup_dirs(AGENT_CONTEXT_DIR)


def _installed_context_copy_path(stem: str, directory: Optional[Path] = None) -> Path:
    """Return the installed context path for ``stem`` in ``directory``.

    Prefers the flat ``<stem>.md`` the writer produces; falls back to the
    directory-style ``<stem>/agent.md`` discovery also recognises, so an
    operator-arranged copy in that shape still counts as occupying the id.
    """
    installed_dir = _context_dir() if directory is None else directory
    flat = installed_dir / f"{stem}.md"
    if flat.exists():
        return flat
    nested = installed_dir / stem / "agent.md"
    if nested.exists():
        return nested
    return flat


def _installed_context_copy_remedy(path: Path) -> str:
    """Tell operators how to recover from an unproven installed context copy."""
    return (
        f"If '{path}' is your own profile's context copy from an earlier CAO "
        "version, delete it and reinstall."
    )


class InstalledContextCopyCollisionError(ValueError):
    """A different profile already owns the shared context copy this install would overwrite.

    The provider-neutral counterpart of :class:`OpenCodeAgentIdCollisionError`:
    raised by :func:`_guard_installed_copy_ownership` for every provider other
    than OpenCode, whose installs additionally share the ``<id>.md`` agent file
    and ``agent.<id>`` config section. Subclasses ``ValueError`` for the same
    reason: ``install_agent``'s broad handler turns it into a clean CLI error.
    """


def _is_opencode(provider: str) -> bool:
    return provider == ProviderType.OPENCODE_CLI.value


def _collision_error_class(provider: str) -> type:
    return (
        OpenCodeAgentIdCollisionError
        if _is_opencode(provider)
        else InstalledContextCopyCollisionError
    )


def _installed_copy_display(provenance_stem: Optional[str], candidate_path: Path) -> str:
    """Render the occupying context copy for collision errors."""
    if provenance_stem:
        return f"'{provenance_stem}.md' (installed copy at '{candidate_path}')"
    return f"an installed copy without CAO source provenance at '{candidate_path}'"


def _raise_unloadable_installed_collision(
    target_id: str, source_name: str, profile_name: str, candidate_path: Path, provider: str
) -> None:
    """Block an install whose target slot is held by a copy of unknowable ownership."""
    slot = (
        f"OpenCode agent id '{target_id}'"
        if _is_opencode(provider)
        else f"Profile name '{target_id}'"
    )
    artifacts = "OpenCode artifacts" if _is_opencode(provider) else "installed artifacts"
    raise _collision_error_class(provider)(
        f"{slot} is already occupied by installed context copy '{candidate_path}', but "
        "CAO cannot read or validate that file, so it cannot prove whether it belongs "
        f"to the profile being installed ('{source_name}.md', name '{profile_name}'). "
        f"The install was refused to avoid silently overwriting existing {artifacts}. "
        f"{_installed_context_copy_remedy(candidate_path)}"
    )


def _raise_unreadable_installed_copy(
    target_id: str, source_name: str, candidate_path: Path, exc: OSError, provider: str
) -> None:
    """Block an install whose target slot holds a copy CAO could not read (an I/O fault)."""
    slot = (
        f"OpenCode agent id '{target_id}'"
        if _is_opencode(provider)
        else f"Profile name '{target_id}'"
    )
    raise _collision_error_class(provider)(
        f"{slot} is already occupied by installed context copy '{candidate_path}', which "
        f"could not be read ({exc.strerror or exc.__class__.__name__}), so CAO cannot tell "
        f"whether it belongs to the profile being installed ('{source_name}.md'). The install "
        "was refused rather than overwrite it. Fix the file's permissions (or the underlying "
        "I/O problem) and reinstall; do not delete the copy, it is the ownership record."
    )


def _non_regular_target_error(context_file: Path) -> ValueError:
    return ValueError(
        f"Context file '{context_file}' is already occupied by a non-regular "
        "filesystem entry. The install was refused to avoid writing through "
        "a symlink or overwriting a directory, device, socket, or FIFO. "
        "Remove that path or replace it with a regular file, then reinstall."
    )


def _create_context_temp_file(context_file: Path) -> Tuple[int, Path]:
    """Create a same-directory temp file for the context copy and return its
    open fd and path.

    Created 0o600, matching the mode the pre-atomic ``os.open`` sink asked for:
    this holds agent instruction content under
    ``~/.aws/cli-agent-orchestrator/`` and does not need to be group- or
    world-readable. Naming the mode outright rather than requesting 0o666 and
    letting the umask subtract also means the result does not depend on the
    ambient umask, so a permissive umask cannot widen a new context copy. On a
    reinstall the caller restores the existing target's mode via ``os.fchmod``
    before the replace.

    ``O_EXCL`` is what makes the name unguessable-and-unique rather than merely
    unlikely to collide: the create fails rather than opening a file an attacker
    pre-planted at the temp path.
    """
    last_exc: Optional[OSError] = None
    for _ in range(_TEMP_FILE_NAME_ATTEMPTS):
        candidate = context_file.parent / f".{context_file.name}.{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            last_exc = exc
            continue
        return fd, candidate
    raise OSError(
        f"Could not create a unique temporary file next to '{context_file}'"
    ) from last_exc


def _write_context_file(agent_name: str, raw_content: str, source_name: str) -> Path:
    """Write the unresolved profile source to the shared context directory.

    ``agent_name`` is the *resolved* profile name (frontmatter ``name:``) and
    determines the filename — the context copy lives at
    ``<context dir>/<resolved-name>.md`` (see ``_context_dir``), NOT under the original install
    stem. ``source_name`` is the install *source handle* (the stem/name passed
    to ``cao install``), so it can be stamped into the copy's frontmatter under
    ``_CONTEXT_SOURCE_STEM_KEY``. The opencode collision guard later uses that
    marker to prove "this installed-dir
    artifact is a prior copy of the profile being reinstalled" versus "this is
    a different profile that resolves to the same agent id" (see
    :func:`_guard_installed_copy_ownership`). The marker is inserted
    textually, preserving source formatting aside from that one marker line.

    SECURITY. The filename derives from the profile's RESOLVED frontmatter
    ``name:``. That value is NOT covered by ``_PROFILE_NAME_RE`` -- that regex
    validates the install *source handle* (the URL stem / bare-name argument),
    not the resolved name -- and a profile can be installed straight from a URL,
    so the field is attacker-controlled. Without a guard, a name like
    ``../../foo`` or an absolute path steers this write outside
    the context directory and can overwrite a trusted ``.md`` instruction file.
    Three layers, all in this function (see the barrier note below):

    1. ``validate_path_component`` -- the shared segment validator, which rejects
       empty, ``.``/``..``, NUL, every path separator, and anything outside
       ``[A-Za-z0-9._-]``. The allowlist also makes Unicode normalization a
       non-issue: a fullwidth solidus (U+FF0F) is rejected outright rather than
       having to be caught before it folds to ``/`` under NFKC.
    2. Lexical containment under the realpath of the base directory.
    3. Refusal to write through a symlink at the final component -- enforced here
       by the ``lstat`` type check plus ``os.replace`` (which replaces a symlink
       rather than following it), where the pre-atomic writer used
       ``O_NOFOLLOW`` on a direct open of the target. See the long comment at
       that check for why the substitution is not a weakening.

    ATOMICITY AND MODE. The target must be absent or a regular file; symlinks,
    directories, FIFOs, sockets, and devices are refused before writing (one
    ``lstat`` serves both that check and the existing-mode read, so neither
    follows a symlink planted in the window between them). Content goes to a
    same-directory temporary file and is atomically replaced into place, so CAO
    never opens the target path itself and a failed write cannot leave a
    truncated context copy. A brand-new copy is created 0o600; on a reinstall the
    existing target's mode is restored via ``os.fchmod`` before the replace,
    since otherwise ``os.replace`` would carry an unrelated mode onto the target
    and silently tighten or widen permissions on every install.
    """
    context_dir = _context_dir()
    if not context_dir.is_absolute():
        # ``installed_context_dir_override`` already discards blank and relative
        # settings; this is the sink's own refusal to ever treat the server's
        # working directory as the trusted write root (a profile named README or
        # AGENTS would otherwise land on a repository file).
        raise ValueError(
            f"Refusing to write context copy: the installed-profile directory "
            f"{str(context_dir)!r} is not an absolute path. Set agents.dirs.cao_installed "
            "to an absolute directory or remove it to use the default."
        )
    context_dir.mkdir(parents=True, exist_ok=True)
    # BARRIER PLACEMENT: the validation and the containment check are inlined
    # here, in the same function as the write sink, rather than factored into a
    # helper. This mirrors the deliberate repetition in ``services/profile_store``
    # -- CodeQL's py/path-injection dataflow only recognises a barrier that
    # guards, in the same function as the sink, the very variable that reaches
    # it. A helper that returns a validated path is more readable but invisible
    # to the analysis, and this repo has a history of that alert reopening (see
    # profile_store._PROFILE_NAME_RE). Load-bearing, not an oversight.
    safe_name = validate_path_component(agent_name, description="profile name")
    # Resolve only the BASE (so a symlinked context root is handled) and keep the
    # final component UNRESOLVED. Resolving the whole candidate -- as
    # ``safe_join_under_base`` does -- would follow a symlink planted at the
    # target and silently write to wherever it resolves; leaving the final
    # component lexical means such a symlink is refused by the lstat check
    # below. That is why this does not simply call ``safe_join_under_base``.
    base = os.path.realpath(context_dir)
    candidate = os.path.join(base, f"{safe_name}.md")
    if candidate != base and not candidate.startswith(base + os.sep):
        raise ValueError(
            f"Refusing to write context copy: profile name {agent_name!r} resolves "
            f"to a path outside the agent context directory ({candidate!r})."
        )
    context_file = Path(candidate)
    # HOW THE SYMLINK REFUSAL IS ENFORCED HERE, having replaced O_NOFOLLOW.
    # The pre-atomic writer opened the target directly, so it needed O_NOFOLLOW to
    # stop the kernel writing THROUGH a symlink planted at the final component.
    # This writer never opens the target at all: it writes a same-directory temp
    # file and ``os.replace``s it into place, and ``os.replace`` replaces the
    # symlink ITSELF rather than following it, so the write cannot land outside
    # the directory even if the lstat below is raced. The lstat is what turns that
    # into a clear refusal instead of silently clobbering an operator's symlink.
    # Strictly stronger than the O_NOFOLLOW form on two counts: it also refuses
    # directories/FIFOs/sockets/devices by type rather than by errno, and it holds
    # on Windows, where os.O_NOFOLLOW does not exist and degraded to a no-op.
    try:
        st = os.lstat(context_file)
    except FileNotFoundError:
        st = None
    if st is not None and not stat.S_ISREG(st.st_mode):
        raise _non_regular_target_error(context_file)
    # Reinstalls keep the target's current mode; a brand-new copy gets 0o600 from
    # _create_context_temp_file. This file lives under
    # ~/.aws/cli-agent-orchestrator/ and holds agent instruction content, so it is
    # not group/world readable by default -- but silently RE-tightening a mode an
    # operator widened on purpose would be its own surprise, so an existing mode
    # is preserved rather than reasserted.
    existing_mode = stat.S_IMODE(st.st_mode) if st is not None else None

    content = _context_content_with_provenance(raw_content, source_name)
    temp_path: Optional[Path] = None
    try:
        fd, temp_path = _create_context_temp_file(context_file)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as tmp:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
            if existing_mode is not None and platform.system() != "Windows":
                os.fchmod(tmp.fileno(), existing_mode)
        os.replace(temp_path, context_file)
    except Exception as exc:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
        try:
            recheck = os.lstat(context_file)
        except FileNotFoundError:
            recheck = None
        if recheck is not None and not stat.S_ISREG(recheck.st_mode):
            raise _non_regular_target_error(context_file) from exc
        # Name the real target the operator asked to install, not an
        # internal, randomly-suffixed temp filename that may no longer even
        # exist (e.g. a read-only context dir before the temp file was ever
        # created, or a mid-write failure, or a `.tmp` cleaner racing
        # `os.replace`). `strerror` (unlike `str(exc)`) never embeds a path.
        detail = getattr(exc, "strerror", None) or str(exc)
        raise OSError(f"Failed to write context file '{context_file}': {detail}") from exc
    return context_file


def _build_provider_config(
    profile_name: str,
    resolved_prompt: str,
    description: str,
) -> frontmatter.Post:
    """Create the frontmatter post for a Copilot agent file."""
    return frontmatter.Post(
        resolved_prompt.rstrip(),
        name=profile_name,
        description=description,
    )


def _guard_installed_copy_ownership(source_name: str, profile_name: str, provider: str) -> None:
    """Refuse an install that would overwrite a context copy owned by another profile.

    Every install writes the shared context copy ``<context dir>/<resolved
    name>.md``; an OpenCode install additionally writes ``OPENCODE_AGENTS_DIR/<id>.md``
    and the ``agent.<id>`` section of ``opencode.json``, where ``<id>`` is the
    resolved name (``to_opencode_agent_id`` is the identity for every name that
    passes validation). Two profile FILES can resolve to the same ``name:``, so
    the second install would silently replace the first's artifacts -- and the
    context copy is not bookkeeping: a Kiro agent's ``resources`` point at it
    and it is what the installed agent reads at runtime. That is why this runs
    for every provider, not only OpenCode (round-3 review of #493).

    **Occupancy is read from the destination itself**, not from profile
    discovery. ``list_agent_profiles()`` keeps the first profile per stem, so an
    installed copy is relegated to ``duplicated_in`` whenever the local store or
    a provider directory holds a file of the same stem -- which is the ordinary
    case of a profile installed under its own name -- and a disabled directory,
    a ``~`` spelling or a discovery failure erased the evidence outright. The
    files a second install overwrites are at known paths, so those paths are what
    is probed: ``_context_lookup_dirs()`` (the configured directory, plus the
    legacy default when an override is active) for ``<id>.md``.

    **Ownership is the provenance marker.** ``_write_context_file`` stamps each
    copy with ``_CONTEXT_SOURCE_STEM_KEY`` naming the install stem it came from.
    A copy whose marker names ``source_name`` is this very profile's earlier
    copy: reinstall and upgrade proceed. A marker naming a different stem is a
    collision. A missing marker (a copy written before the marker existed)
    cannot prove either, so it blocks with a recovery message rather than being
    assumed to be self -- legitimate upgrades change the body, so payload
    equality is not an identity signal. The installed copy's own ``name:`` is
    never parsed here: it may hold an unresolved ``${VAR}`` placeholder, and the
    id it occupies is already the filename.

    A non-regular entry at the probed path (symlink, directory, device) is left
    to ``_write_context_file``, whose lstat check refuses it with the message
    that names the real problem. Only a collision implicating the profile being
    installed blocks; two OTHER profiles clashing is not this install's concern.
    This remains a pre-write check with no locking between check and write.
    """
    # Validated here, in the same function as the paths built from it, so the
    # probe below cannot be steered outside the context directory by a hostile
    # ``name:`` (the writer repeats this check at its own sink for the same
    # reason; see the BARRIER PLACEMENT note there). A separator-bearing name is
    # therefore refused AS an invalid name, whether or not its flattened id
    # happens to be occupied: it can never be installed, so "rename the other
    # profile" would be the wrong remedy.
    safe_name = validate_path_component(profile_name, description="profile name")
    target_id = to_opencode_agent_id(safe_name)

    for context_dir in _context_lookup_dirs():
        candidate_path = _installed_context_copy_path(target_id, context_dir)
        try:
            entry = os.lstat(candidate_path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            _raise_unreadable_installed_copy(target_id, source_name, candidate_path, exc, provider)
        if not stat.S_ISREG(entry.st_mode):
            # The writer refuses this target itself, naming the real problem.
            continue
        try:
            raw = candidate_path.read_text(encoding="utf-8")
        except OSError as exc:
            # An I/O fault, not a bad file: the remedy is to fix access, not to
            # delete the copy (which would discard the ownership record).
            _raise_unreadable_installed_copy(target_id, source_name, candidate_path, exc, provider)
        try:
            provenance_stem = _context_source_stem(raw)
        except Exception as exc:
            logger.debug(
                "Could not parse installed-profile provenance from '%s': %s", candidate_path, exc
            )
            _raise_unloadable_installed_collision(
                target_id, source_name, profile_name, candidate_path, provider
            )
        if provenance_stem == source_name:
            # Our own earlier copy (possibly in the legacy directory): a reinstall.
            continue

        existing = _installed_copy_display(provenance_stem, candidate_path)
        recovery = "" if provenance_stem else f" {_installed_context_copy_remedy(candidate_path)}"
        if _is_opencode(provider):
            message = (
                f"OpenCode agent id '{target_id}' is produced by both the profile "
                f"being installed ('{source_name}.md', name '{profile_name}') and "
                f"the existing profile {existing} (name '{target_id}'). Two "
                "distinct profiles cannot share an OpenCode agent id: they install "
                f"to the same '{target_id}.md' file and 'agent.{target_id}' config "
                "section, so the second would silently overwrite the first. Rename "
                "one of these profiles (their frontmatter 'name:' must differ)."
            )
        else:
            message = (
                f"Profile name '{target_id}' is already installed from the existing "
                f"profile {existing}; installing '{source_name}.md' (name "
                f"'{profile_name}') for {provider} would overwrite its shared context "
                f"copy, which the installed agent reads at runtime. Two distinct "
                "profiles cannot share a resolved name. Rename one of these profiles "
                "(their frontmatter 'name:' must differ)."
            )
        raise _collision_error_class(provider)(message + recovery)


def install_agent(
    source: str,
    provider: Optional[str] = None,
    env_vars: Optional[Dict[str, str]] = None,
) -> InstallResult:
    """Install an agent profile for the requested provider.

    ``provider`` resolution follows the same precedence as launch/handoff
    (see ``resolve_provider``): an explicit argument wins, then the profile's
    frontmatter ``provider:`` key, then ``DEFAULT_PROVIDER``. Pass ``None``
    to defer to the profile.

    ``source`` must be either an https:// URL on the allowlist or a bare
    profile name matching ``_PROFILE_NAME_RE``. Local ``.md`` file paths
    are deliberately NOT accepted here — the CLI copies user files into
    the local store itself and then calls this function with the resulting
    bare stem. This split is what lets the HTTP/MCP surface share this
    function safely: every caller reaches the same two sanitised shapes,
    and no call site constructs ``Path(user_input)`` through this module.
    """
    try:
        valid_providers = [provider_type.value for provider_type in ProviderType]
        # An explicit provider is validated up front so bad input fails fast
        # BEFORE any URL download or env-file mutation. Frontmatter providers
        # are validated after the profile is parsed (below).
        if provider is not None and provider not in valid_providers:
            return InstallResult(
                success=False,
                message=(
                    f"Invalid provider '{provider}'. "
                    f"Valid providers: {', '.join(valid_providers)}"
                ),
            )

        if source.startswith(("http://", "https://")):
            agent_name = _download_agent(source)
            source_kind: Literal["url", "name"] = "url"
        else:
            # `source` is treated as a bare profile name and feeds
            # _read_agent_profile_source() which builds Path objects from it.
            # Enforce the sanitiser at the boundary so every downstream sink
            # (agent_profiles.py and the provider-dir loop) sees safe input.
            if not _PROFILE_NAME_RE.fullmatch(source):
                return InstallResult(
                    success=False,
                    message=(
                        f"Invalid profile name '{source}'. "
                        "Expected a name matching [A-Za-z0-9_-]{1,64}, "
                        "an https:// URL, or (CLI only) a local .md file path."
                    ),
                )
            agent_name = source
            source_kind = "name"

        if env_vars:
            for key, value in env_vars.items():
                set_env_var(key, value)

        raw_content = _read_agent_profile_source(agent_name)
        resolved_content = resolve_env_vars(raw_content)
        profile = parse_agent_profile_text(resolved_content, agent_name)

        # No explicit provider — honour the profile's frontmatter ``provider:``
        # key, mirroring resolve_provider() on the launch/handoff paths. Bogus
        # frontmatter values warn and fall back to the default; built-in store
        # profiles carry no frontmatter provider and keep the default.
        if provider is None:
            if profile.provider and profile.provider in valid_providers:
                provider = profile.provider
            else:
                if profile.provider:
                    logger.warning(
                        "Agent profile '%s' has invalid provider '%s'. "
                        "Valid providers: %s. Falling back to '%s'.",
                        profile.name,
                        profile.provider,
                        valid_providers,
                        DEFAULT_PROVIDER,
                    )
                provider = DEFAULT_PROVIDER

        # Resolve the bundled cao-mcp-server console script to a PATH-independent
        # invocation before materializing provider configs. The
        # configs Kiro/Q write to disk are consumed verbatim by those CLIs, so
        # resolution must happen here rather than at launch time. persisted=True
        # prefers the stable PATH launcher (e.g. ~/.local/bin/cao-mcp-server)
        # over the versioned venv-internal path, so a later `uv tool upgrade`
        # does not leave the written config pointing at a relocated binary.
        if profile.mcpServers:
            profile.mcpServers = {
                name: resolve_mcp_server_config(dict(cfg), persisted=True)
                for name, cfg in profile.mcpServers.items()
            }

        # Record the provider we actually installed for into the LOCAL store
        # copy, so later provider resolution on this node is deterministic.
        #
        # Without this, `cao install <p> --provider <x>` materialised the
        # provider-specific config (below) but left no trace of <x> anywhere
        # readable: resolve_provider() re-reads the profile, finds no
        # frontmatter `provider:` key, and silently falls back to the caller's
        # provider or DEFAULT_PROVIDER. Locally that is usually masked because
        # the fallback is inherited from the calling terminal, but on the
        # cross-node assign/handoff path `_assign_remote` deliberately omits
        # the provider and lets the TARGET node resolve it — so the target
        # would resolve DEFAULT_PROVIDER regardless of what was installed
        # there, and remote placement fails on any node whose installed
        # provider is not the default.
        #
        # Only the resolved `provider:` key is added; the body and every other
        # frontmatter key are preserved verbatim, and raw (unresolved) content
        # is stored so ${VARS} keep their placeholder form like the context
        # file. Note this materialises a local-store copy of a built-in
        # profile, which then shadows the packaged one on this node — that is
        # intended (the install is a per-node fact), but it does mean later CAO
        # upgrades will not change this profile's body on this node.
        if profile.provider != provider:
            stored = frontmatter.loads(raw_content)
            stored["provider"] = provider
            write_profile(agent_name, frontmatter.dumps(stored), overwrite=True)

        unresolved_vars = sorted(set(re.findall(r"\$\{(\w+)\}", resolved_content)))

        mcp_server_names = list(profile.mcpServers.keys()) if profile.mcpServers else None
        allowed_tools = resolve_allowed_tools(profile.allowedTools, profile.role, mcp_server_names)

        agent_file: Optional[Path] = None
        # Defence in depth. The resolved profile name is attacker-controlled, but
        # the ownership guard and _write_context_file BELOW both reject any name
        # carrying a path separator before any provider sink is reached, so
        # nothing separator-bearing gets here in the normal flow. The flatten
        # stays so each sink is independently safe if the order ever changes or
        # a new caller appears.
        safe_filename = flatten_path_separators(profile.name)

        # The ownership guard runs BEFORE any destructive write, for every
        # provider: the shared context copy is the first thing an install
        # overwrites and the artifact every provider's agent reads, so a rejected
        # install must leave it -- and, for OpenCode, the agent file and config
        # section it also shares -- untouched.
        _guard_installed_copy_ownership(agent_name, profile.name, provider)
        context_file = _write_context_file(profile.name, raw_content, agent_name)

        if provider == ProviderType.KIRO_CLI.value:
            if profile.engine == KiroEngine.KAS:
                raise ValueError(
                    "Kiro KAS profiles cannot be installed in Phase 0: CAO cannot "
                    "render KAS profiles or translate allowedTools/toolsSettings to Cedar. "
                    "Set engine: v2 or wait for a later migration phase."
                )
            KIRO_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
            # Kiro natively supports skill:// resources with progressive loading
            # (metadata at startup, full content on demand).
            kiro_resources = [
                f"file://{context_file.absolute()}",
                f"skill://{SKILLS_DIR}/**/SKILL.md",
            ]
            raw_prompt = (
                profile.prompt.strip() if profile.prompt and profile.prompt.strip() else None
            )
            kiro_agent_config = KiroAgentConfig(
                name=profile.name,
                description=profile.description,
                tools=profile.tools if profile.tools is not None else ["*"],
                allowedTools=allowed_tools,
                resources=kiro_resources,
                prompt=raw_prompt,
                # Raise the cao-mcp-server tool-call timeout so kiro doesn't
                # cancel long handoff RPCs client-side (see helper docstring).
                mcpServers=_inject_kiro_mcp_timeout(profile.mcpServers),
                toolAliases=profile.toolAliases,
                toolsSettings=profile.toolsSettings,
                hooks=profile.hooks,
                model=profile.model,
            )
            agent_file = KIRO_AGENTS_DIR / f"{safe_filename}.json"
            agent_file.write_text(
                kiro_agent_config.model_dump_json(indent=2, exclude_none=True),
                encoding="utf-8",
            )

        elif provider == ProviderType.COPILOT_CLI.value:
            COPILOT_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
            system_prompt = profile.system_prompt.strip() if profile.system_prompt else ""
            fallback_prompt = profile.prompt.strip() if profile.prompt else ""
            base_prompt = system_prompt or fallback_prompt
            if not base_prompt:
                raise ValueError(
                    f"Agent '{profile.name}' has no usable prompt content for Copilot "
                    "(both system_prompt and prompt are empty or whitespace)"
                )

            prompt = compose_agent_prompt(profile, base_prompt=base_prompt) or base_prompt
            copilot_agent_config = CopilotAgentConfig(
                name=profile.name,
                description=profile.description,
                prompt=prompt,
            )
            agent_file = COPILOT_AGENTS_DIR / f"{safe_filename}.agent.md"
            agent_file.write_text(
                frontmatter.dumps(
                    _build_provider_config(
                        profile_name=copilot_agent_config.name,
                        resolved_prompt=copilot_agent_config.prompt,
                        description=copilot_agent_config.description,
                    )
                ),
                encoding="utf-8",
            )

        elif provider == ProviderType.OPENCODE_CLI.value:
            OPENCODE_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
            ensure_skills_symlink()
            # OpenCode discovers skills natively from OPENCODE_CONFIG_DIR/skills,
            # so the installed system prompt should not embed the CAO skill catalog.
            body = profile.system_prompt or profile.prompt or ""
            opencode_agent_config = OpenCodeAgentConfig(
                description=profile.description,
                mode="all",
                permission=cao_tools_to_opencode_permission(allowed_tools),
            )

            agent_id = to_opencode_agent_id(profile.name)
            agent_file = OPENCODE_AGENTS_DIR / f"{agent_id}.md"
            agent_file.write_text(
                frontmatter.dumps(
                    frontmatter.Post(
                        body.rstrip() if body else "",
                        **opencode_agent_config.model_dump(exclude_none=True),
                    )
                ),
                encoding="utf-8",
            )

            # OpenCode uses a shared opencode.json for MCP declarations. Keep
            # top-level MCP entries default-denied, then re-enable them only
            # for the installed agent. A reinstall without MCP removes stale
            # per-agent grants.
            if profile.mcpServers:
                mcp_names = list(profile.mcpServers.keys())
                for mcp_name, mcp_cfg in profile.mcpServers.items():
                    opencode_mcp_cfg = translate_mcp_server_config(dict(mcp_cfg))
                    upsert_mcp_server(mcp_name, opencode_mcp_cfg)
                upsert_agent_tools(agent_id, mcp_names)
            else:
                remove_agent_tools(agent_id)

        return InstallResult(
            success=True,
            message=f"Agent '{profile.name}' installed successfully",
            agent_name=profile.name,
            context_file=str(context_file),
            agent_file=str(agent_file) if agent_file else None,
            unresolved_vars=unresolved_vars or None,
            source_kind=source_kind,
            provider=provider,
        )

    except requests.RequestException as exc:
        return InstallResult(success=False, message=f"Failed to download agent: {exc}")
    except FileNotFoundError as exc:
        return InstallResult(success=False, message=str(exc))
    except Exception as exc:
        return InstallResult(success=False, message=f"Failed to install agent: {exc}")
