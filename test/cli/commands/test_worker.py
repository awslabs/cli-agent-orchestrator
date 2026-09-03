"""Tests for the `cao worker` command group.

`cao worker` is `cao session` aimed at an agent in another cluster, and the
interesting behaviour is all in the seams where the two sources of truth
disagree:

- **A lease and a terminal answer different questions.** The lease says whether
  the cluster still considers the worker alive; the terminal says whether the
  agent inside it is working. `status` has to show both, because the case worth
  catching is a live pod whose agent finished minutes ago without saying so.
- **A settled lease means the worker is gone.** `status` must not then try to
  reach its cao-server: the Service was deleted with the Deployment, so the
  call cannot succeed and its failure would mask the lease row that is the
  actual answer.
- **A missing lease does not mean a missing worker.** Leases live in the
  broker's memory, so a restart leaves a healthy worker with no row. `status`
  says which of the two it is.
- **`list` hides settled leases by default and `--all` is where the reasons
  live** -- `expired`, `failed` and their `REASON` text exist nowhere else, not
  in the supervisor's transcript and not in the cluster once the pod is gone.
- **Interrupting a wait must not release the worker.** Both `send` and `attach`
  catch `KeyboardInterrupt`, print what the agent said so far, and leave the
  lease alone.

Stubbed at `FleetClient.from_env` and at `poll_until_done`, so no broker, no
cluster and no three-second sleeps.
"""

import json
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.worker import worker


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def broker():
    client = MagicMock()
    client.url = "http://broker:9890"
    with patch(
        "cli_agent_orchestrator.cli.commands.worker.FleetClient.from_env", return_value=client
    ):
        # No real waiting: `send` and `attach` both sleep 3s before polling.
        with patch("cli_agent_orchestrator.cli.commands.worker.time.sleep"):
            yield client


@pytest.fixture
def poll():
    with patch("cli_agent_orchestrator.cli.commands.worker.poll_until_done") as poll_until_done:
        yield poll_until_done


def _lease(worker_id, state, **extra):
    lease = {
        "worker_id": worker_id,
        "state": state,
        "reason": None,
        "agent_profile": "developer",
        "provider": "claude_code",
        "age_seconds": 30,
    }
    lease.update(extra)
    return lease


def _terminal(terminal_id="t1", status="completed", **extra):
    terminal = {
        "id": terminal_id,
        "agent_profile": "developer",
        "provider": "claude_code",
        "status": status,
    }
    terminal.update(extra)
    return terminal


class TestList:
    def test_settled_leases_are_hidden_by_default(self, runner, broker):
        broker.workers.return_value = [
            _lease("live1", "leased"),
            _lease("new01", "creating"),
            _lease("done1", "completed"),
        ]

        result = runner.invoke(worker, ["list"])

        assert result.exit_code == 0
        assert "live1" in result.output
        assert "new01" in result.output
        assert "done1" not in result.output

    def test_all_shows_settled_leases_and_the_reason_the_broker_recorded(self, runner, broker):
        broker.workers.return_value = [
            _lease("live1", "leased"),
            _lease("dead1", "expired", reason="no completion within 900s", age_seconds=1128),
        ]

        result = runner.invoke(worker, ["list", "--all"])

        assert "dead1" in result.output
        assert "no completion within 900s" in result.output
        assert "1128s" in result.output

    def test_the_table_has_a_reason_column_header(self, runner, broker):
        broker.workers.return_value = [_lease("live1", "leased")]

        result = runner.invoke(worker, ["list"])

        header = result.output.splitlines()[0]
        assert header.split() == ["WORKER", "STATE", "AGENT", "PROVIDER", "AGE", "REASON"]

    def test_a_missing_profile_or_provider_reads_na_rather_than_none(self, runner, broker):
        broker.workers.return_value = [_lease("live1", "leased", agent_profile=None, provider=None)]

        result = runner.invoke(worker, ["list"])

        assert "None" not in result.output
        assert result.output.count("N/A") == 2

    def test_the_two_empty_cases_say_different_things(self, runner, broker):
        """`No live workers` invites `--all`; `No workers` says do not bother."""
        broker.workers.return_value = [_lease("done1", "completed")]

        assert "No live workers" in runner.invoke(worker, ["list"]).output

        broker.workers.return_value = []
        assert "No workers" in runner.invoke(worker, ["list", "--all"]).output

    def test_json_is_filtered_the_same_way_the_table_is(self, runner, broker):
        broker.workers.return_value = [_lease("live1", "leased"), _lease("done1", "completed")]

        rows = json.loads(runner.invoke(worker, ["list", "--json"]).output)

        assert [r["worker_id"] for r in rows] == ["live1"]

    def test_json_with_all_keeps_everything(self, runner, broker):
        broker.workers.return_value = [_lease("live1", "leased"), _lease("done1", "completed")]

        rows = json.loads(runner.invoke(worker, ["list", "--all", "--json"]).output)

        assert [r["worker_id"] for r in rows] == ["live1", "done1"]

    def test_an_empty_json_listing_is_still_json(self, runner, broker):
        broker.workers.return_value = []

        assert json.loads(runner.invoke(worker, ["list", "--json"]).output) == []


