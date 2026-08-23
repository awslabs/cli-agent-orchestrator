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
class ParseResult:
    frontmatter: dict[str, Any]
    cao: dict[str, Any]
    region: FrontmatterRegion
    finding_code: Optional[FindingCode] = None
    finding_detail: Optional[str] = None


def _quarantined(region: FrontmatterRegion, code: FindingCode, detail: str) -> ParseResult:
    return ParseResult({}, {}, region, code, detail)


def split_frontmatter(text: str, max_frontmatter_bytes: int) -> FrontmatterRegion:
    """Split YAML frontmatter and refuse an over-cap region before YAML sees it."""
    offset = 1 if text.startswith("\ufeff") else 0
    source = text[offset:]
    if not source.startswith("---\n") and source != "---":
        return FrontmatterRegion("", source, offset, offset)
    closing_match = next(
        (match for match in re.finditer(r"(?m)^---(?:\n|\Z)", source[4:])),
        None,
    )
    if closing_match is None:
        raise ValueError("frontmatter_malformed")
    closing = 4 + closing_match.start()
    end = 4 + closing_match.end()
    raw_end = closing - 1 if source[closing - 1] == "\n" else closing
    raw = source[4:raw_end]
    if len(raw.encode("utf-8")) > max_frontmatter_bytes:
        raise ValueError("frontmatter_too_large")
    return FrontmatterRegion(raw, source[end:], offset, offset + end)


def parse_note(text: str, *, max_frontmatter_bytes: int, secret_gate: str) -> ParseResult:
    """Parse text only; invalid frontmatter is represented as a quarantine finding."""
    try:
        region = split_frontmatter(text, max_frontmatter_bytes)
    except ValueError as exc:
        region = FrontmatterRegion("", text, 0, 0)
        if str(exc) == "frontmatter_too_large":
            return _quarantined(region, FindingCode.FRONTMATTER_TOO_LARGE, "frontmatter byte cap")
        return _quarantined(region, FindingCode.FRONTMATTER_MALFORMED, "unterminated frontmatter")
    if not region.raw:
        return ParseResult({}, {}, region)
    try:
        for token in yaml.scan(region.raw, Loader=yaml.SafeLoader):
            if isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken)):
                return _quarantined(region, FindingCode.FRONTMATTER_UNSAFE, "YAML anchor or alias")
        loaded = yaml.safe_load(region.raw)
    except (yaml.YAMLError, RecursionError):
        return _quarantined(region, FindingCode.FRONTMATTER_MALFORMED, "invalid YAML")
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        return _quarantined(region, FindingCode.INVALID_CAO_BLOCK, "frontmatter must be an object")
    cao = loaded.get("cao", {})
    if cao is None:
        cao = {}
    if not isinstance(cao, dict):
        return _quarantined(region, FindingCode.INVALID_CAO_BLOCK, "cao must be an object")
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
    if links is not None and (not isinstance(links, list) or len(links) > MAX_CAO_LINKS):
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
        raise ValueError("cao.links[].to contains unsupported characters or exceeds 256 characters")
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


def classify_secret(body: str, *, secret_gate: str) -> Optional[tuple[FindingCode, str, str]]:
    """Return the content-free secret finding with explicit mapping gate severity."""
    pattern = scan_for_secrets(body)
    if pattern is None:
        return None
    return (
        FindingCode.SECRET_DETECTED,
        finding_severity(FindingCode.SECRET_DETECTED, secret_gate=secret_gate),
        pattern,
    )
