"""Unit tests for strip_terminal_escapes."""

from cli_agent_orchestrator.utils.text import strip_terminal_escapes


class TestStripTerminalEscapes:
    """Tests for terminal escape / control-sequence stripping."""

    def test_strips_sgr_color_codes(self):
        assert strip_terminal_escapes("\x1b[38;5;246mhello\x1b[39m") == "hello"

    def test_column_1_cha_becomes_newline(self):
        # \x1b[1G and \x1b[G (CHA to column 1) start a new logical line.
        assert strip_terminal_escapes("first\x1b[1Gsecond") == "first\nsecond"
        assert strip_terminal_escapes("first\x1b[Gsecond") == "first\nsecond"

    def test_carriage_return_becomes_newline(self):
        assert strip_terminal_escapes("a\r\nb\rc") == "a\nb\nc"

    def test_forward_column_move_becomes_space(self):
        """CHA to column > 1 lays out spaced words without literal spaces; it must
        become a space so the words don't glue together (regression: the newest
        Claude Code TUI renders "✻\\x1b[3GWorked\\x1b[10Gfor\\x1b[14G3s")."""
        assert strip_terminal_escapes("✻\x1b[3GWorked\x1b[10Gfor\x1b[14G3s") == "✻ Worked for 3s"

    def test_cursor_forward_becomes_space(self):
        # \x1b[<n>C (CUF, cursor forward) is also same-line horizontal spacing.
        assert strip_terminal_escapes("a\x1b[5Cb") == "a b"

    def test_column_move_does_not_eat_following_text(self):
        # A spinner glyph positioned with CHA keeps its separating space.
        assert strip_terminal_escapes("✢\x1b[3GCultivating…") == "✢ Cultivating…"