class TestStatus:
    def test_a_live_worker_shows_both_the_lease_and_the_agent(self, runner, broker):
        broker.workers.return_value = [_lease("w1", "leased", age_seconds=97)]
        broker.sole_terminal.return_value = _terminal("3d3224ce")
        broker.terminal.return_value = _terminal("3d3224ce", status="completed")
        broker.terminal_output.return_value = "all done"

        result = runner.invoke(worker, ["status", "w1"])

        assert result.exit_code == 0
        assert "Lease:    leased (97s)" in result.output
        assert "Terminal: 3d3224ce" in result.output
        assert "Status:   completed" in result.output
        assert "all done" in result.output

    def test_a_settled_lease_does_not_reach_for_a_deleted_worker(self, runner, broker):
        """The Service went with the Deployment; the lease row IS the answer."""
        broker.workers.return_value = [
            _lease("w1", "expired", reason="no completion within 900s", age_seconds=1128)
        ]

        result = runner.invoke(worker, ["status", "w1"])

        assert result.exit_code == 0
        assert "Lease:    expired (1128s)" in result.output
        assert "Reason:   no completion within 900s" in result.output
        assert "Terminal:" not in result.output
        broker.sole_terminal.assert_not_called()

    def test_no_lease_row_says_the_broker_may_have_restarted(self, runner, broker):
        broker.workers.return_value = []
        broker.sole_terminal.return_value = _terminal()
        broker.terminal.return_value = _terminal()
        broker.terminal_output.return_value = None

        result = runner.invoke(worker, ["status", "w1"])

        assert result.exit_code == 0
        assert "not in the broker's ledger" in result.output
        # And it still asks the worker, because the worker may be perfectly fine.
        broker.sole_terminal.assert_called_once_with("w1")
        assert "Terminal: t1" in result.output

    def test_a_long_last_response_is_truncated_and_says_by_how_much(self, runner, broker):
        broker.workers.return_value = [_lease("w1", "leased")]
        broker.sole_terminal.return_value = _terminal()
        broker.terminal.return_value = _terminal()
        broker.terminal_output.return_value = "\n".join(f"line {i}" for i in range(1, 26))

        result = runner.invoke(worker, ["status", "w1"])

        assert "line 20" in result.output
        assert "line 21" not in result.output
        assert "... (5 more lines)" in result.output

    def test_json_carries_all_four_pieces(self, runner, broker):
        broker.workers.return_value = [_lease("w1", "leased")]
        broker.sole_terminal.return_value = _terminal()
        broker.terminal.return_value = _terminal()
        broker.terminal_output.return_value = "hi"

        payload = json.loads(runner.invoke(worker, ["status", "w1", "--json"]).output)

        assert payload["worker_id"] == "w1"
        assert payload["lease"]["state"] == "leased"
        assert payload["terminal"]["id"] == "t1"
        assert payload["last_output"] == "hi"

    def test_a_booting_worker_reports_the_clients_hint(self, runner, broker):
        broker.workers.return_value = [_lease("w1", "leased")]
        broker.sole_terminal.side_effect = click.ClickException(
            "Worker w1 has no terminal yet. It may still be booting; "
            "`cao worker logs w1` shows how far it got."
        )

        result = runner.invoke(worker, ["status", "w1"])

        assert result.exit_code == 1
        assert "cao worker logs w1" in result.output


