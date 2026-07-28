"""The websocket resize frame's validation and clamping (§6.6 hardening).

The resize frame is accepted viewer geometry — it reflows the bound TUI
and carries no keystroke content, so it is unfiltered for managed panes.
What it must not be is unbounded or untyped: a wild dimension could
balloon the pty, and a non-integer one used to tear the viewer websocket
down on a ``struct.pack`` TypeError with no reason the client could act
on.  These pin the helper's three answers: clamp in-range, default when
absent, reject when malformed.
"""

from __future__ import annotations

from cli_agent_orchestrator.api.main import (
    _WS_RESIZE_MAX_COLS,
    _WS_RESIZE_MAX_ROWS,
    _web_resize_dimensions,
)


class TestResizeDimensions:
    def test_absent_dimensions_keep_the_deployed_defaults(self):
        assert _web_resize_dimensions({}) == (24, 80)

    def test_in_range_dimensions_pass_through(self):
        assert _web_resize_dimensions({"rows": 50, "cols": 132}) == (50, 132)

    def test_out_of_range_dimensions_clamp_to_the_bound(self):
        assert _web_resize_dimensions({"rows": 10_000, "cols": 10_000}) == (
            _WS_RESIZE_MAX_ROWS,
            _WS_RESIZE_MAX_COLS,
        )
        # And the floor: zero and negative sizes are not a viewport.
        assert _web_resize_dimensions({"rows": 0, "cols": -4}) == (1, 1)

    def test_the_bound_is_the_pinned_one(self):
        """Positive, at most 500 columns by 200 rows (§6.6)."""
        assert (_WS_RESIZE_MAX_COLS, _WS_RESIZE_MAX_ROWS) == (500, 200)

    def test_non_integer_dimensions_are_malformed(self):
        for bad in ("80", 80.5, [80], {"v": 80}, None, True, False):
            assert _web_resize_dimensions({"rows": 24, "cols": bad}) is None
            assert _web_resize_dimensions({"rows": bad, "cols": 80}) is None

    def test_a_bool_is_not_a_size(self):
        # bool is an int subclass; ``True`` is not 1 row.
        assert _web_resize_dimensions({"rows": True, "cols": 80}) is None
