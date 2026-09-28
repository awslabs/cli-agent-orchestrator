"""Tests for the `cao memory relationships` CLI subgroup (issue #511).

The subgroup shipped with NO tests (human review, PR #524). These cover each
subcommand's contract: what it passes to the service, what it renders, and how it
reports the failure modes an operator will actually hit (unknown id, a service
ValueError). The service is mocked — this is the CLI's own logic under test, the
same isolation the sibling memory CLI tests use.
"""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.memory import (
    relationships_inspect,
    relationships_list,
    relationships_promote,
    relationships_reject,
)


def _dto(
    id="11111111-1111-4111-8111-111111111111",
    source_key="a",
    target_key="b",
    type="relates_to",
    origin="compiler",
    status="active",
    stale=False,
):
    """A stand-in RelationshipDTO: MagicMock would render its repr in the table,
    so the attributes the renderer reads are set explicitly."""
    d = MagicMock()
    d.id = id
    d.source_key = source_key
    d.target_key = target_key
    d.type = type
    d.origin = origin
    d.status = status
    d.stale = stale
    d.to_dict.return_value = {
        "id": id,
        "source_key": source_key,
        "target_key": target_key,
        "type": type,
        "origin": origin,
        "status": status,
        "stale": stale,
    }
    return d


def _patched(rsvc, scope_id="proj1"):
    """Patch the CLI's service accessors; returns the patch context managers."""
    msvc = MagicMock()
    return (
        patch(
            "cli_agent_orchestrator.cli.commands.memory._relationship_service", return_value=rsvc
        ),
        patch("cli_agent_orchestrator.cli.commands.memory._get_memory_service", return_value=msvc),
        patch(
            "cli_agent_orchestrator.cli.commands.memory._resolve_cli_scope_id",
            return_value=scope_id,
        ),
    )


