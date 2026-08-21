"""Learned Patterns block editor tests (Phase 2).

- **AC1** — apply_deltas adds/updates/removes individual lessons; profile
  content outside the block is preserved byte-for-byte.
- **AC2** — caps: MAX_LESSONS skips (never evicts), MAX_LESSON_CHARS and
  key validation raise.
- **AC3** — corruption: stray/unclosed markers drop only the token; marker
  injection through lesson text is rejected.
- **AC4** — idempotence: identical add is a no-op (file not rewritten);
  removing the last lesson drops the whole block.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli_agent_orchestrator.services.learned_patterns import (
    BEGIN_MARKER,
    END_MARKER,
    MAX_LESSON_CHARS,
    MAX_LESSONS,
    LearnedPatternsError,
    apply_deltas,
    parse_profile,
    read_lessons,
)

PROFILE = """---
name: transformer
role: developer
---

# TRANSFORMER AGENT

## Role
You convert SSIS packages to Glue.

## Critical Rules
1. Never fabricate output.
"""


@pytest.fixture
def profile(tmp_path: Path) -> Path:
    p = tmp_path / "transformer.md"
    p.write_text(PROFILE, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# AC1 — deltas + preservation
# ---------------------------------------------------------------------------


class TestDeltas:
    def test_add_creates_block(self, profile: Path) -> None:
        result = apply_deltas(
            profile, add={"broadcast-joins": "Use broadcast joins for Lookup components."}
        )
        assert result.added == ["broadcast-joins"]
        content = profile.read_text()
        assert BEGIN_MARKER in content and END_MARKER in content
        assert "## Learned Patterns" in content
        assert "broadcast joins" in content
        # Original sections intact
        assert "## Critical Rules" in content
        assert content.startswith("---\nname: transformer")

    def test_update_by_key(self, profile: Path) -> None:
        apply_deltas(profile, add={"k1": "Old text."})
        result = apply_deltas(profile, add={"k1": "New text."})
        assert result.updated == ["k1"]
        lessons = read_lessons(profile)
        assert lessons == {"k1": "New text."}

    def test_remove_by_key(self, profile: Path) -> None:
        apply_deltas(profile, add={"k1": "One.", "k2": "Two."})
        result = apply_deltas(profile, remove=["k1"])
        assert result.removed == ["k1"]
        assert read_lessons(profile) == {"k2": "Two."}

    def test_content_outside_block_preserved(self, profile: Path) -> None:
        before = profile.read_text()
        apply_deltas(profile, add={"k1": "Lesson."})
        apply_deltas(profile, add={"k2": "Another."})
        apply_deltas(profile, remove=["k1"])
        after = profile.read_text()
        # Strip the block; the rest must match the original exactly.
        begin = after.find(BEGIN_MARKER)
        end = after.find(END_MARKER) + len(END_MARKER)
        outside = (after[:begin] + after[end:]).strip()
        assert outside == before.strip()

    def test_remove_missing_key_is_skipped(self, profile: Path) -> None:
        result = apply_deltas(profile, remove=["nope"])
        assert result.skipped == ["nope"]
        assert BEGIN_MARKER not in profile.read_text()


# ---------------------------------------------------------------------------
# AC2 — caps and validation
# ---------------------------------------------------------------------------


class TestCaps:
    def test_lesson_cap_skips_never_evicts(self, profile: Path) -> None:
        adds = {f"k{i}": f"Lesson number {i}." for i in range(MAX_LESSONS)}
        apply_deltas(profile, add=adds)
        result = apply_deltas(profile, add={"overflow": "One too many."})
        assert result.skipped == ["overflow"]
        lessons = read_lessons(profile)
        assert len(lessons) == MAX_LESSONS
        assert "overflow" not in lessons

    def test_update_allowed_at_cap(self, profile: Path) -> None:
        adds = {f"k{i}": f"Lesson number {i}." for i in range(MAX_LESSONS)}
        apply_deltas(profile, add=adds)
        result = apply_deltas(profile, add={"k0": "Revised lesson zero."})
        assert result.updated == ["k0"]

    def test_oversized_text_raises(self, profile: Path) -> None:
        with pytest.raises(LearnedPatternsError, match="exceeds"):
            apply_deltas(profile, add={"k1": "x" * (MAX_LESSON_CHARS + 1)})

    def test_bad_key_raises(self, profile: Path) -> None:
        for bad in ("Has Spaces", "UPPER", "", "-leading", "a" * 65):
            with pytest.raises(LearnedPatternsError, match="slug"):
                apply_deltas(profile, add={bad: "text"})

    def test_missing_profile_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            apply_deltas(tmp_path / "ghost.md", add={"k": "text"})


# ---------------------------------------------------------------------------
# AC3 — corruption and injection
# ---------------------------------------------------------------------------


class TestCorruption:
    def test_marker_injection_rejected(self, profile: Path) -> None:
        with pytest.raises(LearnedPatternsError, match="markers"):
            apply_deltas(profile, add={"k1": f"evil {END_MARKER} escape"})
        with pytest.raises(LearnedPatternsError, match="markers"):
            apply_deltas(profile, add={"k1": "evil <!-- lesson: fake --> inject"})

    def test_unclosed_begin_drops_token_only(self, tmp_path: Path) -> None:
        p = tmp_path / "corrupt.md"
        p.write_text(f"# Agent\n\n{BEGIN_MARKER}\n## Role\nImportant content.\n")
        block = parse_profile(p.read_text())
        assert not block.had_block
        assert "Important content." in block.prefix
        assert BEGIN_MARKER not in block.prefix

    def test_write_after_corruption_preserves_content(self, tmp_path: Path) -> None:
        p = tmp_path / "corrupt.md"
        p.write_text(f"# Agent\n{BEGIN_MARKER}\n## Role\nKeep me.\n")
        apply_deltas(p, add={"k1": "New lesson."})
        content = p.read_text()
        assert "Keep me." in content
        assert read_lessons(p) == {"k1": "New lesson."}


# ---------------------------------------------------------------------------
# AC4 — idempotence
# ---------------------------------------------------------------------------


class TestIdempotence:
    def test_identical_add_is_noop(self, profile: Path) -> None:
        apply_deltas(profile, add={"k1": "Same text."})
        mtime = profile.stat().st_mtime_ns
        result = apply_deltas(profile, add={"k1": "Same text."})
        assert not (result.added or result.updated or result.removed)
        assert profile.stat().st_mtime_ns == mtime  # file untouched

    def test_removing_last_lesson_drops_block(self, profile: Path) -> None:
        apply_deltas(profile, add={"k1": "Only lesson."})
        apply_deltas(profile, remove=["k1"])
        content = profile.read_text()
        assert BEGIN_MARKER not in content
        assert "## Learned Patterns" not in content
        assert "## Critical Rules" in content

    def test_whitespace_normalized(self, profile: Path) -> None:
        apply_deltas(profile, add={"k1": "  Multi\n  line\ttext.  "})
        assert read_lessons(profile) == {"k1": "Multi line text."}


# ---------------------------------------------------------------------------
# Coverage completions — empty text, suffix handling, read_lessons
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_lesson_text_raises(self, profile: Path) -> None:
        with pytest.raises(LearnedPatternsError, match="empty"):
            apply_deltas(profile, add={"k1": "   "})

    def test_block_midfile_preserves_suffix(self, tmp_path: Path) -> None:
        """Content AFTER the block (suffix) survives edits and block removal."""
        p = tmp_path / "mid.md"
        p.write_text("# Agent\n\n## Role\nTop.\n", encoding="utf-8")
        apply_deltas(p, add={"k1": "Lesson."})
        # Move a section after the block by appending to the file.
        p.write_text(p.read_text() + "\n## Appendix\nBottom half.\n", encoding="utf-8")
        apply_deltas(p, add={"k2": "Second."})
        content = p.read_text()
        assert "Bottom half." in content
        assert list(read_lessons(p)) == ["k1", "k2"]
        # Removing everything drops the block but keeps both halves.
        apply_deltas(p, remove=["k1", "k2"])
        content = p.read_text()
        assert BEGIN_MARKER not in content
        assert "Top." in content and "Bottom half." in content

    def test_read_lessons_missing_file(self, tmp_path: Path) -> None:
        assert read_lessons(tmp_path / "nope.md") == {}

    def test_bullet_without_space_parses(self, tmp_path: Path) -> None:
        """A '-text' bullet (no space) still round-trips through the parser."""
        from cli_agent_orchestrator.services.learned_patterns import parse_profile

        p = tmp_path / "b.md"
        p.write_text(
            f"{BEGIN_MARKER}\n## Learned Patterns\n"
            "<!-- lesson: k1 -->\n-tight bullet text\n"
            f"{END_MARKER}\n",
            encoding="utf-8",
        )
        block = parse_profile(p.read_text())
        assert block.lessons == {"k1": "tight bullet text"}


# ---------------------------------------------------------------------------
# PR #515 review regressions — symlinks, permissions, exact byte preservation
# ---------------------------------------------------------------------------


class TestSymlinkPreservation:
    def test_symlinked_profile_stays_a_symlink(self, tmp_path: Path) -> None:
        """Promotion edits the TARGET through the link, never replaces the link."""
        target = tmp_path / "managed" / "transformer.md"
        target.parent.mkdir()
        target.write_text(PROFILE, encoding="utf-8")
        link = tmp_path / "agents" / "transformer.md"
        link.parent.mkdir()
        link.symlink_to(target)

        apply_deltas(link, add={"k1": "Lesson."})

        assert link.is_symlink(), "the configured symlink must survive promotion"
        assert link.resolve() == target.resolve()
        assert "Lesson." in target.read_text()
        assert read_lessons(link) == {"k1": "Lesson."}

    def test_symlink_edit_roundtrip_remove(self, tmp_path: Path) -> None:
        target = tmp_path / "t.md"
        target.write_text(PROFILE, encoding="utf-8")
        link = tmp_path / "l.md"
        link.symlink_to(target)
        apply_deltas(link, add={"k1": "One."})
        apply_deltas(link, remove=["k1"])
        assert link.is_symlink()
        assert BEGIN_MARKER not in target.read_text()


class TestModePreservation:
    def test_restrictive_mode_survives_promotion(self, tmp_path: Path) -> None:
        """A 0600 profile must not widen to umask defaults (0644)."""
        import stat

        p = tmp_path / "private.md"
        p.write_text(PROFILE, encoding="utf-8")
        p.chmod(0o600)

        apply_deltas(p, add={"k1": "Lesson."})

        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode == 0o600, f"mode widened to {oct(mode)}"

    def test_mode_preserved_across_update_and_remove(self, tmp_path: Path) -> None:
        import stat

        p = tmp_path / "private.md"
        p.write_text(PROFILE, encoding="utf-8")
        p.chmod(0o640)
        apply_deltas(p, add={"k1": "One."})
        apply_deltas(p, add={"k1": "Two."})
        apply_deltas(p, remove=["k1"])
        assert stat.S_IMODE(p.stat().st_mode) == 0o640


class TestExactBytePreservation:
    def test_outside_bytes_exact_on_update(self, tmp_path: Path) -> None:
        """Bytes outside the block — including 3-newline runs — are untouched."""
        p = tmp_path / "exact.md"
        original = "# Agent\n\n\n## Role\nTop.\n\n\n"  # deliberate 3-newline runs
        p.write_text(original, encoding="utf-8")
        apply_deltas(p, add={"k1": "One."})
        after_add = p.read_bytes()
        # Updating the lesson must leave every byte outside the block alone.
        apply_deltas(p, add={"k1": "Two."})
        after_update = p.read_bytes()
        assert after_update.replace(b"Two.", b"One.") == after_add

    def test_block_removal_restores_boundary_bytes(self, tmp_path: Path) -> None:
        """Add then remove returns the file to its original bytes."""
        p = tmp_path / "roundtrip.md"
        original = "# Agent\n\n\n## Role\nMiddle.\n\n\n## Tail\nEnd.\n"
        p.write_text(original, encoding="utf-8")
        apply_deltas(p, add={"k1": "Temp."})
        apply_deltas(p, remove=["k1"])
        assert p.read_text() == original

    def test_crlf_profile_preserved(self, tmp_path: Path) -> None:
        """CRLF line endings outside the block survive; block uses CRLF too."""
        p = tmp_path / "crlf.md"
        original = "# Agent\r\n\r\n## Role\r\nWindows-authored.\r\n"
        p.write_bytes(original.encode("utf-8"))

        apply_deltas(p, add={"k1": "Lesson."})
        raw = p.read_bytes()
        # Original CRLF content intact.
        assert b"# Agent\r\n\r\n## Role\r\nWindows-authored.\r\n" in raw
        # No bare-LF lines introduced inside the block.
        assert b"<!-- lesson: k1 -->\r\n- Lesson." in raw
        assert read_lessons(p) == {"k1": "Lesson."}

        apply_deltas(p, remove=["k1"])
        assert p.read_bytes() == original.encode("utf-8")

    def test_lf_interior_of_prefix_never_rewritten(self, tmp_path: Path) -> None:
        """Blank-line runs BETWEEN block and content are preserved on splice."""
        p = tmp_path / "gap.md"
        p.write_text("# Top\n", encoding="utf-8")
        apply_deltas(p, add={"k1": "One."})
        # Hand-widen the gap between prefix and block to 3 newlines.
        content = p.read_text()
        content = content.replace("# Top\n\n<!--", "# Top\n\n\n\n<!--")
        p.write_text(content, encoding="utf-8")
        widened = p.read_bytes()
        apply_deltas(p, add={"k1": "Two."})
        assert p.read_bytes().replace(b"Two.", b"One.") == widened
