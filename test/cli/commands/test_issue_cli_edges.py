"""Edge-case coverage for the ``cao project`` / ``cao issue`` CLI.

``test_issue_cli`` pins the happy paths that motivated this surface (works on a
fresh state root). These tests reach the branches it leaves uncovered: the rich
``show`` renders, the refusal -> exit-non-zero mappings, file/body-file
ingestion, scope add/remove, links and comments, and the markdown export-to-file
path.
"""

import json

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.cli.commands import issue as issue_cli
from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services import issue_tracker as tracker


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/edges.db")
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(tracker, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(issue_cli, "ensure_tracker_schema", lambda: None)
    yield
    engine.dispose()


@pytest.fixture
def runner():
    return CliRunner()


def _ok(runner, group, *args):
    result = runner.invoke(group, list(args))
    assert result.exit_code == 0, result.output + str(result.exception)
    return result.output


@pytest.fixture
def project(runner, tmp_path):
    repo = tmp_path / "cao-conductor"
    (repo / "conduct").mkdir(parents=True)
    _ok(
        runner,
        issue_cli.project,
        "create",
        "CAO System",
        "--id",
        "cao-system",
        "--prefix",
        "cond",
        "--path",
        str(repo),
    )
    return repo


def _file_issue(runner, *extra):
    args = ["file", "--title", "a defect", "--project", "cao-system"]
    args.extend(extra)
    return _ok(runner, issue_cli.issue, *args)


class TestProjectShowUpdateDelete:
    def test_show_renders_description_and_status_breakdown(self, runner, project):
        _ok(runner, issue_cli.project, "update", "cao-system", "--description", "the system")
        _file_issue(runner, "--severity", "P1")
        out = _ok(runner, issue_cli.project, "show", "cao-system")
        assert "the system" in out
        assert "open" in out  # by_status breakdown rendered

    def test_show_unknown_project_refuses(self, runner, project):
        result = runner.invoke(issue_cli.project, ["show", "ghost"])
        assert result.exit_code == 1
        assert "error [" in result.output

    def test_update_renames_and_reprefixes(self, runner, project):
        out = _ok(runner, issue_cli.project, "update", "cao-system", "--name", "Renamed")
        assert "updated cao-system" in out

    def test_delete_without_force_refuses_when_issues_exist(self, runner, project):
        _file_issue(runner)
        result = runner.invoke(issue_cli.project, ["delete", "cao-system"])
        assert result.exit_code == 1

    def test_delete_with_force_removes_project(self, runner, project):
        _file_issue(runner)
        out = _ok(runner, issue_cli.project, "delete", "cao-system", "--force")
        assert "deleted cao-system" in out


class TestProjectResolveExportScope:
    def test_resolve_with_unknown_explicit_project_refuses(self, runner, project):
        result = runner.invoke(
            issue_cli.project, ["resolve", "--project", "ghost", "--cwd", str(project)]
        )
        assert result.exit_code == 1

    def test_export_writes_markdown_to_file(self, runner, project, tmp_path):
        _file_issue(runner)
        out_path = tmp_path / "log.md"
        out = _ok(runner, issue_cli.project, "export", "cao-system", "-o", str(out_path))
        assert f"wrote {out_path}" in out
        assert "a defect" in out_path.read_text(encoding="utf-8")

    def test_export_unknown_project_refuses(self, runner, project):
        result = runner.invoke(issue_cli.project, ["export", "ghost"])
        assert result.exit_code == 1

    def test_scope_add_and_remove_round_trip(self, runner, project):
        out = _ok(
            runner,
            issue_cli.project,
            "scope",
            "add",
            "cao-system",
            "--kind",
            "session",
            "--value",
            "cao-p1",
        )
        assert "added:" in out
        scope_id = out.split("[")[1].split("]")[0]
        # Adding the same value again reports "already present".
        again = _ok(
            runner,
            issue_cli.project,
            "scope",
            "add",
            "cao-system",
            "--kind",
            "session",
            "--value",
            "cao-p1",
        )
        assert "already present" in again
        removed = _ok(runner, issue_cli.project, "scope", "rm", "cao-system", scope_id)
        assert "removed scope" in removed


class TestIssueShowEditClose:
    def test_show_renders_full_detail_with_links_and_comments(self, runner, project):
        _file_issue(
            runner,
            "--severity",
            "P1",
            "--component",
            "api",
            "--reporter",
            "colin",
            "--command",
            "pytest -x",
            "--evidence",
            "/tmp/log",
            "--label",
            "bug",
            "--body",
            "steps to reproduce",
        )
        _file_issue(runner, "--title", "second")  # cond-0002 to link to
        _ok(runner, issue_cli.issue, "link", "cond-0001", "--to", "cond-0002", "--kind", "blocks")
        _ok(runner, issue_cli.issue, "comment", "cond-0001", "--body", "a note", "--author", "rev")
        out = _ok(runner, issue_cli.issue, "show", "cond-0001")
        assert "[P1]" in out
        assert "api" in out
        assert "colin" in out
        assert "pytest -x" in out
        assert "/tmp/log" in out
        assert "bug" in out
        assert "steps to reproduce" in out
        assert "blocks" in out
        assert "a note" in out

    def test_show_unknown_issue_refuses(self, runner, project):
        result = runner.invoke(issue_cli.issue, ["show", "cond-9999"])
        assert result.exit_code == 1

    def test_edit_body_file_and_labels(self, runner, project, tmp_path):
        _file_issue(runner)
        body = tmp_path / "b.md"
        body.write_text("edited body", encoding="utf-8")
        out = _ok(
            runner,
            issue_cli.issue,
            "edit",
            "cond-0001",
            "--body-file",
            str(body),
            "--label",
            "urgent",
            "--actor",
            "colin",
        )
        assert "cond-0001" in out
        assert tracker.get_issue("cond-0001")["body"] == "edited body"

    def test_edit_with_nothing_to_change_refuses(self, runner, project):
        _file_issue(runner)
        result = runner.invoke(issue_cli.issue, ["edit", "cond-0001"])
        assert result.exit_code == 1
        assert "nothing to change" in result.output

    def test_close_as_wontfix(self, runner, project):
        _file_issue(runner)
        out = _ok(
            runner,
            issue_cli.issue,
            "close",
            "cond-0001",
            "--resolution",
            "out of scope",
            "--as",
            "wontfix",
        )
        assert "wontfix" in out
        assert tracker.get_issue("cond-0001")["status"] == "wontfix"


class TestIssueCommentLinkRmStats:
    def test_comment_body_file(self, runner, project, tmp_path):
        _file_issue(runner)
        body = tmp_path / "c.md"
        body.write_text("filed comment", encoding="utf-8")
        out = _ok(runner, issue_cli.issue, "comment", "cond-0001", "--body-file", str(body))
        assert "comment" in out

    def test_comment_without_body_refuses(self, runner, project):
        _file_issue(runner)
        result = runner.invoke(issue_cli.issue, ["comment", "cond-0001"])
        assert result.exit_code == 1
        assert "needs --body" in result.output

    def test_link_round_trip(self, runner, project):
        _file_issue(runner)
        _file_issue(runner, "--title", "second")
        out = _ok(
            runner, issue_cli.issue, "link", "cond-0001", "--to", "cond-0002", "--kind", "relates"
        )
        assert "cond-0001 relates cond-0002" in out

    def test_rm_with_yes_deletes(self, runner, project):
        _file_issue(runner)
        out = _ok(runner, issue_cli.issue, "rm", "cond-0001", "--yes")
        assert "deleted cond-0001" in out

    def test_stats_renders_component_breakdown(self, runner, project):
        _file_issue(runner, "--component", "api", "--severity", "P1")
        out = _ok(runner, issue_cli.issue, "stats", "--project", "cao-system")
        assert "by component:" in out
        assert "api" in out
        assert "P1" in out


class TestIssueImportLedger:
    def test_dry_run_reports_entries(self, runner, project, tmp_path):
        ledger = tmp_path / "ledger.md"
        ledger.write_text(
            "## cond-0001 — a bug\n\n"
            "- **filed:** 2026-07-21T11:44:29Z\n"
            "- **status:** open\n"
            "- **failing command:** `make test`\n\n",
            encoding="utf-8",
        )
        out = _ok(
            runner,
            issue_cli.issue,
            "import-ledger",
            str(ledger),
            "--project",
            "cao-system",
            "--dry-run",
        )
        # Dry-run must not create tracker state.
        assert tracker.list_issues(project_id="cao-system")["total"] == 0
        assert "would import 1 of 1" in out
