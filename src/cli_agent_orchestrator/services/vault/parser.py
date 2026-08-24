"""Pure, bounded parser for untrusted vault note text."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

import yaml

from cli_agent_orchestrator.models.memory import MemoryType
from cli_agent_orchestrator.models.relationship import (
    VALID_ORIGINS,
    VALID_STATUSES,
    VALID_TYPES,
)
from cli_agent_orchestrator.services.secret_gate import scan_for_secrets
from cli_agent_orchestrator.services.vault.findings import FindingCode, finding_severity
from cli_agent_orchestrator.services.vault.identity import validate_cao_key

MAX_CAO_LINKS = 64
MAX_CAO_LINK_TARGET_CHARS = 256
_CAO_FIELDS = frozenset({"key", "type", "managed", "links"})
_CAO_LINK_FIELDS = frozenset({"to", "type", "status", "origin", "confidence"})


@dataclass(frozen=True)
class FrontmatterRegion:
    raw: str
    body: str
    start: int
    end: int


@dataclass(frozen=True)
class CaoBlockLocations:
    """Exact top-level ``cao`` entry spans in a frontmatter mapping."""

    spans: tuple[tuple[int, int], ...]
    indentation: str


@dataclass(frozen=True)
class ParseResult:
    frontmatter: dict[str, Any]
    cao: dict[str, Any]
    region: FrontmatterRegion
    finding_code: Optional[FindingCode] = None
    finding_detail: Optional[str] = None


def _quarantined(
    region: FrontmatterRegion, code: FindingCode, detail: str
) -> ParseResult:
    return ParseResult({}, {}, region, code, detail)


def frontmatter_boundary(text: str) -> Optional[tuple[FrontmatterRegion, str]]:
    """Return the raw frontmatter region and its fence newline, if present.

    A leading BOM is ignored only for fence recognition and remains outside the
    returned region.  The closing fence must occupy a complete line, matching
    the parser's existing boundary rule rather than accepting ``---suffix``.
    """
    offset = 1 if text.startswith("\ufeff") else 0
    source = text[offset:]
    newline = "\r\n" if source.startswith("---\r\n") else "\n"
    opening = f"---{newline}"
    if not source.startswith(opening):
        return None
    closing_pattern = (
        r"(?m)^---(?:\r\n|\Z)" if newline == "\r\n" else r"(?m)^---(?:\n|\Z)"
    )
    closing_match = next(
        (match for match in re.finditer(closing_pattern, source[len(opening) :])),
        None,
    )
    if closing_match is None:
        raise ValueError("frontmatter_malformed")
    closing = len(opening) + closing_match.start()
    end = len(opening) + closing_match.end()
    raw_end = (
        closing - len(newline)
        if source[closing - len(newline) : closing] == newline
        else closing
    )
    raw = source[len(opening) : raw_end]
    return FrontmatterRegion(raw, source[end:], offset, offset + end), newline


def split_frontmatter(text: str, max_frontmatter_bytes: int) -> FrontmatterRegion:
    """Split YAML frontmatter and refuse an over-cap region before YAML sees it."""
    boundary = frontmatter_boundary(text)
    if boundary is None:
        offset = 1 if text.startswith("\ufeff") else 0
        return FrontmatterRegion("", text[offset:], offset, offset)
    region, _newline = boundary
    raw = region.raw
    if len(raw.encode("utf-8")) > max_frontmatter_bytes:
        raise ValueError("frontmatter_too_large")
    return region


def locate_top_level_cao_blocks(raw: str) -> CaoBlockLocations:
    """Locate semantic top-level ``cao`` entries without interpreting text lines.

    The token check mirrors ``parse_note`` before this second YAML load path can
    compose an anchored document.  The returned indentation lets writers append
    a replacement entry to uniformly indented mappings without changing shape.
    """
    try:
        if _has_yaml_anchor_or_alias(raw):
            raise ValueError("frontmatter_unsafe")
        document = yaml.compose(raw, Loader=yaml.SafeLoader)
    except (yaml.YAMLError, RecursionError) as exc:
        raise ValueError("frontmatter_malformed") from exc
    if not isinstance(document, yaml.MappingNode):
        return CaoBlockLocations((), "")

    indentation = _line_indentation(raw, document.start_mark.index)
    spans = tuple(
        _mapping_entry_span(raw, key, value)
        for key, value in document.value
        if isinstance(key, yaml.ScalarNode) and key.value == "cao"
    )
    return CaoBlockLocations(spans, indentation)


def _has_yaml_anchor_or_alias(raw: str) -> bool:
    return any(
        isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken))
        for token in yaml.scan(raw, Loader=yaml.SafeLoader)
    )


def _line_indentation(raw: str, index: int) -> str:
    line_start = raw.rfind("\n", 0, index) + 1
    return raw[line_start:index]


def _mapping_entry_span(raw: str, key: yaml.Node, value: yaml.Node) -> tuple[int, int]:
    """Return the full source span for one mapping entry, including ``?`` syntax."""
    start = key.start_mark.index
    line_start = raw.rfind("\n", 0, start) + 1
    prefix = raw[line_start:start]
    if prefix.lstrip(" \t").startswith("?"):
        start = line_start
    end = value.end_mark.index
    # PyYAML includes trailing document blank lines in a final mapping value's
    # end mark. Retain those separators; only the entry's final newline belongs
    # to the span being excised.
    if raw[:end].endswith("\r\n\r\n"):
        end -= len("\r\n")
    elif raw[:end].endswith("\n\n"):
        end -= len("\n")
    return start, end


def parse_note(
    text: str, *, max_frontmatter_bytes: int, secret_gate: str
) -> ParseResult:
    """Parse text only; invalid frontmatter is represented as a quarantine finding."""
    try:
        region = split_frontmatter(text, max_frontmatter_bytes)
    except ValueError as exc:
        region = FrontmatterRegion("", text, 0, 0)
        if str(exc) == "frontmatter_too_large":
            return _quarantined(
                region, FindingCode.FRONTMATTER_TOO_LARGE, "frontmatter byte cap"
            )
        return _quarantined(
            region, FindingCode.FRONTMATTER_MALFORMED, "unterminated frontmatter"
        )
    if not region.raw:
        return ParseResult({}, {}, region)
    try:
        if _has_yaml_anchor_or_alias(region.raw):
            return _quarantined(
                region, FindingCode.FRONTMATTER_UNSAFE, "YAML anchor or alias"
            )
        loaded = yaml.safe_load(region.raw)
    except (yaml.YAMLError, RecursionError):
        return _quarantined(region, FindingCode.FRONTMATTER_MALFORMED, "invalid YAML")
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        return _quarantined(
            region, FindingCode.INVALID_CAO_BLOCK, "frontmatter must be an object"
        )
    cao = loaded.get("cao", {})
    if cao is None:
        cao = {}
    if not isinstance(cao, dict):
        return _quarantined(
            region, FindingCode.INVALID_CAO_BLOCK, "cao must be an object"
        )
    try:
        _validate_cao(cao)
    except ValueError as exc:
        code = (
            FindingCode.KEY_INVALID
            if str(exc).startswith("cao.key")
            else FindingCode.INVALID_CAO_BLOCK
        )
        return _quarantined(region, code, str(exc))
    return ParseResult(loaded, cao, region)


def _validate_cao(cao: dict[str, Any]) -> None:
    unknown = set(cao) - _CAO_FIELDS
    if unknown:
        raise ValueError("cao contains unknown member")
    if "key" in cao:
        validate_cao_key(cao["key"])
    if "type" in cao and cao["type"] not in {item.value for item in MemoryType}:
        raise ValueError("cao.type is invalid")
    if "managed" in cao and not isinstance(cao["managed"], bool):
        raise ValueError("cao.managed must be a boolean")
    links = cao.get("links")
    if links is not None and (
        not isinstance(links, list) or len(links) > MAX_CAO_LINKS
    ):
        raise ValueError("cao.links must be a list of at most 64 objects")
    for link in links or ():
        _validate_cao_link(link)


def _validate_cao_link(link: Any) -> None:
    if not isinstance(link, dict):
        raise ValueError("cao.links must contain only objects")
    unknown = set(link) - _CAO_LINK_FIELDS
    if unknown:
        raise ValueError("cao.links contains unknown member")
    if not isinstance(link.get("to"), str) or not link["to"].strip():
        raise ValueError("cao.links[].to must be a non-empty string")
    if len(link["to"]) > MAX_CAO_LINK_TARGET_CHARS or any(
        ord(character) < 32 or ord(character) == 127 for character in link["to"]
    ):
        raise ValueError(
            "cao.links[].to contains unsupported characters or exceeds 256 characters"
        )
    for key, values in (
        ("type", VALID_TYPES),
        ("status", VALID_STATUSES),
        ("origin", VALID_ORIGINS),
    ):
        if key in link and link[key] not in values:
            raise ValueError(f"cao.links[].{key} is invalid")
    if "confidence" in link and (
        isinstance(link["confidence"], bool)
        or not isinstance(link["confidence"], (int, float))
        or not 0 <= link["confidence"] <= 1
    ):
        raise ValueError("cao.links[].confidence must be between 0 and 1")


def classify_secret(
    body: str, *, secret_gate: str
) -> Optional[tuple[FindingCode, str, str]]:
    """Return the content-free secret finding with explicit mapping gate severity."""
    pattern = scan_for_secrets(body)
    if pattern is None:
        return None
    return (
        FindingCode.SECRET_DETECTED,
        finding_severity(FindingCode.SECRET_DETECTED, secret_gate=secret_gate),
        pattern,
    )
