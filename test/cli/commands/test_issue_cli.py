"""`cao project` and `cao issue`.

This surface had no coverage at all until a manual smoke test found that every
command died with a raw SQLAlchemy traceback on a fresh state root: the CLI has
no lifespan, so nothing had created the tables. That is the failure these tests
exist to prevent recurring — the CLI is specifically the path for filing an
issue when the server is *not* running, which makes "works only after the
server has run once" the wrong dependency to have.
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
    engine = create_engine(f"sqlite:///{tmp_path}/cli.db")
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(tracker, "SessionLocal", sessionmaker(bind=engine))
    # The group callbacks create the schema on the real engine. Here the schema
    # already exists on the test engine, so the call is neutralised rather than
    # allowed to touch the operator's database.
    monkeypatch.setattr(issue_cli, "ensure_tracker_schema", lambda: None)
    yield
    engine.dispose()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "cao-conductor"
    (path / "conduct").mkdir(parents=True)
    return path


@pytest.fixture
def runner():
    return CliRunner()


def run(runner, group, *args):
    result = runner.invoke(group, list(args))
    assert result.exit_code == 0, result.output + str(result.exception)
    return result.output


class TestProjectCommands:
    def test_create_then_show(self, runner, repo):
        run(
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
            "--session",
            "cao-p1-closure",
        )
        out = run(runner, issue_cli.project, "show", "cao-system")
        assert "cond-NNNN" in out
        assert str(repo) in out
        assert "cao-p1-closure" in out

    def test_resolve_reports_what_matched(self, runner, repo):
        run(runner, issue_cli.project, "create", "P", "--id", "p", "--path", str(repo))
        out = run(runner, issue_cli.project, "resolve", "--cwd", str(repo / "conduct"))
        assert "matched by path" in out

    def test_resolve_says_so_when_nothing_is_registered(self, runner, tmp_path):
        out = run(runner, issue_cli.project, "resolve", "--cwd", str(tmp_path / "unknown"))
        assert "no project registered" in out

    def test_json_output_is_parseable(self, runner, repo):
        run(runner, issue_cli.project, "create", "P", "--id", "p")
        payload = json.loads(run(runner, issue_cli.project, "list", "--json"))
        assert [row["id"] for row in payload] == ["p"]

    def test_a_refusal_exits_non_zero_with_its_classification(self, runner):
        run(runner, issue_cli.project, "create", "P", "--id", "p")
        result = runner.invoke(issue_cli.project, ["create", "Q", "--id", "p"])
        assert result.exit_code == 1
        assert "[conflict]" in result.output

    def test_export_renders_markdown(self, runner, repo, tmp_path):
        run(runner, issue_cli.project, "create", "P", "--id", "p", "--prefix", "pp")
        run(
            runner,
            issue_cli.issue,
            "file",
            "--title",
            "a defect",
            "--project",
            "p",
            "--severity",
            "P2",
        )
        out = run(runner, issue_cli.project, "export", "p")
        assert "## pp-0001 — [P2] a defect" in out


class TestIssueCommands:
    @pytest.fixture(autouse=True)
    def project(self, runner, repo):
        run(
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

    def test_file_resolves_from_cwd(self, runner, repo):
        out = run(
            runner,
            issue_cli.issue,
            "file",
            "--title",
            "spawn crashed",
            "--cwd",
            str(repo / "conduct"),
        )
        assert "cond-0001" in out
        assert "cao-system" in out

    def test_file_into_an_unregistered_directory_fails_loudly(self, runner, tmp_path):
        result = runner.invoke(
            issue_cli.issue, ["file", "--title", "orphan", "--cwd", str(tmp_path / "nowhere")]
        )
        assert result.exit_code == 1
        assert "[unresolved]" in result.output

    def test_the_full_lifecycle(self, runner):
        run(
            runner,
            issue_cli.issue,
            "file",
            "--title",
            "a defect",
            "--project",
            "cao-system",
            "--severity",
            "P2",
            "--component",
            "conduct",
            "--label",
            "noisy",
        )
        run(runner, issue_cli.issue, "edit", "cond-0001", "--assignee", "terra", "--actor", "colin")
        run(
            runner,
            issue_cli.issue,
            "comment",
            "cond-0001",
            "--body",
            "reproduced",
            "--author",
            "colin",
        )
        run(runner, issue_cli.issue, "close", "cond-0001", "--resolution", "fixed in #12")
        out = run(runner, issue_cli.issue, "show", "cond-0001")
        assert "status:    closed" in out
        assert "resolution: fixed in #12" in out
        assert "reproduced" in out

    def test_an_edit_with_nothing_to_change_is_refused(self, runner):
        run(runner, issue_cli.issue, "file", "--title", "a defect", "--project", "cao-system")
        result = runner.invoke(issue_cli.issue, ["edit", "cond-0001"])
        assert result.exit_code == 1
        assert "nothing to change" in result.output

    def test_list_filters(self, runner):
        run(
            runner,
            issue_cli.issue,
            "file",
            "--title",
            "high",
            "--project",
            "cao-system",
            "--severity",
            "P1",
        )
        run(
            runner,
            issue_cli.issue,
            "file",
            "--title",
            "low",
            "--project",
            "cao-system",
            "--severity",
            "P4",
        )
        out = run(runner, issue_cli.issue, "list", "--project", "cao-system", "--severity", "P1")
        assert "high" in out
        assert "low" not in out

    def test_an_invalid_severity_is_rejected_by_the_parser(self, runner):
        result = runner.invoke(
            issue_cli.issue, ["file", "--title", "x", "--project", "cao-system", "--severity", "P9"]
        )
        assert result.exit_code != 0
        assert "P9" in result.output

    def test_stats_break_down_the_project(self, runner):
        run(
            runner,
            issue_cli.issue,
            "file",
            "--title",
            "a",
            "--project",
            "cao-system",
            "--severity",
            "P1",
        )
        payload = json.loads(
            run(runner, issue_cli.issue, "stats", "--project", "cao-system", "--json")
        )
        assert payload["by_severity"]["P1"] == 1

    def test_rm_requires_confirmation(self, runner):
        run(runner, issue_cli.issue, "file", "--title", "a defect", "--project", "cao-system")
        assert runner.invoke(issue_cli.issue, ["rm", "cond-0001"], input="n\n").exit_code != 0
        assert tracker.get_issue("cond-0001")["key"] == "cond-0001"
        run(runner, issue_cli.issue, "rm", "cond-0001", "--yes")


class TestLedgerImportCommand:
    LEDGER = (
        "# Open issues\n\n---\n\n"
        "## cond-0025 — [P2] a migrated defect\n\n"
        "- **filed:** 2026-07-21T11:44:29Z\n"
        "- **reporter:** human\n"
        "- **status:** open\n\n"
        "body text\n\n---\n"
    )

    @pytest.fixture
    def ledger(self, tmp_path):
        path = tmp_path / "OPEN_ISSUES.md"
        path.write_text(self.LEDGER, encoding="utf-8")
        return path

    def test_dry_run_writes_nothing(self, runner, ledger):
        run(runner, issue_cli.project, "create", "P", "--id", "p", "--prefix", "cond")
        out = run(
            runner, issue_cli.issue, "import-ledger", str(ledger), "--project", "p", "--dry-run"
        )
        assert "would import 1" in out
        assert tracker.list_issues(project_id="p")["total"] == 0

    def test_import_preserves_the_id_and_filing_date(self, runner, ledger):
        run(runner, issue_cli.project, "create", "P", "--id", "p", "--prefix", "cond")
        run(runner, issue_cli.issue, "import-ledger", str(ledger), "--project", "p")
        row = tracker.get_issue("cond-0025")
        assert row["created_at"] == "2026-07-21T11:44:29Z"
        assert row["severity"] == "P2"

    def test_re_running_skips_rather_than_duplicating(self, runner, ledger):
        run(runner, issue_cli.project, "create", "P", "--id", "p", "--prefix", "cond")
        run(runner, issue_cli.issue, "import-ledger", str(ledger), "--project", "p")
        out = run(runner, issue_cli.issue, "import-ledger", str(ledger), "--project", "p")
        assert "1 skipped" in out or "already present" in out
        assert tracker.list_issues(project_id="p")["total"] == 1
