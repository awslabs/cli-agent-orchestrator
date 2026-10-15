"""Unit tests for utils/delegation.py, the one comparator for delegation ceilings."""

import pytest

from cli_agent_orchestrator.constants import ROLE_DELEGATE_DEFAULTS, ROLE_TOOL_DEFAULTS
from cli_agent_orchestrator.utils.delegation import (
    DelegationCeilingError,
    bound_child,
    ceiling_from_row,
    describe_rejection,
    intersect_ceiling,
    resolve_delegate_tools,
    tools_exceeding,
    union_tools,
)

SUPERVISOR = ROLE_TOOL_DEFAULTS["supervisor"]
REVIEWER = ROLE_TOOL_DEFAULTS["reviewer"]
DEVELOPER = ROLE_TOOL_DEFAULTS["developer"]
SCOUT = ROLE_TOOL_DEFAULTS["workflow_scout"]


class TestToolsExceeding:
    @pytest.mark.parametrize(
        "child,ceiling,expected",
        [
            (DEVELOPER, ["*"], []),
            (["*"], ["*"], []),
            (None, ["*"], []),
            (None, REVIEWER, ["*"]),
            (["*"], REVIEWER, ["*"]),
            (DEVELOPER, REVIEWER, ["fs_*", "execute_bash", "web_fetch"]),
            (REVIEWER, SUPERVISOR, []),  # @builtin ignored
            (SUPERVISOR, REVIEWER, []),
            (SCOUT, REVIEWER, ["execute_bash"]),
            (["fs_read"], ["fs_*"], []),
            (["fs_*"], ["fs_read"], ["fs_*"]),
            (["fs_*"], ["fs_read", "fs_list", "fs_write"], []),
            (["fs_write", "fs_read"], ["fs_*"], []),
            (["@serply"], REVIEWER, ["@serply"]),
            (["@serply"], [*REVIEWER, "@serply"], []),
            (["discovery"], ["@cao-mcp-server"], ["discovery"]),
            ([], REVIEWER, []),
            (["fs_read", "fs_read"], ["fs_list"], ["fs_read"]),  # deduped
        ],
    )
    def test_table(self, child, ceiling, expected):
        assert tools_exceeding(child, ceiling) == expected

    def test_matches_ephemeral_creator_rule(self):
        """Same answers the ephemeral path's inline comparison gave (fs_*/* expansion)."""
        assert tools_exceeding(["fs_read", "fs_list"], ["fs_*", "@cao-mcp-server"]) == []
        assert tools_exceeding(["execute_bash"], ["fs_*", "@cao-mcp-server"]) == ["execute_bash"]
        assert tools_exceeding(["execute_bash", "web_fetch"], ["*"]) == []


class TestIntersectCeiling:
    def test_star_on_either_side(self):
        assert intersect_ceiling(["*"], REVIEWER) == REVIEWER
        assert intersect_ceiling(DEVELOPER, ["*"]) == DEVELOPER
        assert intersect_ceiling(["*"], ["*"]) == ["*"]

    def test_narrows_to_ceiling(self):
        # developer's fs_* narrows to the filesystem atoms the reviewer ceiling holds;
        # execute_bash and web_fetch drop out.
        assert intersect_ceiling(DEVELOPER, REVIEWER) == [
            "@builtin",
            "fs_read",
            "fs_list",
            "@cao-mcp-server",
        ]
        assert intersect_ceiling(SUPERVISOR, REVIEWER) == SUPERVISOR

    def test_fs_glob_narrows_to_present_atoms(self):
        assert intersect_ceiling(["fs_*", "execute_bash"], ["fs_read", "fs_list"]) == [
            "fs_read",
            "fs_list",
        ]
        assert intersect_ceiling(["fs_*"], ["fs_read", "fs_list", "fs_write"]) == ["fs_*"]
        assert intersect_ceiling(["fs_read"], ["fs_*"]) == ["fs_read"]

    def test_never_wider_than_ceiling(self):
        for declared in (DEVELOPER, SCOUT, ["*"], ["fs_*", "@x"]):
            for ceiling in (REVIEWER, SUPERVISOR, ["fs_read"], []):
                assert tools_exceeding(intersect_ceiling(declared, ceiling), ceiling) == []


