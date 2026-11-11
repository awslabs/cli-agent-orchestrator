"""Checking a YAML workflow's step params against their profiles' declarations.

Contract section 3: ``check_step_params`` loads each step's profile by name, once per
distinct name, and reports a missing required param or a value of the wrong shape. A
profile that is missing, fails to load, or declares nothing gives no findings.
"""

from __future__ import annotations

from typing import Dict, List

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.workflow import (
    StepParamError,
    ValidationResult,
    WorkflowSpec,
)
from cli_agent_orchestrator.services import step_params

ORDERS_PARAMS = {
    "table_name": {"description": "DynamoDB table name", "required": True},
    "key_type": {"required": True, "enum": ["S", "N", "B"]},
    "limit": {"type": "int", "default": 1},
    "dry_run": {"type": "bool"},
    "page": {"type": "int", "enum": [1, 5, 10]},
}


def _spec(*steps: Dict) -> WorkflowSpec:
    return WorkflowSpec(
        name="wf",
        steps=[
            {"id": f"s{i}", "provider": "claude_code", "prompt": "go", **step}
            for i, step in enumerate(steps, 1)
        ],
    )


@pytest.fixture
def profiles(monkeypatch: pytest.MonkeyPatch):
    """Serve profiles by name from a dict, and record every load."""
    store: Dict[str, object] = {}
    loads: List[str] = []

    def _load(name: str):
        loads.append(name)
        value = store.get(name)
        if value is None:
            raise FileNotFoundError(f"Agent profile not found: {name}")
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(step_params.agent_profiles, "load_agent_profile", _load)

    def _add(name: str, params=None, error: Exception | None = None) -> None:
        store[name] = error or AgentProfile(name=name, description="d", params=params)

    _add.loads = loads  # type: ignore[attr-defined]
    return _add


def _good() -> Dict[str, str]:
    return {"table_name": "orders", "key_type": "S"}


