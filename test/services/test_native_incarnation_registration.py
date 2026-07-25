"""Live-incarnation registration for a v2 native launch.

Reproduced in production: a native Claude and a native Kimi terminal both
launched real TUIs with native session ids, and both logged ``was not
registered as a live incarnation``. ``create_terminal(protocol_vintage=
"v2")`` writes ``managed_launch_v2_terminals`` and then registered through
the legacy writer, which asks ``terminals`` -- a table the v2 launch never
writes. Every native launch therefore got an absent-row answer about a row
that existed, in a store nobody looked in.

Two things made that survivable long enough to reach production, and both
are pinned here: the failure was reported as a bare ``False`` that could
not distinguish "no row" from "unreadable pane" from "another pane already
holds this handle", and it was logged and passed over rather than failing
the launch.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import terminal_service

GEN = "gen-9f2c4a1b"
IDENTITY = {
    "server_socket_path": "/private/tmp/tmux-501/default",
    "session_id": "$7",
    "window_id": "@30",
    "pane_id": "%30",
    "pane_pid": 54321,
}


def _v2_row(terminal_id: str = "36bf6af6", **overrides) -> dict:
    kwargs = {
        "terminal_id": terminal_id,
        "tmux_session": "cao-chess-shakedown",
        "tmux_window": f"{terminal_id}-{GEN}",
        "provider": "kimi_cli",
        "agent_profile": "reviewer",
        "generation": GEN,
    }
    kwargs.update(overrides)
    return database.create_terminal_v2(**kwargs)


class TestTheV2RowIsTheOneRegistered:
    def test_a_native_launch_registers_its_incarnation(self, isolated_memory_db):
        """The reproduced fault: this returned absent-row for every launch."""
        _v2_row()

        assert (
            terminal_service._register_incarnation("36bf6af6", GEN, IDENTITY, protocol_vintage="v2")
            is True
        )

    def test_the_identity_lands_on_the_v2_row(self, isolated_memory_db):
        _v2_row()

        terminal_service._register_incarnation("36bf6af6", GEN, IDENTITY, protocol_vintage="v2")

        row = database.get_terminal_metadata_v2("36bf6af6")
        assert row["pane_id"] == "%30"
        assert row["window_id"] == "@30"
        assert row["server_socket_path"] == IDENTITY["server_socket_path"]
        # The v2 identity columns are ``v2_``-prefixed: the vintage receipt
        # records bare names and requires them unique across the surface,
        # which is also why this store gets its own writer rather than one
        # that switches column names by vintage.
        assert row["v2_session_id"] == "$7"
        assert row["v2_pane_pid"] == 54321
        assert row["v2_lifecycle_state"] == "live"

    def test_the_legacy_table_still_holds_no_v2_row(self, isolated_memory_db):
        """Vintage isolation is what makes an old binary blind to v2 state.

        The repair may not spend it to make a status lookup convenient, so
        registering a v2 incarnation must not create a legacy twin.
        """
        _v2_row()

        terminal_service._register_incarnation("36bf6af6", GEN, IDENTITY, protocol_vintage="v2")

        assert database.get_terminal_metadata("36bf6af6") is None

    def test_re_registering_the_same_identity_is_free(self, isolated_memory_db):
        """Re-driving a registration is how recovery works."""
        _v2_row()

        first = terminal_service._register_incarnation(
            "36bf6af6", GEN, IDENTITY, protocol_vintage="v2"
        )
        second = terminal_service._register_incarnation(
            "36bf6af6", GEN, IDENTITY, protocol_vintage="v2"
        )

        assert (first, second) == (True, True)


class TestTheThreeCausesAreDistinguished:
    """One boolean makes these indistinguishable exactly when it matters.

    They need different fixes: an absent row is a launch that wrote no
    terminal, a partial identity is a pane that could not be fully
    observed, and a held pane is a safety refusal that must never be
    overridden.
    """

    def test_an_absent_row_is_named_as_such(self, isolated_memory_db):
        with pytest.raises(terminal_service.IncarnationRegistrationRefused) as raised:
            terminal_service._register_incarnation(
                "no-such-id", GEN, IDENTITY, protocol_vintage="v2"
            )

        assert raised.value.cause == database.REGISTRATION_ABSENT_ROW

    def test_a_partial_identity_is_named_as_such(self, isolated_memory_db):
        """Refused outright rather than stored with the unreadable parts NULL.

        A row published with three of five fields is a row some later
        identity check will pass against.
        """
        _v2_row()

        with pytest.raises(terminal_service.IncarnationRegistrationRefused) as raised:
            terminal_service._register_incarnation(
                "36bf6af6", GEN, {**IDENTITY, "pane_pid": None}, protocol_vintage="v2"
            )

        assert raised.value.cause == database.REGISTRATION_PARTIAL_IDENTITY

    def test_a_pane_already_held_is_named_as_such(self, isolated_memory_db):
        """The never-re-point rule, which this repair must not weaken."""
        _v2_row()
        terminal_service._register_incarnation("36bf6af6", GEN, IDENTITY, protocol_vintage="v2")

        with pytest.raises(terminal_service.IncarnationRegistrationRefused) as raised:
            terminal_service._register_incarnation(
                "36bf6af6", GEN, {**IDENTITY, "pane_id": "%99"}, protocol_vintage="v2"
            )

        assert raised.value.cause == database.REGISTRATION_PANE_ALREADY_HELD

    def test_the_held_pane_is_left_exactly_as_it_was(self, isolated_memory_db):
        _v2_row()
        terminal_service._register_incarnation("36bf6af6", GEN, IDENTITY, protocol_vintage="v2")

        with pytest.raises(terminal_service.IncarnationRegistrationRefused):
            terminal_service._register_incarnation(
                "36bf6af6", GEN, {**IDENTITY, "pane_id": "%99"}, protocol_vintage="v2"
            )

        assert database.get_terminal_metadata_v2("36bf6af6")["pane_id"] == "%30"

    def test_the_causes_are_a_closed_vocabulary(self):
        assert database.REGISTRATION_OK in database.REGISTRATION_OUTCOMES
        for cause in (
            database.REGISTRATION_ABSENT_ROW,
            database.REGISTRATION_PARTIAL_IDENTITY,
            database.REGISTRATION_PANE_ALREADY_HELD,
            database.REGISTRATION_GENERATION_MISMATCH,
        ):
            assert cause in database.REGISTRATION_OUTCOMES
            assert cause != database.REGISTRATION_OK


class TestRegistrationIsALaunchPrecondition:
    def test_a_native_launch_fails_closed_rather_than_passing_over_it(self, isolated_memory_db):
        """The identity registered here is what every later lookup resolves.

        A launch that proceeds without it produces exactly what production
        showed: a live pane whose terminal nothing can find, status stuck
        unknown, and a lookup failure repeating for as long as the pane
        lives. That pane cannot be addressed and cannot be cleaned up.
        """
        with pytest.raises(terminal_service.IncarnationRegistrationRefused):
            terminal_service._register_incarnation(
                "no-such-id", GEN, IDENTITY, protocol_vintage="v2"
            )

    def test_the_refusal_names_the_terminal_and_the_observed_identity(self, isolated_memory_db):
        with pytest.raises(terminal_service.IncarnationRegistrationRefused) as raised:
            terminal_service._register_incarnation(
                "no-such-id", GEN, IDENTITY, protocol_vintage="v2"
            )

        assert raised.value.terminal_id == "no-such-id"
        assert raised.value.identity["pane_id"] == "%30"
        assert "no-such-id" in str(raised.value)
        assert database.REGISTRATION_ABSENT_ROW in str(raised.value)


class TestV1IsUnchanged:
    """The v1 path logs and continues, exactly as before.

    Its row is already correct by the time this runs, so this call can only
    confirm it, and tearing down a working terminal over a redundant write
    would be the worse error. Only the native precondition raises.
    """

    def test_an_absent_v1_row_still_returns_false_without_raising(self, isolated_memory_db):
        assert terminal_service._register_incarnation("no-such-id", GEN, IDENTITY) is False

    def test_a_partial_v1_identity_still_returns_false_without_raising(self, isolated_memory_db):
        database.create_terminal("v1abcdef", "cao-test", "w", "claude_code", generation=GEN)

        assert (
            terminal_service._register_incarnation("v1abcdef", GEN, {**IDENTITY, "pane_pid": None})
            is False
        )

    def test_a_v1_registration_still_succeeds(self, isolated_memory_db):
        database.create_terminal("v1abcdef", "cao-test", "w", "claude_code", generation=GEN)

        assert terminal_service._register_incarnation("v1abcdef", GEN, IDENTITY) is True
        assert database.get_terminal_metadata("v1abcdef")["pane_id"] == "%30"

    def test_the_bool_wrapper_agrees_with_the_typed_outcome(self, isolated_memory_db):
        """The bool form is a projection of the rules, not a second copy."""
        database.create_terminal("v1abcdef", "cao-test", "w", "claude_code", generation=GEN)

        outcome = database.register_terminal_incarnation_outcome(
            "v1abcdef", generation=GEN, **IDENTITY
        )
        assert outcome == database.REGISTRATION_OK
        assert database.register_terminal_incarnation("v1abcdef", generation=GEN, **IDENTITY)
