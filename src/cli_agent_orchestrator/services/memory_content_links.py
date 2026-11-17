"""Parse memory-authored links into producer-scoped relationship targets."""

from __future__ import annotations

import re
from typing import Callable

_SEE_ALSO_HEADING = re.compile(r"## See Also")
_MARKDOWN_LINK = re.compile(r"\[[^\[\]\n]+\]\(([^)\n]+)\)")
_SECTION_END = re.compile(r"^#{1,2}\s+")
_WIKILINK = re.compile(r"!?\[\[[^\]\n]*\]\]")


def strip_content_links(content: str) -> str:
    """Return ``content`` without its link markup or ``## See Also`` section.

    A link names its target, so leaving it in would make any short note look
    like a restatement of every note it cites (#1335 change 5).
    """
    kept: list[str] = []
    lines = content.splitlines()
    see_also_lines = {
        index for start, end, _ in see_also_sections(lines) for index in range(start, end)
    }
    for index, line in enumerate(lines):
        if index in see_also_lines:
            continue
        kept.append(_MARKDOWN_LINK.sub(" ", _WIKILINK.sub(" ", line)))
    return "\n".join(kept)


def see_also_sections(lines: list[str]) -> list[tuple[int, int, set[int]]]:
    """Return section bounds and unfenced body lines, shared by readers and writers."""
    sections: list[tuple[int, int, set[int]]] = []
    start: int | None = None
    body: set[int] = set()
    fence: str | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            fence = None if fence == marker else marker if fence is None else fence
            continue
        if fence is not None:
            continue
        if _SECTION_END.match(stripped):
            if start is not None:
                sections.append((start, index, body))
                start = None
                body = set()
            if _SEE_ALSO_HEADING.fullmatch(line):
                start = index
        elif start is not None:
            body.add(index)
    if start is not None:
        sections.append((start, len(lines), body))
    return sections


def extract_content_link_targets(
    content: str,
    sanitize_key: Callable[[str], str],
    scope: str | None = None,
    *,
    scope_id: str | None = None,
    cross_scope_only: bool = False,
) -> list[str]:
    """Return canonical targets from ``[[wikilinks]]`` and See Also bullets."""
    from cli_agent_orchestrator.services.vault.links import extract_wikilinks

    targets: list[str] = []
    seen: set[str] = set()

    extracted = extract_wikilinks(content)
    for (embed, raw), relative in zip(extracted.links, extracted.relative_paths):
        if cross_scope_only:
            continue
        if embed or relative or raw.lstrip().startswith(("./", "../", "/")):
            continue
        target = raw.split("|", 1)[0].split("#", 1)[0].strip()
        try:
            canonical = sanitize_key(target)
        except ValueError:
            continue
        if canonical not in seen:
            seen.add(canonical)
            targets.append(canonical)

    lines = content.splitlines()
    see_also_lines = {index for _, _, body in see_also_sections(lines) for index in body}
    for index, line in enumerate(lines):
        stripped = line.strip()
        if index not in see_also_lines or not stripped.startswith(("- ", "* ", "+ ")):
            continue
        bullet = stripped[2:].strip()
        match = _MARKDOWN_LINK.search(bullet)
        raw = match.group(1) if match else bullet
        raw = raw.split(" ", 1)[0] if match else raw
        if "://" in raw or raw.lower().startswith("mailto:") or raw.startswith("/"):
            continue
        parts = raw.split("/")
        crosses_scope = (
            scope is not None
            and len(parts) >= 3
            and parts[0] == ".."
            and parts[1] not in {scope, scope_id or scope}
        )
        if crosses_scope != cross_scope_only:
            continue
        if not match:
            try:
                if sanitize_key(raw) != raw:
                    continue
            except ValueError:
                continue
        raw = raw.split("#", 1)[0].rstrip("/")
        if raw.lower().endswith(".md"):
            raw = raw[:-3]
        raw = raw.rsplit("/", 1)[-1].strip()
        try:
            canonical = sanitize_key(raw)
        except ValueError:
            continue
        if canonical not in seen:
            seen.add(canonical)
            targets.append(canonical)
    return targets