class TestFindings:
    def test_a_step_that_fits_has_no_findings(self, profiles) -> None:
        profiles("orders-agent", ORDERS_PARAMS)
        spec = _spec({"agent": "orders-agent", "params": {**_good(), "limit": "25"}})

        assert step_params.check_step_params(spec) == []

    def test_a_missing_required_param_names_the_step_and_the_profile(self, profiles) -> None:
        profiles("orders-agent", ORDERS_PARAMS)
        spec = _spec({"agent": "orders-agent", "params": {"key_type": "S"}})

        assert step_params.check_step_params(spec) == [
            "step 's1': missing required param 'table_name' (declared by profile 'orders-agent')"
        ]

    def test_a_step_without_params_misses_every_required_one(self, profiles) -> None:
        profiles("orders-agent", ORDERS_PARAMS)

        errors = step_params.check_step_params(_spec({"agent": "orders-agent"}))

        assert errors == [
            "step 's1': missing required param 'table_name' (declared by profile 'orders-agent')",
            "step 's1': missing required param 'key_type' (declared by profile 'orders-agent')",
        ]

    def test_an_empty_string_counts_as_missing(self, profiles) -> None:
        profiles("orders-agent", ORDERS_PARAMS)
        spec = _spec({"agent": "orders-agent", "params": {**_good(), "table_name": ""}})

        assert step_params.check_step_params(spec) == [
            "step 's1': missing required param 'table_name' (declared by profile 'orders-agent')"
        ]

    def test_an_optional_param_may_be_left_out_or_empty(self, profiles) -> None:
        profiles("orders-agent", ORDERS_PARAMS)
        spec = _spec({"agent": "orders-agent", "params": {**_good(), "limit": "", "page": ""}})

        assert step_params.check_step_params(spec) == []

    @pytest.mark.parametrize("value", ["0", "25", "-3", "9" * 18, "-" + "9" * 18])
    def test_whole_numbers_fit_an_int(self, profiles, value: str) -> None:
        profiles("orders-agent", ORDERS_PARAMS)
        spec = _spec({"agent": "orders-agent", "params": {**_good(), "limit": value}})

        assert step_params.check_step_params(spec) == []

    @pytest.mark.parametrize(
        "value", ["ten", "1.0", "+1", " 1", "1 ", "--1", "0x10", "1e3", "١٢", "1-"]
    )
    def test_anything_else_does_not_fit_an_int(self, profiles, value: str) -> None:
        profiles("orders-agent", ORDERS_PARAMS)
        spec = _spec({"agent": "orders-agent", "params": {**_good(), "limit": value}})

        assert step_params.check_step_params(spec) == [
            "step 's1': param 'limit' must be a whole number"
        ]

    @pytest.mark.parametrize("value", ["9" * 19, "-" + "1" * 19])
    def test_a_whole_number_over_18_digits_is_told_the_limit(self, profiles, value: str) -> None:
        profiles("orders-agent", ORDERS_PARAMS)
        spec = _spec({"agent": "orders-agent", "params": {**_good(), "limit": value}})

        assert step_params.check_step_params(spec) == [
            "step 's1': param 'limit' must be a whole number of at most 18 digits"
        ]

    @pytest.mark.parametrize("value", ["true", "false"])
    def test_true_and_false_fit_a_bool(self, profiles, value: str) -> None:
        profiles("orders-agent", ORDERS_PARAMS)
        spec = _spec({"agent": "orders-agent", "params": {**_good(), "dry_run": value}})

        assert step_params.check_step_params(spec) == []

    @pytest.mark.parametrize("value", ["True", "FALSE", "yes", "1", "0"])
    def test_anything_else_does_not_fit_a_bool(self, profiles, value: str) -> None:
        profiles("orders-agent", ORDERS_PARAMS)
        spec = _spec({"agent": "orders-agent", "params": {**_good(), "dry_run": value}})

        assert step_params.check_step_params(spec) == [
            "step 's1': param 'dry_run' must be true or false"
        ]

    def test_a_value_outside_the_enum_lists_the_values(self, profiles) -> None:
        profiles("orders-agent", ORDERS_PARAMS)
        spec = _spec({"agent": "orders-agent", "params": {**_good(), "key_type": "s"}})

        assert step_params.check_step_params(spec) == [
            "step 's1': param 'key_type' must be one of: S, N, B"
        ]

    def test_an_int_enum_value_is_matched_as_written(self, profiles) -> None:
        profiles("orders-agent", ORDERS_PARAMS)
        fits = _spec({"agent": "orders-agent", "params": {**_good(), "page": "5"}})
        padded = _spec({"agent": "orders-agent", "params": {**_good(), "page": "05"}})

        assert step_params.check_step_params(fits) == []
        assert step_params.check_step_params(padded) == [
            "step 's1': param 'page' must be one of: 1, 5, 10"
        ]

    def test_an_int_enum_value_that_is_not_a_number_gets_the_number_error(self, profiles) -> None:
        profiles("orders-agent", ORDERS_PARAMS)
        spec = _spec({"agent": "orders-agent", "params": {**_good(), "page": "five"}})

        assert step_params.check_step_params(spec) == [
            "step 's1': param 'page' must be a whole number"
        ]

    def test_an_enum_error_lists_at_most_ten_values(self, profiles) -> None:
        profiles("orders-agent", {"region": {"enum": [f"r{i}" for i in range(12)]}})
        spec = _spec({"agent": "orders-agent", "params": {"region": "elsewhere"}})

        assert step_params.check_step_params(spec) == [
            "step 's1': param 'region' must be one of: "
            "r0, r1, r2, r3, r4, r5, r6, r7, r8, r9, ..."
        ]

    def test_a_name_the_profile_does_not_declare_is_allowed(self, profiles) -> None:
        profiles("orders-agent", ORDERS_PARAMS)
        spec = _spec({"agent": "orders-agent", "params": {**_good(), "note": "anything"}})

        assert step_params.check_step_params(spec) == []

    def test_messages_never_echo_a_value(self, profiles) -> None:
        profiles("orders-agent", ORDERS_PARAMS)
        secretish = "orders-value-7f3a"
        spec = _spec(
            {
                "agent": "orders-agent",
                "params": {
                    "table_name": "orders",
                    "key_type": secretish,
                    "limit": secretish,
                    "dry_run": secretish,
                },
            }
        )

        errors = step_params.check_step_params(spec)

        assert len(errors) == 3
        assert all(secretish not in error for error in errors)

    def test_findings_follow_step_order_then_declaration_order(self, profiles) -> None:
        profiles("orders-agent", ORDERS_PARAMS)
        profiles("queue-agent", {"queue": {"required": True}})
        spec = _spec(
            {"agent": "queue-agent"},
            {"agent": "orders-agent", "params": {"limit": "x", "key_type": "Q"}},
        )

        assert step_params.check_step_params(spec) == [
            "step 's1': missing required param 'queue' (declared by profile 'queue-agent')",
            "step 's2': missing required param 'table_name' (declared by profile 'orders-agent')",
            "step 's2': param 'key_type' must be one of: S, N, B",
            "step 's2': param 'limit' must be a whole number",
        ]


