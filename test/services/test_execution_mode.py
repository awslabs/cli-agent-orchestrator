"""Closed immutable execution-mode contract (native_tui | acp).

Covers the acceptance matrix row for the mode contract: the precedence
resolution table, rejection of conflicting inputs, impossibility of an
in-place mutation, and the rule that a row predating the contract is
legacy ACP and never native.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.services import execution_mode as em


class TestClosedEnum:
    def test_exactly_two_modes_exist(self):
        assert em.EXECUTION_MODES == {"native_tui", "acp"}

    @pytest.mark.parametrize(
        "value",
        ["Native_TUI", "NATIVE_TUI", " native_tui", "native-tui", "tui", "", None, 1, True],
    )
    def test_non_member_values_reject(self, value):
        with pytest.raises(em.ExecutionModeInvalid):
            em.validate_mode(value)

    def test_members_pass_through_unchanged(self):
        assert em.validate_mode("native_tui") == "native_tui"
        assert em.validate_mode("acp") == "acp"


class TestPrecedenceTable:
    """launch input > task input > profile default > class default."""

    def test_launch_input_wins_over_profile_default(self):
        resolved = em.resolve(launch_input="native_tui", profile_default="acp")
        assert resolved.mode == "native_tui"
        assert resolved.source == em.SOURCE_LAUNCH

    def test_task_input_wins_over_profile_default(self):
        resolved = em.resolve(task_input="native_tui", profile_default="acp")
        assert resolved.mode == "native_tui"
        assert resolved.source == em.SOURCE_TASK

    def test_launch_input_wins_over_agreeing_task_input(self):
        resolved = em.resolve(launch_input="acp", task_input="acp", profile_default="native_tui")
        assert resolved.mode == "acp"
        assert resolved.source == em.SOURCE_LAUNCH

    def test_profile_default_used_when_no_explicit_input(self):
        resolved = em.resolve(profile_default="native_tui")
        assert resolved.mode == "native_tui"
        assert resolved.source == em.SOURCE_PROFILE_DEFAULT

    @pytest.mark.parametrize("worker_class", ["persistent", "long_running", "human_monitored"])
    def test_native_is_the_default_for_persistent_classes(self, worker_class):
        resolved = em.resolve(worker_class=worker_class)
        assert resolved.mode == "native_tui"
        assert resolved.source == em.SOURCE_CLASS_DEFAULT

    @pytest.mark.parametrize("worker_class", ["one_shot", "hands_off", "unspecified", None])
    def test_acp_remains_the_default_for_non_persistent_classes(self, worker_class):
        """A caller naming neither mode nor a persistent class keeps ACP.

        This is what makes the module safe to deploy ahead of the
        mode-aware caller: an existing fleet that sends no mode does not
        silently move onto the native branch.
        """
        resolved = em.resolve(worker_class=worker_class)
        assert resolved.mode == "acp"

    def test_acp_requires_explicit_opt_in_on_a_persistent_class(self):
        resolved = em.resolve(launch_input="acp", worker_class="persistent")
        assert resolved.mode == "acp"
        assert resolved.source == em.SOURCE_LAUNCH

    def test_resolution_records_every_offered_input(self):
        resolved = em.resolve(launch_input="native_tui", task_input="native_tui")
        assert resolved.as_dict()["execution_mode_inputs"] == {
            "launch": "native_tui",
            "task": "native_tui",
            "profile_default": None,
        }


class TestConflictsReject:
    def test_conflicting_launch_and_task_inputs_reject(self):
        with pytest.raises(em.ExecutionModeConflict):
            em.resolve(launch_input="native_tui", task_input="acp")

    def test_conflict_rejects_in_both_orders(self):
        with pytest.raises(em.ExecutionModeConflict):
            em.resolve(launch_input="acp", task_input="native_tui")

    def test_conflict_rejects_even_when_profile_default_agrees_with_one_side(self):
        with pytest.raises(em.ExecutionModeConflict):
            em.resolve(launch_input="acp", task_input="native_tui", profile_default="acp")

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"launch_input": "bogus"},
            {"task_input": "bogus"},
            {"profile_default": "bogus"},
        ],
    )
    def test_invalid_value_at_any_precedence_level_rejects(self, kwargs):
        """Even a level a higher-precedence input would have overruled.

        A caller must never learn that its malformed field was quietly
        ignored because something else happened to win.
        """
        kwargs = {"launch_input": "native_tui", **kwargs}
        with pytest.raises(em.ExecutionModeInvalid):
            em.resolve(**kwargs)

    def test_invalid_worker_class_rejects(self):
        with pytest.raises(em.ExecutionModeInvalid):
            em.resolve(worker_class="supervisor")


class TestImmutability:
    def test_restating_the_bound_mode_is_accepted(self):
        assert em.assert_immutable("native_tui", "native_tui") == "native_tui"

    def test_silence_is_not_a_change(self):
        assert em.assert_immutable("native_tui", None) == "native_tui"

    def test_changing_a_bound_mode_is_refused(self):
        with pytest.raises(em.ExecutionModeConflict):
            em.assert_immutable("native_tui", "acp")

    def test_changing_a_bound_acp_mode_to_native_is_refused(self):
        with pytest.raises(em.ExecutionModeConflict):
            em.assert_immutable("acp", "native_tui")

    def test_an_unbound_row_cannot_be_mutated_into_a_mode(self):
        with pytest.raises(em.ExecutionModeInvalid):
            em.assert_immutable(None, "native_tui")


class TestLegacyRows:
    def test_a_row_without_a_mode_is_legacy_acp(self):
        assert em.mode_of_record({"reservation_id": "r"}) == "acp"
        assert em.source_of_record({"reservation_id": "r"}) == em.SOURCE_LEGACY
        assert em.is_legacy_row({"reservation_id": "r"}) is True

    def test_an_absent_row_is_legacy_acp(self):
        assert em.mode_of_record(None) == "acp"
        assert em.is_legacy_row(None) is True

    def test_an_explicit_null_mode_is_legacy_acp_never_native(self):
        record = {"execution_mode": None, "execution_mode_source": "launch"}
        assert em.mode_of_record(record) == "acp"
        assert em.source_of_record(record) == em.SOURCE_LEGACY

    def test_a_native_row_reports_native(self):
        record = {"execution_mode": "native_tui", "execution_mode_source": "launch"}
        assert em.mode_of_record(record) == "native_tui"
        assert em.source_of_record(record) == "launch"
        assert em.is_legacy_row(record) is False

    def test_a_corrupt_persisted_mode_rejects_rather_than_defaulting(self):
        with pytest.raises(em.ExecutionModeInvalid):
            em.mode_of_record({"execution_mode": "native"})


class TestModeCrossingRefused:
    def test_matching_modes_pass(self):
        assert em.assert_same_mode("native_tui", "native_tui", context="delivery") == "native_tui"

    def test_acp_operation_against_a_native_session_is_refused(self):
        with pytest.raises(em.ExecutionModeConflict):
            em.assert_same_mode("acp", "native_tui", context="delivery")

    def test_native_operation_against_an_acp_session_is_refused(self):
        with pytest.raises(em.ExecutionModeConflict):
            em.assert_same_mode("native_tui", "acp", context="delivery")

    def test_a_legacy_side_resolves_to_acp_and_refuses_a_native_peer(self):
        with pytest.raises(em.ExecutionModeConflict):
            em.assert_same_mode(None, "native_tui", context="delivery")

    def test_two_legacy_sides_match_as_acp(self):
        assert em.assert_same_mode(None, None, context="delivery") == "acp"
