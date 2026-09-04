"""Tests for the `cao fleet` command group.

Two commands, and the second one destroys things: `cao fleet shutdown` deletes
running agent sessions in a shared cluster, and a released worker's state volume
goes with it. So most of what is asserted here is about restraint --

- a settled lease is never released again (it is already gone, and asking would
  turn a clean shutdown into an error),
- the confirmation prompt lists what is about to die before asking,
- declining releases nothing,
- one unreachable worker does not strand the rest, because the reason to run
  this command at all is that something is already wrong.

`cao fleet status` gets the same treatment for a smaller reason: its `Live:` and
`States:` lines answer different questions -- now, and since the last hour of
history -- and a reader who conflates them reads chapter 4's finished fan-out as
a fleet that is still burning nodes.

The client is stubbed at `FleetClient.from_env`, which is also the seam the
module's own design intends: no broker, no cluster, no `kubernetes` import.
"""

import json
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.fleet import fleet


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def broker():
    client = MagicMock()
    client.url = "http://broker:9890"
    with patch(
        "cli_agent_orchestrator.cli.commands.fleet.FleetClient.from_env", return_value=client
    ):
        yield client


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


class TestStatus:
    def test_live_counts_only_the_states_a_worker_still_exists_in(self, runner, broker):
        broker.workers.return_value = [
            _lease("a", "creating"),
            _lease("b", "leased"),
            _lease("c", "completed"),
            _lease("d", "expired"),
        ]

        result = runner.invoke(fleet, ["status"])

        assert result.exit_code == 0
        assert "Live:    2 worker(s)" in result.output
        assert "Broker:  http://broker:9890" in result.output

    def test_states_line_includes_settled_leases_sorted(self, runner, broker):
        broker.workers.return_value = [
            _lease("a", "leased"),
            _lease("b", "completed"),
            _lease("c", "completed"),
            _lease("d", "expired"),
        ]

        result = runner.invoke(fleet, ["status"])

        assert "States:  completed=2, expired=1, leased=1" in result.output

    def test_a_lease_with_no_state_is_counted_rather_than_dropped(self, runner, broker):
        """A row the client cannot classify must still appear somewhere."""
        broker.workers.return_value = [{"worker_id": "a", "age_seconds": 1}]

        result = runner.invoke(fleet, ["status"])

        assert "unknown=1" in result.output
        assert "Live:    0 worker(s)" in result.output

    def test_an_empty_ledger_says_so_in_words(self, runner, broker):
        """`no leases on record` is a stronger claim than `0` and reads as one."""
        broker.workers.return_value = []

        result = runner.invoke(fleet, ["status"])

        assert "States:  no leases on record" in result.output
        assert "Live:    0 worker(s)" in result.output

    def test_json_keeps_the_two_questions_separate(self, runner, broker):
        broker.workers.return_value = [_lease("a", "leased"), _lease("b", "completed")]

        result = runner.invoke(fleet, ["status", "--json"])

        assert result.exit_code == 0
        assert json.loads(result.output) == {
            "broker": "http://broker:9890",
            "live": 1,
            "states": {"leased": 1, "completed": 1},
        }

    def test_no_fleet_configured_is_reported_not_traced(self, runner):
        with patch(
            "cli_agent_orchestrator.cli.commands.fleet.FleetClient.from_env",
            side_effect=click.ClickException("No fleet configured."),
        ):
            result = runner.invoke(fleet, ["status"])

        assert result.exit_code == 1
        assert "No fleet configured." in result.output
        assert result.exception.__class__ is SystemExit


