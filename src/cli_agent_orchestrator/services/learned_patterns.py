"""Learned Patterns block editor for agent profile markdown (Phase 2).

Maintains a delimited ``## Learned Patterns`` section inside an agent
profile ``.md`` file via **itemized delta operations** (add / update /
remove of individual lessons) — never whole-block rewrites. Incremental
deltas are the ACE (Agentic Context Engineering) discipline: monolithic
rewrites suffer brevity bias (compressing away detail) and context
collapse (iterative erosion), so each lesson is an addressable line item
keyed by a slug.

Block format inside the profile file::

    <!-- cao-learned:begin -->
    ## Learned Patterns
    <!-- lesson: prefer-broadcast-join -->
    - Prefer broadcast joins for SSIS Lookup with partial cache. Applies
      when: converting Lookup components.
    <!-- lesson: check-odbc-spellings -->
    - ...
    <!-- cao-learned:end -->

Safety invariants (mirroring plugins/builtin/claude_code_memory.py, the
delimited-block precedent):
- Only content between the markers is ever touched; the rest of the
  profile file is preserved byte-for-byte.
- Writes are atomic (temp file + os.replace).
- Stray/unclosed markers are treated as corruption: the marker token is
  dropped, user content is never deleted.
- Caps bound the block: max lessons, per-lesson chars — a runaway
  promoter cannot bloat a profile.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

BEGIN_MARKER = "<!-- cao-learned:begin -->"
END_MARKER = "<!-- cao-learned:end -->"
SECTION_HEADING = "## Learned Patterns"
LESSON_MARKER_RE = re.compile(r"<!-- lesson: ([a-z0-9][a-z0-9-]{0,63}) -->")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

MAX_LESSONS = 10
MAX_LESSON_CHARS = 400


@dataclass
class LearnedBlock:
    """Parsed view of a profile's Learned Patterns block."""

    lessons: Dict[str, str] = field(default_factory=dict)  # key -> text, insertion-ordered
    # Profile content before/after the block, block excluded.
    prefix: str = ""
    suffix: str = ""
    had_block: bool = False


class LearnedPatternsError(ValueError):
    """Raised on invalid keys, oversized lessons, or cap violations."""


def _validate_key(key: str) -> str:
    key = (key or "").strip()
    if not KEY_RE.match(key):
        raise LearnedPatternsError(
            f"lesson key {key!r} must be a lowercase slug (a-z, 0-9, hyphen; max 64 chars)"
        )
    return key


def _validate_text(text: str) -> str:
    text = " ".join((text or "").split())  # collapse whitespace/newlines
    if not text:
        raise LearnedPatternsError("lesson text must not be empty")
    if len(text) > MAX_LESSON_CHARS:
        raise LearnedPatternsError(
            f"lesson text exceeds {MAX_LESSON_CHARS} chars ({len(text)}); "
            "lessons are 1-2 sentence conclusions, not essays"
        )
    if BEGIN_MARKER in text or END_MARKER in text or "<!-- lesson:" in text:
        raise LearnedPatternsError("lesson text must not contain block markers")
    return text


def parse_profile(content: str) -> LearnedBlock:
    """Extract the learned block (if any) from profile file content.

    ``prefix`` and ``suffix`` are RAW slices of everything before/after the
    marker-delimited range — no whitespace normalization — so a rewrite can
    splice a new block into exactly the bytes the old one occupied and leave
    the rest of the file untouched (byte-for-byte contract).

    Stray/unclosed BEGIN markers are dropped (token only), matching the
    corruption handling in claude_code_memory._strip_existing_block.
    """
    block = LearnedBlock()
    begin = content.find(BEGIN_MARKER)
    if begin == -1:
        block.prefix = content
        return block
    end = content.find(END_MARKER, begin + len(BEGIN_MARKER))
    if end == -1:
        # Unclosed BEGIN: drop the marker token, keep everything else.
        logger.warning("learned_patterns: unclosed begin marker; dropping token")
        block.prefix = content[:begin] + content[begin + len(BEGIN_MARKER) :]
        return block

    block.had_block = True
    block.prefix = content[:begin]
    block.suffix = content[end + len(END_MARKER) :]

    body = content[begin + len(BEGIN_MARKER) : end]
    # Parse "<!-- lesson: key -->" followed by its text (up to the next
    # lesson marker). The section heading line is regenerated on render.
    matches = list(LESSON_MARKER_RE.finditer(body))
    for i, m in enumerate(matches):
        start = m.end()
        stop = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        text = body[start:stop]
        # Strip the "- " bullet and collapse whitespace.
        text = " ".join(text.split())
        if text.startswith("- "):
            text = text[2:]
        elif text.startswith("-"):
            text = text[1:].lstrip()
        if text:
            block.lessons[m.group(1)] = text
    return block