class TestSend:
    def test_async_returns_without_waiting(self, runner, broker, poll):
        broker.sole_terminal.return_value = _terminal("t1")

        result = runner.invoke(worker, ["send", "w1", "hello", "--async"])

        assert result.exit_code == 0
        broker.send_input.assert_called_once_with("w1", "t1", "hello")
        poll.assert_not_called()
        assert "Message sent to worker w1 (terminal t1)" in result.output

    def test_sync_waits_then_prints_the_reply(self, runner, broker, poll):
        broker.sole_terminal.return_value = _terminal("t1")
        broker.terminal_output.return_value = "Hostname: cao-worker-w1-abc-def, CPUs: 4."

        result = runner.invoke(worker, ["send", "w1", "hostname?"])

        assert result.exit_code == 0
        broker.send_input.assert_called_once_with("w1", "t1", "hostname?")
        assert "CPUs: 4." in result.output

    def test_done_detection_reads_status_through_the_broker(self, runner, broker, poll):
        """Duplicating the local done-detection here would hang a kiro worker."""
        broker.sole_terminal.return_value = _terminal("t1")
        broker.terminal_output.return_value = ""
        broker.terminal_status.return_value = "idle"

        runner.invoke(worker, ["send", "w1", "hi"])

        read_status = poll.call_args.kwargs["read_status"]
        assert read_status("t1") == "idle"
        broker.terminal_status.assert_called_with("w1", "t1")

    def test_the_default_timeout_is_used_when_none_is_given(self, runner, broker, poll):
        broker.sole_terminal.return_value = _terminal("t1")
        broker.terminal_output.return_value = ""

        runner.invoke(worker, ["send", "w1", "hi"])

        assert poll.call_args.args[1] == 300

    def test_an_explicit_timeout_wins(self, runner, broker, poll):
        broker.sole_terminal.return_value = _terminal("t1")
        broker.terminal_output.return_value = ""

        runner.invoke(worker, ["send", "w1", "hi", "--timeout", "30"])

        assert poll.call_args.args[1] == 30

    def test_ctrl_c_prints_what_arrived_and_exits_130(self, runner, broker, poll):
        """130 is the shell's SIGINT convention; the worker keeps its lease."""
        broker.sole_terminal.return_value = _terminal("t1")
        broker.terminal_output.return_value = "partial answer"
        poll.side_effect = KeyboardInterrupt

        result = runner.invoke(worker, ["send", "w1", "hi"])

        assert result.exit_code == 130
        assert "partial answer" in result.output
        broker.release.assert_not_called()


class TestSessions:
    def test_each_terminal_is_fetched_by_id_so_status_is_real(self, runner, broker):
        """The listing route omits `status`, and an always-N/A column is worse."""
        broker.sessions.return_value = [{"name": "cao-worker-76372746"}]
        broker.terminals.return_value = [{"id": "3d3224ce"}]
        broker.terminal.return_value = _terminal("3d3224ce", status="completed")

        result = runner.invoke(worker, ["sessions", "76372746"])

        assert result.exit_code == 0
        assert "cao-worker-76372746" in result.output
        assert "3d3224ce" in result.output
        assert "completed" in result.output
        broker.terminal.assert_called_once_with("76372746", "3d3224ce")

    def test_no_sessions_says_so(self, runner, broker):
        broker.sessions.return_value = []

        result = runner.invoke(worker, ["sessions", "w1"])

        assert "Worker w1 has no sessions" in result.output

    def test_json_nests_terminals_under_their_session(self, runner, broker):
        broker.sessions.return_value = [{"name": "cao-worker-w1"}]
        broker.terminals.return_value = [{"id": "t1"}]
        broker.terminal.return_value = _terminal("t1")

        payload = json.loads(runner.invoke(worker, ["sessions", "w1", "--json"]).output)

        assert payload == [
            {
                "session": "cao-worker-w1",
                "terminals": [_terminal("t1")],
            }
        ]