class TestRelationshipsList:
    def test_renders_a_table_row_per_edge(self):
        rsvc = MagicMock()
        rsvc.list_relationships.return_value = [_dto(), _dto(id="2", target_key="c", stale=True)]
        p1, p2, p3 = _patched(rsvc)
        with p1, p2, p3:
            result = CliRunner().invoke(relationships_list, [])
        assert result.exit_code == 0, result.output
        assert "SOURCE -> TARGET" in result.output
        assert "a -> b" in result.output
        assert "a -> c" in result.output
        # The stale column is rendered per row, not as a constant.
        assert "yes" in result.output and "no" in result.output

    def test_empty_result_is_reported_not_an_empty_table(self):
        rsvc = MagicMock()
        rsvc.list_relationships.return_value = []
        p1, p2, p3 = _patched(rsvc)
        with p1, p2, p3:
            result = CliRunner().invoke(relationships_list, [])
        assert result.exit_code == 0
        assert "No relationships found." in result.output
        assert "SOURCE -> TARGET" not in result.output

    def test_status_filter_widens_beyond_active(self):
        """--status must reach the service AND set include_non_active, else the
        proposal queue the flag exists to show comes back empty."""
        rsvc = MagicMock()
        rsvc.list_relationships.return_value = []
        p1, p2, p3 = _patched(rsvc)
        with p1, p2, p3:
            result = CliRunner().invoke(relationships_list, ["--status", "proposal"])
        assert result.exit_code == 0
        kwargs = rsvc.list_relationships.call_args.kwargs
        assert kwargs["status"] == "proposal"
        assert kwargs["include_non_active"] is True

    def test_default_does_not_widen(self):
        """Control for the above: with no --status the listing stays active-only."""
        rsvc = MagicMock()
        rsvc.list_relationships.return_value = []
        p1, p2, p3 = _patched(rsvc)
        with p1, p2, p3:
            CliRunner().invoke(relationships_list, [])
        kwargs = rsvc.list_relationships.call_args.kwargs
        assert kwargs["status"] is None
        assert kwargs["include_non_active"] is False

    def test_stale_flag_is_forwarded(self):
        rsvc = MagicMock()
        rsvc.list_relationships.return_value = []
        p1, p2, p3 = _patched(rsvc)
        with p1, p2, p3:
            CliRunner().invoke(relationships_list, ["--stale"])
        assert rsvc.list_relationships.call_args.kwargs["stale_only"] is True

    def test_explicit_scope_id_overrides_cwd_resolution(self):
        rsvc = MagicMock()
        rsvc.list_relationships.return_value = []
        p1, p2, p3 = _patched(rsvc, scope_id="resolved-from-cwd")
        with p1, p2, p3:
            CliRunner().invoke(relationships_list, ["--scope-id", "explicit"])
        # positional: (scope, scope_id, source_key)
        assert rsvc.list_relationships.call_args.args[1] == "explicit"

    def test_scope_id_falls_back_to_cwd_resolution(self):
        rsvc = MagicMock()
        rsvc.list_relationships.return_value = []
        p1, p2, p3 = _patched(rsvc, scope_id="resolved-from-cwd")
        with p1, p2, p3:
            CliRunner().invoke(relationships_list, [])
        assert rsvc.list_relationships.call_args.args[1] == "resolved-from-cwd"

    def test_json_format_emits_parseable_json(self):
        import json

        rsvc = MagicMock()
        rsvc.list_relationships.return_value = [_dto()]
        p1, p2, p3 = _patched(rsvc)
        with p1, p2, p3:
            result = CliRunner().invoke(relationships_list, ["--format", "json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload[0]["source_key"] == "a"
        assert "SOURCE -> TARGET" not in result.output


class TestRelationshipsInspect:
    def test_renders_each_field(self):
        rsvc = MagicMock()
        rsvc.get.return_value = _dto()
        p1, p2, p3 = _patched(rsvc)
        with p1, p2, p3:
            result = CliRunner().invoke(relationships_inspect, ["some-id"])
        assert result.exit_code == 0
        assert "source_key" in result.output
        assert "relates_to" in result.output

    def test_unknown_id_is_a_clean_error_not_a_traceback(self):
        rsvc = MagicMock()
        rsvc.get.return_value = None
        p1, p2, p3 = _patched(rsvc)
        with p1, p2, p3:
            result = CliRunner().invoke(relationships_inspect, ["nope"])
        assert result.exit_code != 0
        assert "relationship not found" in result.output
        assert "Traceback" not in result.output

    def test_json_format_emits_parseable_json(self):
        import json

        rsvc = MagicMock()
        rsvc.get.return_value = _dto()
        p1, p2, p3 = _patched(rsvc)
        with p1, p2, p3:
            result = CliRunner().invoke(relationships_inspect, ["some-id", "--format", "json"])
        assert result.exit_code == 0
        assert json.loads(result.output)["status"] == "active"


class TestRelationshipsPromoteReject:
    def test_promote_reports_the_new_status(self):
        rsvc = MagicMock()
        rsvc.promote.return_value = _dto(status="active")
        p1, p2, p3 = _patched(rsvc)
        with p1, p2, p3:
            result = CliRunner().invoke(relationships_promote, ["rid"])
        assert result.exit_code == 0
        rsvc.promote.assert_called_once_with("rid")
        assert "status -> active" in result.output

    def test_reject_reports_the_new_status(self):
        rsvc = MagicMock()
        rsvc.reject.return_value = _dto(status="rejected")
        p1, p2, p3 = _patched(rsvc)
        with p1, p2, p3:
            result = CliRunner().invoke(relationships_reject, ["rid"])
        assert result.exit_code == 0
        rsvc.reject.assert_called_once_with("rid")
        assert "status -> rejected" in result.output

    def test_promote_surfaces_a_service_valueerror_cleanly(self):
        """``promote()`` raises ValueError for a rejected/deleted row. The CLI must
        render that as a ClickException message, not a traceback."""
        rsvc = MagicMock()
        rsvc.promote.side_effect = ValueError(
            "cannot promote a rejected relationship; re-create it"
        )
        p1, p2, p3 = _patched(rsvc)
        with p1, p2, p3:
            result = CliRunner().invoke(relationships_promote, ["rid"])
        assert result.exit_code != 0
        assert "cannot promote a rejected relationship" in result.output
        assert "Traceback" not in result.output

    def test_reject_surfaces_a_service_valueerror_cleanly(self):
        rsvc = MagicMock()
        rsvc.reject.side_effect = ValueError("relationship not found: 'rid'")
        p1, p2, p3 = _patched(rsvc)
        with p1, p2, p3:
            result = CliRunner().invoke(relationships_reject, ["rid"])
        assert result.exit_code != 0
        assert "relationship not found" in result.output
        assert "Traceback" not in result.output
