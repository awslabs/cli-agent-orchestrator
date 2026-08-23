"""Pure Obsidian wikilink extraction and conservative resolution."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Optional

from cli_agent_orchestrator.services.vault.findings import FindingCode

MAX_BODY_WIKILINKS = 1000
MAX_LINK_TARGET_CHARS = 256
_WIKILINK = re.compile(r"(?P<embed>!)?\[\[(?P<target>[^\]\r\n]+)\]\]")
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


def extract_wikilinks(text: str) -> LinkExtraction:
    """Extract up to 1000 links outside fenced and inline code."""
    prose = _INLINE_CODE.sub("", _FENCED_CODE.sub("", text))
    matches = tuple(
        (bool(match.group("embed")), match.group("target")) for match in _WIKILINK.finditer(prose)
    )
    findings = (FindingCode.LINK_LIMIT_EXCEEDED,) if len(matches) > MAX_BODY_WIKILINKS else ()
    return LinkExtraction(matches[:MAX_BODY_WIKILINKS], findings)


def resolve_wikilink(
    raw_target: str, *, embed: bool, candidates: tuple[LinkCandidate, ...]
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
    matching = tuple(candidate for candidate in candidates if _matches(name, candidate))
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


def _matches(name: str, candidate: LinkCandidate) -> bool:
    plain = candidate.relpath[:-3] if candidate.relpath.endswith(".md") else candidate.relpath
    basename = plain.rsplit("/", 1)[-1]
    return name in (plain, candidate.relpath, basename) or name in candidate.aliases


def _is_non_markdown_attachment(name: str) -> bool:
    """Recognize a concrete non-Markdown filename without treating titles as files."""
    filename = name.rsplit("/", 1)[-1]
    return "." in filename and not filename.lower().endswith(".md")