class TestShutdown:
    def test_nothing_live_releases_nothing(self, runner, broker):
        """Settled leases are already gone; asking the broker again is an error."""
        broker.workers.return_value = [_lease("a", "completed"), _lease("b", "expired")]

        result = runner.invoke(fleet, ["shutdown"])

        assert result.exit_code == 0
        assert "No live workers to release" in result.output
        broker.release.assert_not_called()

    def test_nothing_live_still_emits_valid_json(self, runner, broker):
        broker.workers.return_value = []

        result = runner.invoke(fleet, ["shutdown", "--json", "--yes"])

        assert json.loads(result.output) == {"released": [], "failed": []}
        broker.release.assert_not_called()

    def test_the_prompt_lists_what_is_about_to_die(self, runner, broker):
        broker.workers.return_value = [
            _lease("aaaa1111", "leased", agent_profile="developer", age_seconds=42)
        ]

        result = runner.invoke(fleet, ["shutdown"], input="y\n")

        assert result.exit_code == 0
        assert "About to release 1 worker(s):" in result.output
        assert "aaaa1111" in result.output
        assert "developer" in result.output
        assert "42s" in result.output
        # The prompt names the consequence, not just the count.
        assert "lose their sessions" in result.output
        broker.release.assert_called_once_with("aaaa1111")

    def test_declining_releases_nothing(self, runner, broker):
        broker.workers.return_value = [_lease("a", "leased"), _lease("b", "leased")]

        result = runner.invoke(fleet, ["shutdown"], input="n\n")

        assert result.exit_code == 1
        assert "Aborted" in result.output
        broker.release.assert_not_called()

    def test_no_terminal_to_ask_on_declines_rather_than_guessing(self, runner, broker):
        """`kubectl exec` without `-it` is the case this covers.

        With no stdin the prompt cannot be answered, and the command must fail
        closed. Losing a fleet to a missing `-it` would be the expensive way to
        learn that.
        """
        broker.workers.return_value = [_lease("a", "leased")]

        result = runner.invoke(fleet, ["shutdown"], input="")

        assert result.exit_code == 1
        broker.release.assert_not_called()

    def test_yes_skips_the_prompt(self, runner, broker):
        broker.workers.return_value = [_lease("a", "leased"), _lease("b", "creating")]

        result = runner.invoke(fleet, ["shutdown", "--yes"])

        assert result.exit_code == 0
        assert "About to release" not in result.output
        assert [c.args[0] for c in broker.release.call_args_list] == ["a", "b"]

    def test_settled_leases_are_skipped_even_with_yes(self, runner, broker):
        broker.workers.return_value = [
            _lease("live1", "leased"),
            _lease("done1", "completed"),
            _lease("gone1", "released"),
        ]

        result = runner.invoke(fleet, ["shutdown", "--yes"])

        assert result.exit_code == 0
        assert [c.args[0] for c in broker.release.call_args_list] == ["live1"]

    def test_json_alone_refuses_to_release_anything(self, runner, broker):
        """`--json` cannot prompt without corrupting its own output.

        So it fails closed and asks for `--yes`, rather than becoming the one
        flag that destroys a fleet unattended. The refusal comes before the
        broker is contacted, so a mistyped command cannot half-happen.
        """
        broker.workers.return_value = [_lease("a", "leased")]

        result = runner.invoke(fleet, ["shutdown", "--json"], input="")

        assert result.exit_code == 1
        assert "--yes" in result.output
        # Nothing released, and nothing even asked -- no partial JSON on stdout
        # for a script to misparse as a successful run.
        broker.release.assert_not_called()
        broker.workers.assert_not_called()

    def test_json_with_yes_is_the_unattended_form(self, runner, broker):
        broker.workers.return_value = [_lease("a", "leased")]

        result = runner.invoke(fleet, ["shutdown", "--json", "--yes"], input="")

        assert result.exit_code == 0
        assert json.loads(result.output) == {"released": ["a"], "failed": []}
        broker.release.assert_called_once_with("a")

    def test_one_unreachable_worker_does_not_strand_the_rest(self, runner, broker):
        broker.workers.return_value = [
            _lease("a", "leased"),
            _lease("b", "leased"),
            _lease("c", "leased"),
        ]

        def release(worker_id):
            if worker_id == "b":
                raise click.ClickException("worker b is unreachable")
            return True

        broker.release.side_effect = release

        result = runner.invoke(fleet, ["shutdown", "--yes"])

        assert [c.args[0] for c in broker.release.call_args_list] == ["a", "b", "c"]
        assert "✓ Released worker a" in result.output
        assert "✓ Released worker c" in result.output
        # The failure is on stderr so a pipeline of ✓ lines stays parseable.
        assert "worker b is unreachable" in result.stderr
        # And the exit code says the fleet is not clean.
        assert result.exit_code == 1
        assert "1 of 3 worker(s) could not be released" in result.output

    def test_a_partial_shutdown_is_named_in_json_and_exits_non_zero(self, runner, broker):
        """A script that only reads `$?` must not see a clean run.

        Both modes report first and exit second, so the caller gets the full
        list of what did and did not go AND an exit code that says the fleet is
        not clean. stdout stays pure JSON: the summary goes to stderr.
        """
        broker.workers.return_value = [_lease("a", "leased"), _lease("b", "leased")]
        broker.release.side_effect = lambda wid: (
            True if wid == "a" else (_ for _ in ()).throw(click.ClickException("nope"))
        )

        result = runner.invoke(fleet, ["shutdown", "--json", "--yes"])

        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["released"] == ["a"]
        assert payload["failed"] == [{"worker_id": "b", "error": "nope"}]
        assert "1 of 2 worker(s) could not be released" in result.stderr