def render_block(lessons: Dict[str, str], newline: str = "\n") -> str:
    """Render the delimited block from an ordered lesson mapping.

    ``newline`` lets the caller match the host file's line-ending style so a
    CRLF profile does not end up with a mixed-ending block.
    """
    lines = [BEGIN_MARKER, SECTION_HEADING]
    lines.append(
        "<!-- Maintained by CAO instruction promotion. Edit via "
        "`cao memory promote`; manual edits inside this block may be overwritten. -->"
    )
    for key, text in lessons.items():
        lines.append(f"<!-- lesson: {key} -->")
        lines.append(f"- {text}")
    lines.append(END_MARKER)
    return newline.join(lines)


@dataclass
class DeltaResult:
    """Outcome of applying deltas — content-free counts plus final keys."""

    added: List[str] = field(default_factory=list)
    updated: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)  # no-op removes / cap-skipped adds


def apply_deltas(
    profile_path: Path,
    *,
    add: Optional[Dict[str, str]] = None,
    remove: Optional[List[str]] = None,
) -> DeltaResult:
    """Apply itemized lesson deltas to a profile file's learned block.

    ``add`` upserts lessons by key (add or update); ``remove`` deletes by
    key. Adds beyond ``MAX_LESSONS`` are skipped (oldest lessons are never
    silently evicted — removal is always an explicit delta) and reported in
    ``result.skipped``.

    Preservation contract: only the marker-delimited byte range changes.
    Bytes outside the block — including blank-line runs at the boundaries
    and CRLF line endings — are preserved exactly. A symlinked profile is
    edited through the link (the target file is rewritten; the link
    remains), and the target's permission mode survives the atomic replace.

    The file must exist — promotion never creates profile files. Raises
    ``LearnedPatternsError`` on invalid input, ``FileNotFoundError`` when
    the profile is missing.
    """
    if not profile_path.is_file():
        raise FileNotFoundError(f"profile file not found: {profile_path}")

    # Edit the symlink TARGET, not the link: os.replace() on the link path
    # would delete the link and orphan its managed source (review finding,
    # PR #515). resolve() also pins the path against rename races between
    # read and replace.
    real_path = profile_path.resolve()

    add = {_validate_key(k): _validate_text(v) for k, v in (add or {}).items()}
    remove_keys = [_validate_key(k) for k in (remove or [])]

    # newline="" disables universal-newline translation so CRLF content
    # round-trips byte-identically.
    with open(real_path, "r", encoding="utf-8", newline="") as f:
        content = f.read()
    block = parse_profile(content)
    result = DeltaResult()

    for key in remove_keys:
        if key in block.lessons:
            del block.lessons[key]
            result.removed.append(key)
        else:
            result.skipped.append(key)

    for key, text in add.items():
        if key in block.lessons:
            if block.lessons[key] != text:
                block.lessons[key] = text
                result.updated.append(key)
            # identical text → silent no-op, not even "updated"
        elif len(block.lessons) >= MAX_LESSONS:
            logger.warning(
                "learned_patterns: %s at %d-lesson cap; skipping add of %r",
                profile_path.name,
                MAX_LESSONS,
                key,
            )
            result.skipped.append(key)
        else:
            block.lessons[key] = text
            result.added.append(key)

    if not (result.added or result.updated or result.removed):
        return result  # nothing changed — do not rewrite the file

    # Match the host file's dominant line-ending style for the block itself.
    newline = (
        "\r\n" if content.count("\r\n") > content.count("\n") - content.count("\r\n") else "\n"
    )

    if block.lessons:
        rendered = render_block(block.lessons, newline=newline)
        if block.had_block:
            # Splice the new block into exactly the byte range the old one
            # occupied — prefix and suffix are raw slices, untouched.
            new_content = block.prefix + rendered + block.suffix
        else:
            # First promotion: append the block at the end. Only the join
            # seam is new; the original content bytes are preserved.
            sep = "" if (not block.prefix or block.prefix.endswith(newline)) else newline
            new_content = block.prefix + sep + newline + rendered + newline
    else:
        # Last lesson removed → drop the whole block, absorbing the seam the
        # append path created around it (one trailing newline after the
        # block, one blank line before it) so add→remove round-trips to the
        # original bytes. Everything else in prefix/suffix stays exact.
        prefix, suffix = block.prefix, block.suffix
        if suffix.startswith(newline):
            suffix = suffix[len(newline) :]
        if prefix.endswith(newline * 2):
            prefix = prefix[: -len(newline)]
        new_content = prefix + suffix

    # Atomic temp-file + replace on the RESOLVED target (same idiom as
    # claude_code_memory), preserving the original file's permission mode —
    # a fresh temp file would otherwise take the process umask and widen a
    # 0600 profile to 0644 (review finding, PR #515).
    original_mode = real_path.stat().st_mode
    temp_path = real_path.with_suffix(real_path.suffix + ".tmp")
    try:
        with open(temp_path, "w", encoding="utf-8", newline="") as f:
            f.write(new_content)
        os.chmod(temp_path, original_mode)
        os.replace(temp_path, real_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    return result


def read_lessons(profile_path: Path) -> Dict[str, str]:
    """Return the current lessons in a profile file (empty when none)."""
    if not profile_path.is_file():
        return {}
    return parse_profile(profile_path.read_text(encoding="utf-8")).lessons
