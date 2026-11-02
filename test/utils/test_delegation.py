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

    @pytest.mark.parametrize(
        "child,ceiling,expected",
        [
            (["@plugin-search"], ["@plugin-*"], []),  # Review B F7: glob grant covers the name
            (["@plugin-search"], ["@plugin-s*"], []),
            (["@plugin-search"], ["@plugin-x*"], ["@plugin-search"]),
            (["@plugin-search"], ["@PLUGIN-*"], ["@plugin-search"]),  # case-sensitive
            (["@plugin-*"], ["@*"], []),  # a broader pattern covers a narrower one
            (["@*"], ["@plugin-*"], ["@*"]),  # never the other way round
            (["@plugin-*"], ["@plugin-*"], []),
            (["@plugin-*"], ["@plugin-search"], ["@plugin-*"]),  # a name does not cover a glob
            (["@cao-mcp-server/assign"], ["@cao-mcp-server"], []),  # server grant covers its tool
            (["@cao-mcp-server/assign"], ["@cao-mcp-server/assign"], []),
            (["@cao-mcp-server/assign"], ["@cao-mcp-server/hand*"], ["@cao-mcp-server/assign"]),
            (
                ["@cao-mcp-server"],
                ["@cao-mcp-server/assign"],
                ["@cao-mcp-server"],
            ),  # tool grant is narrower
            (["@srv[1]"], ["@srv[1]"], []),  # exact match survives glob syntax
            (["@builtin", "@plugin-search"], ["@plugin-*"], []),
            (["@plugin-search"], ["*"], []),
            (["@"], ["@*"], ["@"]),  # not a reference at all
        ],
    )
    def test_mcp_reference_semantics_match_the_resolver(self, child, ceiling, expected):
        """Same answers as granted_mcp_servers gives when expanding concrete names."""
        from cli_agent_orchestrator.utils.tool_mapping import granted_mcp_servers

        assert tools_exceeding(child, ceiling) == expected
        name = child[0][1:].split("/")[0]
        if (
            len(child) == 1
            and "/" not in child[0]
            and "*" not in child[0]
            and "[" not in child[0]
            and name
        ):
            resolver_grants = name in granted_mcp_servers(ceiling, [name])
            assert resolver_grants == (expected == []), "comparator and resolver disagree"

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

    def test_mcp_glob_in_ceiling_keeps_matching_declared_servers(self):
        assert intersect_ceiling(
            ["@plugin-search", "@other", "fs_read"], ["@plugin-*", "fs_read"]
        ) == [
            "@plugin-search",
            "fs_read",
        ]

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

    def test_double_null_row_has_no_authority_and_never_reads_the_profile(self, monkeypatch):
        """Review B F6: a row recording neither column must not resolve the live
        profile, which is mutable; it is None and the caller fails closed."""

        def _boom(_row):
            raise AssertionError("profile must not be consulted for a stored ceiling")

        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.caller_tools.caller_effective_allowed_tools", _boom
        )
        row = {"delegate_tools": None, "allowed_tools": None, "agent_profile": "legacy"}
        assert ceiling_from_row(row) is None


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


class TestPatternShapedRequests:
    """Review B round 3, R1: a requested reference that is itself a pattern is a
    language, not a name. It is covered only by an identical grant or a bare ``*``
    grant; matching it as literal text let ``@plugin-?`` admit ``@plugin-*``."""

    @pytest.mark.parametrize(
        "grant,requested,covered",
        [
            ("@plugin-?", "@plugin-*", False),
            ("@a?c", "@a*c", False),
            ("@plugin-[?]", "@plugin-?", False),
            ("@srv/read_?", "@srv/read_*", False),
            ("@plugin-*", "@*", False),
            ("@plugin-a", "@plugin-*", False),
            ("@*", "@plugin-*", True),  # bare * grant covers every language
            ("@plugin-*", "@plugin-*", True),  # identical text
            ("@srv/*", "@srv/read_*", True),  # bare * tool grant
            ("@srv/read_*", "@srv/read_*", True),
            ("@srv", "@srv/read_*", True),  # whole-server grant covers any tool pattern
            ("@plugin-*", "@plugin-a", True),  # concrete names still match the grant pattern
            ("@plugin-?", "@plugin-a", True),
        ],
    )
    def test_pattern_inclusion_rule(self, grant, requested, covered):
        from cli_agent_orchestrator.utils.tool_mapping import mcp_reference_covers

        assert mcp_reference_covers(grant, requested) is covered

    def test_three_generations_cannot_widen_through_a_glob_child(self):
        parent = ["fs_read", "@plugin-?"]
        assert tools_exceeding(["fs_read", "@plugin-*"], parent) == ["@plugin-*"]
        assert intersect_ceiling(["@plugin-*"], parent) == []
        assert tools_exceeding(["@plugin-shell"], parent) == ["@plugin-shell"]
