"""`cao attachment` — the operator surface for provider-session claims.

The claims worth resolving are the ones a dead or wedged server left
behind, so these commands call the service directly rather than the HTTP
API. That choice is what these tests pin hardest: a group callback that
touched the schema, or a command that went through `requests`, would both
be unusable at exactly the moment the command matters.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.cli.commands import attachment as attachment_cli
from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services import native_attachment as na

PROVIDER = "kimi_cli"
SESSION = "session_326c5026"


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    """A per-test store, patched where the service actually opens it.

    `native_attachment` reaches `database.SessionLocal` through the module
    rather than importing the symbol, so patching it here is enough — and
    the group deliberately has no schema-creating callback that would run
    against the operator's real database before this fixture could act.
    """
    engine = create_engine(f"sqlite:///{tmp_path}/attachment-cli.db")
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(na.database, "SessionLocal", sessionmaker(bind=engine))
    return engine


def _reaped_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


def _intent() -> dict:
    return na.acquire_intent(
        acquisition_method=na.ACQUISITION_ACP_BOOTSTRAP,
        acquisition_receipt={"kind": "kimi-acp-session-new", "session_id": SESSION},
        admits_only_new_instructions=True,
        replays_task_bytes=False,
        bootstrap_sent_no_turn=True,
        bootstrap_detached_before_launch=True,
    )


def _attach(*, pid: int, session: str = SESSION, terminal_id: str = "44dda40b") -> dict:
    owner = {
        "terminal_id": terminal_id,
        "generation": "6e3d642e",
        "execution_mode": "native_tui",
    }
    na.declare(
        provider=PROVIDER,
        native_session_id=session,
        pane_id="%12",
        intent=_intent(),
        **owner,
    )
    na.mark_starting(provider=PROVIDER, native_session_id=session, **owner)
    return na.mark_attached(
        provider=PROVIDER,
        native_session_id=session,
        process_identity=na.process_identity(pid=pid, start_marker="Fri Aug  7 12:51:19 2026"),
        **owner,
    )


def _freeze(session: str = SESSION) -> dict:
    na.declare(
        provider=PROVIDER,
        native_session_id=session,
        terminal_id="44dda40b",
        generation="6e3d642e",
        execution_mode="native_tui",
        intent=_intent(),
    )
    return na.mark_ambiguous(
        provider=PROVIDER, native_session_id=session, reason="pane_create_outcome_unknown"
    )


class TestListing:
    def test_listing_shows_a_held_session(self):
        _attach(pid=_reaped_pid())
        result = CliRunner().invoke(attachment_cli.attachment, ["list"])
        assert result.exit_code == 0
        assert SESSION in result.output

    def test_listing_omits_detached_history_by_default(self):
        """The question is what is blocked now, not what once was."""
        record = _attach(pid=_reaped_pid())
        na.release(
            provider=PROVIDER,
            native_session_id=SESSION,
            terminal_id=record["owner"]["terminal_id"],
            generation=record["owner"]["generation"],
            execution_mode=record["owner"]["execution_mode"],
            proof=na.no_survivor_proof(
                provider=PROVIDER,
                native_session_id=SESSION,
                terminal_id=record["owner"]["terminal_id"],
                generation=record["owner"]["generation"],
                execution_mode=record["owner"]["execution_mode"],
                pane_id=record["owner"]["pane_id"],
                process_identity=record["owner"]["process_identity"],
                survivors=[],
                observed_at="2026-08-07T00:00:00Z",
                observer="test",
            ),
        )
        result = CliRunner().invoke(attachment_cli.attachment, ["list"])
        assert "no attachments match" in result.output

    def test_show_reports_whether_the_owner_is_still_there(self):
        """The row records who took the claim, never whether they are still there."""
        _attach(pid=_reaped_pid())
        result = CliRunner().invoke(attachment_cli.attachment, ["show", PROVIDER, SESSION])
        assert result.exit_code == 0
        assert "gone" in result.output

    def test_show_reports_a_missing_claim_as_a_failure(self):
        result = CliRunner().invoke(attachment_cli.attachment, ["show", PROVIDER, "nope"])
        assert result.exit_code == 1
        assert "not-found" in result.output


class TestSweep:
    def test_the_sweep_is_a_dry_run_by_default(self):
        _attach(pid=_reaped_pid())
        result = CliRunner().invoke(attachment_cli.attachment, ["sweep"])
        assert result.exit_code == 0
        assert "mode=dry-run" in result.output
        assert na.get(PROVIDER, SESSION)["state"] == na.ATTACHED

    def test_apply_releases_the_provably_gone(self):
        _attach(pid=_reaped_pid())
        result = CliRunner().invoke(attachment_cli.attachment, ["sweep", "--apply"])
        assert result.exit_code == 0
        assert "mode=apply" in result.output
        assert na.get(PROVIDER, SESSION)["state"] == na.DETACHED

    def test_apply_holds_a_live_owner_and_says_why(self):
        _attach(pid=os.getpid())
        result = CliRunner().invoke(attachment_cli.attachment, ["sweep", "--apply"])
        assert "alive" in result.output
        assert na.get(PROVIDER, SESSION)["state"] == na.ATTACHED


class TestAdjudication:
    def test_adjudicating_a_frozen_claim_releases_it(self):
        _freeze()
        result = CliRunner().invoke(
            attachment_cli.attachment,
            [
                "adjudicate",
                PROVIDER,
                SESSION,
                "--operator",
                "colin",
                "--detail",
                "pane and process both gone for six days",
                "--evidence-sha256",
                "b" * 64,
                "--yes",
            ],
        )
        assert result.exit_code == 0
        stored = na.get(PROVIDER, SESSION)
        assert stored["state"] == na.DETACHED
        assert stored["release_proof"]["operator"] == "colin"

    def test_declining_the_prompt_changes_nothing(self):
        _freeze()
        result = CliRunner().invoke(
            attachment_cli.attachment,
            [
                "adjudicate",
                PROVIDER,
                SESSION,
                "--operator",
                "colin",
                "--detail",
                "not sure actually",
                "--evidence-sha256",
                "b" * 64,
            ],
            input="n\n",
        )
        assert result.exit_code != 0
        assert na.get(PROVIDER, SESSION)["state"] == na.AMBIGUOUS

    def test_evidence_must_be_supplied_exactly_once(self):
        _freeze()
        result = CliRunner().invoke(
            attachment_cli.attachment,
            ["adjudicate", PROVIDER, SESSION, "--operator", "c", "--detail", "d", "--yes"],
        )
        assert result.exit_code != 0
        assert "exactly one of" in result.output

    def test_a_file_of_evidence_is_digested_rather_than_transcribed(self, tmp_path):
        _freeze()
        evidence = tmp_path / "evidence.txt"
        evidence.write_text("ps output showing no such pid\n", encoding="utf-8")
        result = CliRunner().invoke(
            attachment_cli.attachment,
            [
                "adjudicate",
                PROVIDER,
                SESSION,
                "--operator",
                "colin",
                "--detail",
                "captured process table",
                "--evidence",
                str(evidence),
                "--yes",
            ],
        )
        assert result.exit_code == 0
        import hashlib

        expected = hashlib.sha256(evidence.read_bytes()).hexdigest()
        assert na.get(PROVIDER, SESSION)["release_proof"]["evidence_sha256"] == expected

    def test_a_live_owner_is_refused_even_with_a_named_operator(self):
        """An operator may resolve an unresponsive owner, never a running one."""
        _attach(pid=os.getpid())
        na.mark_ambiguous(
            provider=PROVIDER, native_session_id=SESSION, reason="pane_render_mismatch"
        )
        result = CliRunner().invoke(
            attachment_cli.attachment,
            [
                "adjudicate",
                PROVIDER,
                SESSION,
                "--operator",
                "colin",
                "--detail",
                "I want it back",
                "--evidence-sha256",
                "c" * 64,
                "--yes",
            ],
        )
        assert result.exit_code == 1
        assert "still observably alive" in result.output
        assert na.get(PROVIDER, SESSION)["state"] == na.AMBIGUOUS


class TestTheGroupIsUsableWhenTheServerIsNot:
    def test_the_group_has_no_schema_touching_callback(self):
        """A DDL callback would run against the real database in tests.

        `cao issue` needs one and pays for it with a second monkeypatch in
        every test. This group is read-mostly against a table the migration
        ladder already creates, so it can simply not have the problem.
        """
        assert attachment_cli.attachment.callback.__doc__
        assert attachment_cli.attachment.callback() is None

    def test_no_command_reaches_for_the_http_api(self):
        import inspect

        source = inspect.getsource(attachment_cli)
        assert "requests" not in source
        assert "API_BASE_URL" not in source


class TestOperatorFreeze:
    def test_freezing_then_adjudicating_composes(self, monkeypatch):
        from cli_agent_orchestrator.services import native_attachment_recovery as recovery

        _attach(pid=os.getpid())
        monkeypatch.setattr(recovery, "_pid_state", lambda pid: recovery.OWNER_UNOBSERVABLE)
        frozen = CliRunner().invoke(
            attachment_cli.attachment,
            [
                "freeze",
                PROVIDER,
                SESSION,
                "--operator",
                "colin",
                "--detail",
                "process table will not answer",
                "--yes",
            ],
        )
        assert frozen.exit_code == 0
        assert na.get(PROVIDER, SESSION)["state"] == na.AMBIGUOUS

        monkeypatch.setattr(recovery, "_pid_state", lambda pid: recovery.OWNER_GONE)
        decided = CliRunner().invoke(
            attachment_cli.attachment,
            [
                "adjudicate",
                PROVIDER,
                SESSION,
                "--operator",
                "colin",
                "--detail",
                "host rebooted; nothing survived",
                "--evidence-sha256",
                "f" * 64,
                "--yes",
            ],
        )
        assert decided.exit_code == 0
        assert na.get(PROVIDER, SESSION)["state"] == na.DETACHED

    def test_freezing_a_running_owner_is_refused(self):
        _attach(pid=os.getpid())
        result = CliRunner().invoke(
            attachment_cli.attachment,
            ["freeze", PROVIDER, SESSION, "--operator", "colin", "--detail", "x", "--yes"],
        )
        assert result.exit_code == 1
        assert na.get(PROVIDER, SESSION)["state"] == na.ATTACHED


class TestAVirginInstall:
    """The first thing an operator runs is `list`, possibly before any server.

    The table is created by the migration ladder at server start, so a store
    no server has touched has no table — and therefore, demonstrably, no
    claims. Reporting that as a failure sends someone hunting a broken
    database when the honest answer is "nothing is held".
    """

    @pytest.fixture(autouse=True)
    def _no_table(self, tmp_path, monkeypatch):
        engine = create_engine(f"sqlite:///{tmp_path}/empty.db")
        monkeypatch.setattr(na.database, "SessionLocal", sessionmaker(bind=engine))
        return engine

    def test_listing_an_uncreated_store_is_empty_not_an_error(self):
        result = CliRunner().invoke(attachment_cli.attachment, ["list"])
        assert result.exit_code == 0
        assert "no attachments match" in result.output

    def test_sweeping_an_uncreated_store_examines_nothing(self):
        result = CliRunner().invoke(attachment_cli.attachment, ["sweep"])
        assert result.exit_code == 0
        assert "examined=0" in result.output

    def test_a_store_failure_reports_one_readable_line(self):
        """SQLAlchemy renders a driver error followed by the entire query.

        Fourteen column aliases of generated SQL is not a refusal an operator
        can act on; the sentence in front of it is.
        """
        result = CliRunner().invoke(attachment_cli.attachment, ["show", "kimi_cli", "abc"])
        assert result.exit_code == 1
        assert len(result.output.strip().splitlines()) == 1
        assert "no such table" in result.output
        assert "SELECT" not in result.output


class TestARetainedProofIsLabelled:
    def test_a_live_claim_shows_the_prior_proof_as_previous(self):
        """On a re-acquired row the retained proof describes a different owner."""
        record = _attach(pid=_reaped_pid())
        from cli_agent_orchestrator.services import native_attachment_recovery as recovery

        recovery.release_if_owner_gone(record)
        na.declare(
            provider=PROVIDER,
            native_session_id=SESSION,
            terminal_id="ffffffff",
            generation="99999999",
            execution_mode="native_tui",
            intent=_intent(),
        )
        result = CliRunner().invoke(attachment_cli.attachment, ["show", PROVIDER, SESSION])
        assert "previously released by" in result.output

    def test_a_detached_claim_shows_it_plainly(self):
        recovery_mod = __import__(
            "cli_agent_orchestrator.services.native_attachment_recovery",
            fromlist=["release_if_owner_gone"],
        )
        recovery_mod.release_if_owner_gone(_attach(pid=_reaped_pid()))
        result = CliRunner().invoke(attachment_cli.attachment, ["list", "--state", "detached"])
        assert result.exit_code == 0
