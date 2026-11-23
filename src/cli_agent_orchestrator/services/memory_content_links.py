"""Parse memory-authored links into producer-scoped relationship targets."""

from __future__ import annotations

import re
from typing import Callable

_SEE_ALSO_HEADING = re.compile(r"^##\s+See Also\s*$", re.IGNORECASE)
_MARKDOWN_LINK = re.compile(r"\[[^\]\n]+\]\(([^)\n]+)\)")


def extract_content_link_targets(content: str, sanitize_key: Callable[[str], str]) -> list[str]:
    """Return canonical targets from ``[[wikilinks]]`` and See Also bullets."""
    from cli_agent_orchestrator.services.vault.links import extract_wikilinks

    targets: list[str] = []
    seen: set[str] = set()

    extracted = extract_wikilinks(content)
    for (embed, raw), relative in zip(extracted.links, extracted.relative_paths):
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

    in_see_also = False
    in_fence = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if stripped.startswith("## "):
            in_see_also = bool(_SEE_ALSO_HEADING.fullmatch(stripped))
            continue
        if not in_see_also or not stripped.startswith(("- ", "* ", "+ ")):
            continue
        bullet = stripped[2:].strip()
        match = _MARKDOWN_LINK.search(bullet)
        raw = match.group(1) if match else bullet
        if "://" in raw or raw.lower().startswith("mailto:") or raw.startswith("/"):
            continue
        if not match and sanitize_key(raw) != raw:
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
