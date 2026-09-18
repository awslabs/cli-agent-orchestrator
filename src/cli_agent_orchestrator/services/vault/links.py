"""Pure Obsidian wikilink extraction and conservative resolution."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Optional

from cli_agent_orchestrator.services.vault.findings import FindingCode

MAX_BODY_WIKILINKS = 1000
MAX_LINK_TARGET_CHARS = 256
_WIKILINK = re.compile(r"(?P<embed>!)?\[\[(?P<target>[^\]\r\n]+)\]\]")
_INLINE_MD_LINK = re.compile(
    r"(?<![!\\])\[[^\]\\\r\n]{0,256}\]" r"\((?P<destination>[^\s()\\\r\n]{1,256})\)"
)
_FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\r\n]*`")


@dataclass(frozen=True)
class LinkCandidate:
    key: str
    relpath: str
    aliases: tuple[str, ...] = ()
    excluded: bool = False


@dataclass(frozen=True)
class LinkOutcome:
    outcome: str
    target_key: Optional[str] = None
    finding_code: Optional[FindingCode] = None
    attributes: Optional[Mapping[str, object]] = None


@dataclass(frozen=True)
class LinkExtraction:
    """Bounded body-link results and any content-free extraction finding."""

    links: tuple[tuple[bool, str], ...]
    findings: tuple[FindingCode, ...] = ()
    relative_paths: tuple[bool, ...] = ()


def extract_wikilinks(text: str) -> LinkExtraction:
    """Extract up to 1000 supported body links outside fenced and inline code."""
    prose = _INLINE_CODE.sub("", _FENCED_CODE.sub("", text))
    positioned = [
        (match.start(), bool(match.group("embed")), match.group("target"), False)
        for match in _WIKILINK.finditer(prose)
    ]
    positioned.extend(
        (match.start(), False, destination, True)
        for match in _INLINE_MD_LINK.finditer(prose)
        if (destination := _relative_markdown_destination(match.group("destination"))) is not None
    )
    ordered = sorted(positioned)
    matches = tuple((embed, target) for _, embed, target, _ in ordered)
    relative_paths = tuple(relative for _, _, _, relative in ordered)
    findings = (FindingCode.LINK_LIMIT_EXCEEDED,) if len(matches) > MAX_BODY_WIKILINKS else ()
    return LinkExtraction(
        matches[:MAX_BODY_WIKILINKS],
        findings,
        relative_paths[:MAX_BODY_WIKILINKS],
    )


def resolve_wikilink(
    raw_target: str,
    *,
    embed: bool,
    candidates: tuple[LinkCandidate, ...],
    relative_path: bool = False,
    source_relpath: Optional[str] = None,
) -> LinkOutcome:
    """Resolve only exact/path-qualified candidates; ambiguous links are never guessed."""
    target = raw_target.split("|", 1)[0]
    name, separator, fragment = target.partition("#")
    if len(raw_target) > MAX_LINK_TARGET_CHARS or any(
        ord(character) < 32 or ord(character) == 127 for character in raw_target
    ):
        return LinkOutcome("unsupported", finding_code=FindingCode.LINK_TARGET_INVALID)
    if fragment.startswith("^"):
        return LinkOutcome("unsupported", finding_code=FindingCode.BLOCK_REFERENCE_UNSUPPORTED)
    if relative_path:
        resolved_name = _source_relative_name(name, source_relpath)
        if resolved_name is None:
            return LinkOutcome("unsupported", finding_code=FindingCode.LINK_TARGET_INVALID)
        name = resolved_name
    matching = tuple(
        candidate for candidate in candidates if _matches(name, candidate, exact_path=relative_path)
    )
    if not matching:
        if embed and _is_non_markdown_attachment(name):
            return LinkOutcome("unsupported", finding_code=FindingCode.ATTACHMENT_IGNORED)
        return LinkOutcome("dangling", finding_code=FindingCode.LINK_DANGLING)
    available = tuple(candidate for candidate in matching if not candidate.excluded)
    if not available:
        return LinkOutcome("excluded", finding_code=FindingCode.LINK_EXCLUDED)
    if len(available) != 1 or len(matching) != len(available):
        alias_match = any(name in candidate.aliases for candidate in matching)
        return LinkOutcome(
            "ambiguous",
            finding_code=FindingCode.ALIAS_AMBIGUOUS if alias_match else FindingCode.LINK_AMBIGUOUS,
        )
    attributes: dict[str, object] = {}
    finding = None
    if separator:
        attributes["fragment"] = fragment
        finding = FindingCode.HEADING_FRAGMENT_IGNORED
    if embed:
        attributes["embed"] = True
        finding = FindingCode.EMBED_NOT_INLINED
    return LinkOutcome("resolved", available[0].key, finding, attributes or None)


def _matches(name: str, candidate: LinkCandidate, *, exact_path: bool = False) -> bool:
    plain = candidate.relpath[:-3] if candidate.relpath.endswith(".md") else candidate.relpath
    basename = plain.rsplit("/", 1)[-1]
    if exact_path:
        return name == candidate.relpath
    return name in (plain, candidate.relpath, basename) or name in candidate.aliases


def _relative_markdown_destination(destination: str) -> Optional[str]:
    """Syntax-filter a relative Markdown target, preserving dot segments and fragments.

    Containment is source-dependent, so ``.`` and ``..`` remain literal here
    until :func:`_source_relative_name` can collapse them against the source
    note. Candidate and scope filtering remain the resolution authority.
    """
    path = destination.partition("#")[0]
    segments = path.split("/")
    if (
        not path.lower().endswith(".md")
        or path.startswith("/")
        or "?" in path
        or ":" in segments[0]
        or any(segment == "" for segment in segments)
    ):
        return None
    return destination


def _source_relative_name(name: str, source_relpath: Optional[str]) -> Optional[str]:
    """Lexically collapse an inline path against its source inside the vault root.

    This operates only on POSIX path segments; it performs no filesystem or
    symlink traversal. A ``..`` that would pop above the vault-relative root is
    refused before candidate matching. Existing same-scope candidate filtering
    remains authoritative, including links across configured mapping folders.
    """
    if (
        source_relpath is None
        or "\\" in source_relpath
        or _relative_markdown_destination(name) is None
    ):
        return None

    # Keep this parser module side-effect free at import time. The canonical
    # boundary normalizer is needed only during source-aware resolution.
    from cli_agent_orchestrator.services.vault.boundary import normalize_relpath

    try:
        normalized_source = normalize_relpath(source_relpath)
    except ValueError:
        return None
    if not normalized_source.lower().endswith(".md"):
        return None

    collapsed = normalized_source.split("/")[:-1]
    for segment in name.split("/"):
        if segment == ".":
            continue
        if segment == "..":
            if not collapsed:
                return None
            collapsed.pop()
            continue
        collapsed.append(segment)
    try:
        return normalize_relpath("/".join(collapsed))
    except ValueError:
        return None


def _is_non_markdown_attachment(name: str) -> bool:
    """Recognize a concrete non-Markdown filename without treating titles as files."""
    filename = name.rsplit("/", 1)[-1]
    return "." in filename and not filename.lower().endswith(".md")