class TestProfilesThatDeclareNothing:
    def test_a_missing_profile_gives_no_findings(self, profiles) -> None:
        assert step_params.check_step_params(_spec({"agent": "not-installed"})) == []

    @pytest.mark.parametrize(
        "error",
        [
            ValueError("params.limit: bad"),
            RuntimeError("Failed to load agent profile 'broken': boom"),
            OSError("permission denied"),
        ],
        ids=["invalid", "unreadable", "io"],
    )
    def test_a_profile_that_fails_to_load_gives_no_findings(self, profiles, error) -> None:
        profiles("broken", error=error)

        assert step_params.check_step_params(_spec({"agent": "broken"})) == []

    @pytest.mark.parametrize("params", [None, {}])
    def test_a_profile_without_a_declaration_gives_no_findings(self, profiles, params) -> None:
        profiles("plain-agent", params)

        assert step_params.check_step_params(_spec({"agent": "plain-agent"})) == []

    def test_a_profile_stand_in_without_params_is_ignored(self, monkeypatch) -> None:
        """A test double or an older object without the field declares nothing."""
        from unittest.mock import MagicMock

        monkeypatch.setattr(
            step_params.agent_profiles, "load_agent_profile", lambda name: MagicMock()
        )

        assert step_params.check_step_params(_spec({"agent": "anything"})) == []

    def test_each_distinct_profile_is_loaded_once(self, profiles) -> None:
        profiles("orders-agent", ORDERS_PARAMS)
        spec = _spec(
            {"agent": "orders-agent", "params": _good()},
            {"agent": "missing-agent"},
            {"agent": "orders-agent", "params": _good()},
            {"agent": "missing-agent"},
        )

        step_params.check_step_params(spec)

        assert profiles.loads == ["orders-agent", "missing-agent"]


class TestRealStore:
    """Through the real loader and a local profile store, with no stand-ins."""

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        from cli_agent_orchestrator.services import profile_store
        from cli_agent_orchestrator.utils import agent_profiles

        target = tmp_path / "agent-store"
        target.mkdir()
        monkeypatch.setattr(profile_store, "LOCAL_AGENT_STORE_DIR", target)
        monkeypatch.setattr(agent_profiles, "LOCAL_AGENT_STORE_DIR", target)
        return target

    def test_an_installed_profile_is_checked(self, store) -> None:
        (store / "orders-agent.md").write_text(
            "---\nname: orders-agent\ndescription: Reads orders\nparams:\n"
            "  table_name:\n    required: true\n---\n\nRead the orders table.\n",
            encoding="utf-8",
        )

        errors = step_params.check_step_params(_spec({"agent": "orders-agent"}))

        assert errors == [
            "step 's1': missing required param 'table_name' (declared by profile 'orders-agent')"
        ]

    def test_an_installed_profile_with_a_bad_declaration_gives_no_findings(self, store) -> None:
        (store / "orders-agent.md").write_text(
            "---\nname: orders-agent\ndescription: Reads orders\nparams:\n"
            "  table_name:\n    required: maybe\n---\n\nRead the orders table.\n",
            encoding="utf-8",
        )

        assert step_params.check_step_params(_spec({"agent": "orders-agent"})) == []

    def test_a_built_in_profile_declares_nothing(self, store) -> None:
        assert step_params.check_step_params(_spec({"agent": "developer"})) == []


class TestSurfaces:
    def test_refusing_raises_step_param_error_with_every_message(self, profiles) -> None:
        profiles("orders-agent", ORDERS_PARAMS)

        with pytest.raises(StepParamError) as excinfo:
            step_params.refuse_step_param_errors(_spec({"agent": "orders-agent"}))

        assert excinfo.value.errors == [
            "step 's1': missing required param 'table_name' (declared by profile 'orders-agent')",
            "step 's1': missing required param 'key_type' (declared by profile 'orders-agent')",
        ]
        assert str(excinfo.value) == "; ".join(excinfo.value.errors)
        assert isinstance(excinfo.value, ValueError)

    def test_refusing_passes_a_fitting_spec(self, profiles) -> None:
        profiles("orders-agent", ORDERS_PARAMS)

        step_params.refuse_step_param_errors(_spec({"agent": "orders-agent", "params": _good()}))

    def test_validate_adds_the_errors_and_fails(self, profiles) -> None:
        profiles("orders-agent", ORDERS_PARAMS)
        spec = _spec({"agent": "orders-agent", "params": {"table_name": "orders"}})

        result = step_params.with_step_param_errors(ValidationResult(status="pass"), spec)

        assert result == ValidationResult(
            status="fail",
            errors=[
                "step 's1': missing required param 'key_type' (declared by profile 'orders-agent')"
            ],
        )

    def test_validate_keeps_reserved_notes(self, profiles) -> None:
        profiles("orders-agent", ORDERS_PARAMS)
        notes = ["construct 'assign' is reserved (not built yet)"]
        passed = ValidationResult(status="pass_reserved", reserved_notes=notes)

        result = step_params.with_step_param_errors(passed, _spec({"agent": "orders-agent"}))

        assert result.status == "fail"
        assert result.reserved_notes == notes
        assert len(result.errors) == 2

    def test_validate_leaves_a_fitting_result_as_it_was(self, profiles) -> None:
        profiles("orders-agent", ORDERS_PARAMS)
        passed = ValidationResult(status="pass")
        spec = _spec({"agent": "orders-agent", "params": _good()})

        assert step_params.with_step_param_errors(passed, spec) is passed

    def test_validate_skips_the_check_when_grammar_failed(self, profiles) -> None:
        failed = ValidationResult(status="fail", errors=["grammar"])

        assert step_params.with_step_param_errors(failed, None) is failed
        assert profiles.loads == []
