"""Behavior tests for the ``cao feature`` CLI surface.

The feature-request command group (file/list/show/edit/close/comment/link/rm/
stats/import-future-improvements) is the recently-introduced tracker CLI.
These tests exercise its real behavior end-to-end through Click's ``CliRunner``
against an isolated in-process tracker DB: successful creation and rendering,
the ``--json`` machine path, body-file ingestion, the refusal contracts that
classify errors and exit non-zero, and the dry-run / apply import subcommands.
"""

import json

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.cli.commands.issue import feature
from cli_agent_orchestrator.clients import database as dbmod
from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services import issue_tracker as tracker


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path}/feat.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(tracker, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(dbmod, "engine", engine)
    yield engine
    engine.dispose()


@pytest.fixture
def cao_system(tmp_path):
    conductor = tmp_path / "cao-conductor"
    fork = tmp_path / "cli-agent-orchestrator"
    conductor.mkdir()
    fork.mkdir()
    tracker.create_project(
        name="CAO System",
        project_id="cao-system",
        issue_prefix="cond",
        scopes=[
            {"kind": "path", "value": str(conductor)},
            {"kind": "path", "value": str(fork)},
        ],
    )


def _file_feature(args=None):
    runner = CliRunner()
    base = ["file", "--project", "cao-system", "--title", "ship it"]
    return runner.invoke(feature, base + (args or []))


class TestFeatureFile:
    def test_creates_feature_and_renders_key(self, cao_system):
        result = _file_feature(["--priority", "P2"])
        assert result.exit_code == 0
        assert "created cond-0001" in result.output
        assert "ship it" in result.output

    def test_json_output_is_parseable(self, cao_system):
        result = _file_feature(["--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["title"] == "ship it"
        assert payload["key"].startswith("cond-")

    def test_body_file_is_ingested(self, tmp_path, cao_system):
        body = tmp_path / "b.md"
        body.write_text("the long description", encoding="utf-8")
        result = _file_feature(["--body-file", str(body), "--json"])
        assert result.exit_code == 0
        assert json.loads(result.output)["body"] == "the long description"

    def test_refusal_exits_nonzero_with_classification(self, cao_system):
        # Unknown explicit project resolves to nothing.
        runner = CliRunner()
        result = runner.invoke(
            feature,
            ["file", "--project", "ghost", "--title", "x"],
        )
        assert result.exit_code == 1
        assert "error [" in result.output


class TestFeatureList:
    def test_lists_features_with_total(self, cao_system):
        _file_feature(["--priority", "P1"])
        _file_feature(["--title", "second", "--priority", "P3"])
        runner = CliRunner()
        result = runner.invoke(feature, ["list", "--project", "cao-system"])
        assert result.exit_code == 0
        assert "ship it" in result.output
        assert "2 total" in result.output

    def test_empty_list_says_so(self, cao_system):
        runner = CliRunner()
        result = runner.invoke(feature, ["list", "--project", "cao-system"])
        assert result.exit_code == 0
        assert "no feature requests" in result.output

    def test_open_only_and_priority_filter(self, cao_system):
        _file_feature(["--priority", "P1"])
        runner = CliRunner()
        result = runner.invoke(feature, ["list", "--project", "cao-system", "--priority", "P1"])
        assert result.exit_code == 0
        assert "ship it" in result.output


class TestFeatureShowEditClose:
    def test_show_renders_feature(self, cao_system):
        _file_feature(["--priority", "P2", "--component", "api"])
        runner = CliRunner()
        result = runner.invoke(feature, ["show", "cond-0001"])
        assert result.exit_code == 0
        assert "cond-0001 — ship it" in result.output
        assert "api" in result.output

    def test_show_non_feature_kind_refuses(self, cao_system):
        # cond-0001 doesn't exist yet -> not-found path.
        runner = CliRunner()
        result = runner.invoke(feature, ["show", "cond-9999"])
        assert result.exit_code == 1

    def test_edit_updates_title(self, cao_system):
        _file_feature()
        runner = CliRunner()
        result = runner.invoke(feature, ["edit", "cond-0001", "--title", "renamed"])
        assert result.exit_code == 0
        assert "updated cond-0001" in result.output
        assert tracker.get_issue("cond-0001")["title"] == "renamed"

    def test_edit_body_file_and_outcome(self, tmp_path, cao_system):
        _file_feature()
        body = tmp_path / "b.md"
        body.write_text("new body", encoding="utf-8")
        runner = CliRunner()
        result = runner.invoke(
            feature,
            ["edit", "cond-0001", "--body-file", str(body), "--resolution", "done"],
        )
        assert result.exit_code == 0
        issue = tracker.get_issue("cond-0001")
        assert issue["body"] == "new body"
        assert issue["resolution"] == "done"

    def test_edit_with_no_fields_reports_nothing_to_update(self, cao_system):
        _file_feature()
        runner = CliRunner()
        result = runner.invoke(feature, ["edit", "cond-0001"])
        assert result.exit_code == 0
        assert "nothing to update" in result.output

    def test_close_marks_terminal(self, cao_system):
        _file_feature()
        runner = CliRunner()
        result = runner.invoke(
            feature, ["close", "cond-0001", "--outcome", "shipped", "--status", "closed"]
        )
        assert result.exit_code == 0
        assert tracker.get_issue("cond-0001")["status"] == "closed"


class TestFeatureCommentLinkRmStats:
    def test_comment_requires_body(self, cao_system):
        _file_feature()
        runner = CliRunner()
        result = runner.invoke(feature, ["comment", "cond-0001"])
        assert result.exit_code == 1
        assert "comment body required" in result.output

    def test_comment_added(self, cao_system):
        _file_feature()
        runner = CliRunner()
        result = runner.invoke(feature, ["comment", "cond-0001", "--body", "nice"])
        assert result.exit_code == 0
        assert "comment" in result.output

    def test_link_relates_two_features(self, cao_system):
        _file_feature()
        _file_feature(["--title", "second"])
        runner = CliRunner()
        result = runner.invoke(
            feature, ["link", "cond-0001", "--to", "cond-0002", "--kind", "relates"]
        )
        assert result.exit_code == 0
        links = tracker.get_issue("cond-0001").get("links", [])
        assert any(l.get("to_key") == "cond-0002" for l in links)

    def test_rm_requires_yes(self, cao_system):
        _file_feature()
        runner = CliRunner()
        result = runner.invoke(feature, ["rm", "cond-0001"])
        assert result.exit_code == 1
        assert "--yes" in result.output
        # Still present.
        assert tracker.get_issue("cond-0001") is not None

    def test_rm_with_yes_deletes(self, cao_system):
        _file_feature()
        runner = CliRunner()
        result = runner.invoke(feature, ["rm", "cond-0001", "--yes"])
        assert result.exit_code == 0
        assert "deleted cond-0001" in result.output

    def test_stats_reports_counts(self, cao_system):
        _file_feature(["--priority", "P1"])
        runner = CliRunner()
        result = runner.invoke(feature, ["stats", "--project", "cao-system"])
        assert result.exit_code == 0
        assert "features" in result.output
        assert "P1" in result.output


class TestFeatureImportFutureImprovements:
    def _source(self, tmp_path):
        source = tmp_path / "FUTURE.md"
        source.write_text("# P2\n\n- **roadmap item**\n  body\n", encoding="utf-8")
        return source

    def test_dry_run_reports_candidates_without_state(self, tmp_path, cao_system):
        source = self._source(tmp_path)
        before = tracker.list_issues(project_id="cao-system", kind="all")["total"]
        runner = CliRunner()
        result = runner.invoke(
            feature, ["import-future-improvements", "--source", str(source), "--dry-run"]
        )
        assert result.exit_code == 0
        assert "1 candidate" in result.output
        after = tracker.list_issues(project_id="cao-system", kind="all")["total"]
        assert before == after

    def test_dry_run_requires_source(self, cao_system):
        runner = CliRunner()
        result = runner.invoke(feature, ["import-future-improvements", "--dry-run"])
        assert result.exit_code == 1
        assert "--source" in result.output

    def test_apply_requires_manifest_and_yes(self, tmp_path, cao_system):
        runner = CliRunner()
        result = runner.invoke(feature, ["import-future-improvements", "--apply"])
        assert result.exit_code == 1
        assert "--manifest" in result.output

    def test_apply_creates_feature_from_manifest(self, tmp_path, cao_system):
        from cli_agent_orchestrator.services.future_improvements_import import (
            dry_run,
        )

        source = self._source(tmp_path)
        plan = dry_run(source_path=str(source), project_id="cao-system")
        cand = dict(plan["candidates"][0])
        cand["action"] = "create-feature"
        cand["priority"] = "P2"
        cand["status"] = "open"
        cand["labels"] = ["roadmap", "source:future-improvements"]
        manifest = tmp_path / "m.json"
        manifest.write_text(
            json.dumps(
                {
                    "source_sha256": plan["source_sha256"],
                    "project": "cao-system",
                    "target_project": "cao-system",
                    "candidates": [cand],
                }
            ),
            encoding="utf-8",
        )
        runner = CliRunner()
        result = runner.invoke(
            feature,
            [
                "import-future-improvements",
                "--apply",
                "--manifest",
                str(manifest),
                "--yes",
            ],
        )
        assert result.exit_code == 0
        assert "applied 1 candidate" in result.output