class TestLogs:
    def test_the_tail_is_printed_without_a_trailing_blank_line(self, runner, broker):
        broker.logs.return_value = "first\nsecond\n"

        result = runner.invoke(worker, ["logs", "w1"])

        assert result.exit_code == 0
        assert result.output == "first\nsecond\n"
        broker.logs.assert_called_once_with("w1", tail_lines=200)

    def test_n_sets_the_tail(self, runner, broker):
        broker.logs.return_value = ""

        runner.invoke(worker, ["logs", "w1", "-n", "6"])

        broker.logs.assert_called_once_with("w1", tail_lines=6)

    def test_follow_streams_lines(self, runner, broker):
        broker.follow_logs.return_value = iter(["a", "b"])

        result = runner.invoke(worker, ["logs", "w1", "-f"])

        assert result.exit_code == 0
        assert result.output == "a\nb\n"
        broker.follow_logs.assert_called_once_with("w1", tail_lines=200)

    def test_ctrl_c_out_of_a_follow_is_not_an_error(self, runner, broker):
        def lines(*_args, **_kwargs):
            yield "a"
            raise KeyboardInterrupt

        broker.follow_logs.side_effect = lines

        result = runner.invoke(worker, ["logs", "w1", "-f"])

        assert result.exit_code == 0
        assert "a" in result.output


class TestRelease:
    def test_it_releases_the_one_worker_named(self, runner, broker):
        result = runner.invoke(worker, ["release", "w1"])

        assert result.exit_code == 0
        broker.release.assert_called_once_with("w1")
        assert "✓ Released worker w1" in result.output

    def test_a_broker_error_is_reported_not_traced(self, runner, broker):
        broker.release.side_effect = click.ClickException("lease already settled")

        result = runner.invoke(worker, ["release", "w1"])

        assert result.exit_code == 1
        assert "lease already settled" in result.output
        assert "✓" not in result.output


class TestAttach:
    def test_it_says_what_it_is_attached_to(self, runner, broker, poll):
        broker.sole_terminal.return_value = _terminal("t1")

        result = runner.invoke(worker, ["attach", "w1"], input="exit\n")

        assert result.exit_code == 0
        assert "Attached to worker w1 (terminal t1, developer)" in result.output
        assert "the worker keeps its lease" in result.output

    @pytest.mark.parametrize("word", ["exit", "quit"])
    def test_leaving_sends_nothing_and_releases_nothing(self, runner, broker, poll, word):
        broker.sole_terminal.return_value = _terminal("t1")

        result = runner.invoke(worker, ["attach", "w1"], input=f"{word}\n")

        assert result.exit_code == 0
        broker.send_input.assert_not_called()
        broker.release.assert_not_called()

    def test_ctrl_d_leaves_cleanly(self, runner, broker, poll):
        broker.sole_terminal.return_value = _terminal("t1")

        result = runner.invoke(worker, ["attach", "w1"], input="")

        assert result.exit_code == 0
        broker.release.assert_not_called()

    def test_a_turn_is_sent_polled_and_printed(self, runner, broker, poll):
        broker.sole_terminal.return_value = _terminal("t1")
        broker.terminal_output.return_value = "the reply"

        result = runner.invoke(worker, ["attach", "w1"], input="hello\nexit\n")

        assert result.exit_code == 0
        broker.send_input.assert_called_once_with("w1", "t1", "hello")
        assert poll.call_count == 1
        assert "the reply" in result.output

    def test_ctrl_c_stops_waiting_but_keeps_the_session(self, runner, broker, poll):
        broker.sole_terminal.return_value = _terminal("t1")
        poll.side_effect = KeyboardInterrupt

        result = runner.invoke(worker, ["attach", "w1"], input="hello\nexit\n")

        assert result.exit_code == 0
        assert "stopped waiting" in result.output
        assert "the agent is still working" in result.output
        broker.release.assert_not_called()