class TestResolveDelegateTools:
    def test_role_default_table(self):
        assert ROLE_DELEGATE_DEFAULTS == {"supervisor": ["*"]}
        assert resolve_delegate_tools(None, "supervisor", SUPERVISOR) == ["*"]
        for role in ("reviewer", "developer", "workflow_scout"):
            own = ROLE_TOOL_DEFAULTS[role]
            assert resolve_delegate_tools(None, role, own) == own

    def test_custom_role_named_supervisor_keeps_star(self):
        """The table is keyed by NAME: a settings.json role called supervisor
        with a different list still delegates everything (documented ruling)."""
        assert resolve_delegate_tools(None, "supervisor", ["fs_read"]) == ["*"]

    def test_other_custom_role_defaults_to_own_list(self):
        assert resolve_delegate_tools(None, "data_analyst", ["fs_read", "execute_bash"]) == [
            "fs_read",
            "execute_bash",
        ]

    def test_declared_wins_and_always_includes_own(self):
        assert resolve_delegate_tools(["execute_bash"], "reviewer", REVIEWER) == [
            *REVIEWER,
            "execute_bash",
        ]
        assert resolve_delegate_tools(["*"], "reviewer", REVIEWER) == ["*"]
        assert resolve_delegate_tools(["fs_read"], None, ["fs_read", "fs_list"]) == [
            "fs_read",
            "fs_list",
        ]

    def test_unrestricted_own_list_is_star(self):
        assert resolve_delegate_tools(None, "reviewer", ["*"]) == ["*"]  # --yolo
        assert resolve_delegate_tools(None, None, None) == ["*"]

    def test_union(self):
        assert union_tools(["a"], ["b", "a"]) == ["a", "b"]
        assert union_tools(["a"], ["*"]) == ["*"]


class TestCeilingFromRow:
    def test_recorded_ceiling_wins(self):
        assert ceiling_from_row({"delegate_tools": ["*"], "allowed_tools": REVIEWER}) == ["*"]

    def test_legacy_row_is_bounded_by_its_own_allowlist(self):
        assert ceiling_from_row({"delegate_tools": None, "allowed_tools": REVIEWER}) == REVIEWER

    def test_legacy_row_without_tools_resolves_profile(self, monkeypatch):
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.caller_tools.caller_effective_allowed_tools",
            lambda row: ["fs_read"],
        )
        assert ceiling_from_row({"delegate_tools": None, "allowed_tools": None}) == ["fs_read"]

    def test_unresolvable_is_none(self, monkeypatch):
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.caller_tools.caller_effective_allowed_tools",
            lambda row: None,
        )
        assert ceiling_from_row({"delegate_tools": None, "allowed_tools": None}) is None


class TestBoundChild:
    def test_refusal_is_loud(self):
        with pytest.raises(DelegationCeilingError) as excinfo:
            bound_child(
                child_allowed_tools=DEVELOPER,
                child_declared_ceiling=DEVELOPER,
                caller_ceiling=REVIEWER,
                parent_id="a1b2c3d4",
                parent_profile="reviewer",
                child_profile="developer",
            )
        err = excinfo.value
        assert err.status_code == 403
        assert err.excess == ["fs_*", "execute_bash", "web_fetch"]
        message = str(err)
        for needle in (
            "terminal a1b2c3d4",
            "profile 'reviewer'",
            "'developer'",
            "fs_*, execute_bash, web_fetch",
            "delegateTools",
            "relaunch the parent",
        ):
            assert needle in message, needle

    def test_child_ceiling_is_capped(self):
        # A supervisor-role profile (declared *) under a reviewer gets the reviewer's ceiling.
        assert (
            bound_child(
                child_allowed_tools=SUPERVISOR,
                child_declared_ceiling=["*"],
                caller_ceiling=REVIEWER,
                parent_id="a1b2c3d4",
                parent_profile="reviewer",
                child_profile="sup",
            )
            == REVIEWER
        )

    def test_describe_rejection_without_parent_id(self):
        text = describe_rejection(
            parent_id=None,
            parent_profile=None,
            child_profile="developer",
            excess=["execute_bash"],
            ceiling=REVIEWER,
        )
        assert text.startswith("Delegation refused: the calling terminal may not launch")
