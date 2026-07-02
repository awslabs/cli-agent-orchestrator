"""Tests for the workflow spec grammar + validation (issue #312, unit N1).

Covers WorkflowSpec.validate_grammar aggregation (BR-1/BR-4/BR-5/BR-6/BR-7) and
the non-raising validate_only service surface (FR-1.3), including the
reserved-construct honesty invariant (BR-3) that guards the cross-Bolt
WORKFLOW_SHIPPED_UNITS flag flip.
"""

import pytest

from cli_agent_orchestrator.models.workflow import (
    NotBuiltYetError,
    ValidationResult,
    WorkflowSpec,
    is_reserved,
    validate_only,
)

_SEQ_YAML = """\
name: my-workflow
description: a simple sequential flow
steps:
  - id: step1
    provider: kiro_cli
    agent: developer
    prompt: implement the feature
  - id: step2
    provider: kiro_cli
    agent: reviewer
    prompt: review the change
"""


def _spec_kwargs(**overrides):
    base = {
        "name": "wf",
        "steps": [{"id": "s1", "provider": "kiro_cli", "agent": "dev", "prompt": "go"}],
    }
    base.update(overrides)
    return base


class TestHappyPath:
    def test_valid_sequential_spec_constructs(self):
        spec = WorkflowSpec(**_spec_kwargs())
        assert spec.mode == "sequential"
        assert spec.steps[0].id == "s1"

    def test_validate_only_sequential_passes(self):
        result = validate_only(_SEQ_YAML)
        assert result.status == "pass"
        assert result.reserved_notes == []
        assert result.errors == []


class TestNameValidation:
    def test_name_violations_aggregate_together(self):
        """Bad spec name + bad step id + duplicate id are reported TOGETHER
        (BR-1/BR-7: never fail-on-first)."""
        yaml_text = """\
name: "bad name!"
steps:
  - id: ok_id
    provider: p
    agent: a
    prompt: x
  - id: ok_id
    provider: p
    agent: a
    prompt: y
  - id: "bad id!"
    provider: p
    agent: a
    prompt: z
"""
        result = validate_only(yaml_text)
        assert result.status == "fail"
        joined = " ".join(result.errors)
        assert "workflow name 'bad name!'" in joined
        assert "duplicate step id 'ok_id'" in joined
        assert "bad id!" in joined
        # All three distinct problems surfaced, not just the first.
        assert len(result.errors) >= 3

    def test_valid_name_with_dashes_and_underscores(self):
        spec = WorkflowSpec(**_spec_kwargs(name="My-Workflow_1"))
        assert spec.name == "My-Workflow_1"

    def test_name_too_long_fails(self):
        result = validate_only(_spec_yaml_with_name("a" * 65))
        assert result.status == "fail"


def _spec_yaml_with_name(name: str) -> str:
    return f"""\
name: "{name}"
steps:
  - id: s1
    provider: p
    agent: a
    prompt: x
"""


class TestTypedInputs:
    def test_default_type_mismatch_fails(self):
        result = validate_only("""\
name: wf
inputs:
  count:
    type: int
    default: "not-a-number"
steps:
  - id: s1
    provider: p
    agent: a
    prompt: x
""")
        assert result.status == "fail"
        assert any("default" in e and "count" in e for e in result.errors)

    def test_bool_default_not_satisfied_by_int(self):
        # bool is a subclass of int; ensure an int default does NOT satisfy bool.
        with pytest.raises(ValueError):
            WorkflowSpec(**_spec_kwargs(inputs={"flag": {"type": "bool", "default": 1}}))

    def test_unknown_input_type_fails(self):
        # Pydantic Literal rejects the unknown type before validate_grammar; the
        # service still returns a structured fail (never raises).
        result = validate_only("""\
name: wf
inputs:
  x:
    type: float
steps:
  - id: s1
    provider: p
    agent: a
    prompt: x
""")
        assert result.status == "fail"

    def test_valid_typed_defaults_pass(self):
        spec = WorkflowSpec(
            **_spec_kwargs(
                inputs={
                    "name": {"type": "string", "default": "hi"},
                    "n": {"type": "int", "default": 3},
                    "flag": {"type": "bool", "default": True},
                    "p": {"type": "path", "default": "/tmp/x"},
                }
            )
        )
        assert set(spec.inputs) == {"name", "n", "flag", "p"}


class TestOutputSchema:
    def test_malformed_output_schema_rejected(self):
        """{'type': 123} is not valid JSON-Schema -> fail (BR-6/SD-3.1)."""
        result = validate_only("""\
name: wf
steps:
  - id: s1
    provider: p
    agent: a
    prompt: x
    output_schema:
      type: 123
""")
        assert result.status == "fail"
        assert any("output_schema is not valid JSON-Schema" in e for e in result.errors)

    def test_well_formed_output_schema_passes(self):
        spec = WorkflowSpec(
            **_spec_kwargs(
                steps=[
                    {
                        "id": "s1",
                        "provider": "p",
                        "agent": "a",
                        "prompt": "x",
                        "output_schema": {
                            "type": "object",
                            "properties": {"ok": {"type": "boolean"}},
                            "required": ["ok"],
                        },
                    }
                ]
            )
        )
        assert spec.steps[0].output_schema is not None

    def test_over_depth_output_schema_fails(self):
        # Build a deeply nested dict beyond WORKFLOW_OUTPUT_SCHEMA_MAX_DEPTH (8).
        nested = {"type": "object"}
        cur = nested
        for _ in range(12):
            child = {"type": "object"}
            cur["properties"] = {"x": child}
            cur = child
        result = validate_only(_yaml_with_schema(nested))
        assert result.status == "fail"
        assert any("depth" in e for e in result.errors)


def _yaml_with_schema(schema) -> str:
    import yaml as _yaml

    doc = {
        "name": "wf",
        "steps": [
            {
                "id": "s1",
                "provider": "p",
                "agent": "a",
                "prompt": "x",
                "output_schema": schema,
            }
        ],
    }
    return _yaml.safe_dump(doc)


class TestLoopGuards:
    def test_loop_missing_guard_fails(self):
        """A step with a loop field but not all three guards fails (BR-4)."""
        result = validate_only("""\
name: wf
steps:
  - id: s1
    provider: p
    agent: a
    prompt: x
    until: done
""")
        assert result.status == "fail"
        assert any("all three guards" in e for e in result.errors)

    def test_loop_with_all_three_guards_no_guard_error(self):
        """All three guards present -> not a guard error (still pass_reserved
        because loop execution is N8, not shipped in Bolt 1)."""
        result = validate_only("""\
name: wf
steps:
  - id: s1
    provider: p
    agent: a
    prompt: x
    until: done
    max_iterations: 5
    on_stall: halt
    on_no_progress: halt
""")
        # No guard error; the construct is reserved, not invalid.
        assert result.status == "pass_reserved"
        assert all("guards" not in e for e in result.errors)


class TestReservedConstructs:
    def test_parallel_mode_pass_reserved_with_honest_note(self):
        result = validate_only("""\
name: wf
mode: parallel
steps:
  - id: s1
    provider: p
    agent: a
    prompt: x
""")
        assert result.status == "pass_reserved"
        assert result.reserved_notes
        note = result.reserved_notes[0]
        # Honesty invariant (BR-3): "reserved (not built yet)", never "will run".
        assert "reserved" in note
        assert "not built yet" in note
        assert "will run" not in note

    def test_loop_construct_pass_reserved(self):
        result = validate_only("""\
name: wf
mode: loop
steps:
  - id: s1
    provider: p
    agent: a
    prompt: x
    until: done
    max_iterations: 5
    on_stall: halt
    on_no_progress: halt
""")
        assert result.status == "pass_reserved"
        assert any("not built yet" in n for n in result.reserved_notes)

    def test_sequential_only_passes_not_reserved(self):
        """Guards the cross-Bolt flag flip: a sequential-only spec is `pass`,
        NOT `pass_reserved`."""
        result = validate_only(_SEQ_YAML)
        assert result.status == "pass"
        assert result.reserved_notes == []

    def test_is_reserved_for_known_and_unknown_constructs(self):
        # In Bolt 1, WORKFLOW_SHIPPED_UNITS is empty -> all mapped constructs reserved.
        assert is_reserved("parallel") is True
        assert is_reserved("loop") is True
        assert is_reserved("when") is True
        # An unknown construct is not reserved (not in registry).
        assert is_reserved("totally-unknown") is False


class TestServiceSurfaceNeverRaises:
    def test_malformed_yaml_returns_fail_not_raise(self):
        result = validate_only("name: [unclosed\n")
        assert isinstance(result, ValidationResult)
        assert result.status == "fail"
        assert result.errors

    def test_non_mapping_root_fails(self):
        result = validate_only("- just\n- a\n- list\n")
        assert result.status == "fail"
        assert any("mapping" in e for e in result.errors)

    def test_oversized_spec_fails_pre_parse(self):
        # Exceed WORKFLOW_MAX_SPEC_BYTES (256 KiB) with valid-ish YAML padding.
        big = "name: wf\ndescription: " + ("x" * (256 * 1024 + 10)) + "\nsteps: []\n"
        result = validate_only(big)
        assert result.status == "fail"
        assert any("byte cap" in e for e in result.errors)

    def test_non_string_mapping_key_returns_fail_not_raise(self):
        """A YAML mapping with a non-string key (``123: foo``) makes
        ``WorkflowSpec(**data)`` raise TypeError on ``**``-unpacking; that must
        be caught and returned as fail, never escape (FR-1.3 never-raise)."""
        result = validate_only("123: foo\nname: wf\nsteps: []\n")
        assert isinstance(result, ValidationResult)
        assert result.status == "fail"
        assert result.errors

    def test_over_cap_nested_output_schema_returns_fail_not_raise(self):
        """An output_schema nested past the depth cap returns a fail with a
        'depth' reason — never raises out of validate_only (FR-1.3)."""
        import yaml

        from cli_agent_orchestrator.constants import WORKFLOW_OUTPUT_SCHEMA_MAX_DEPTH

        nested: dict = {}
        cur = nested
        for _ in range(WORKFLOW_OUTPUT_SCHEMA_MAX_DEPTH + 5):
            child: dict = {}
            cur["a"] = child
            cur = child
        spec_doc = {
            "name": "wf",
            "steps": [
                {"id": "s1", "provider": "p", "agent": "a", "prompt": "x", "output_schema": nested}
            ],
        }
        result = validate_only(yaml.dump(spec_doc))
        assert isinstance(result, ValidationResult)
        assert result.status == "fail"
        assert any("depth" in e for e in result.errors)

    def test_schema_depth_short_circuits_past_recursion_limit(self):
        """_schema_depth must stop descending once it passes the cap, so a
        structure deeper than Python's recursion limit cannot raise
        RecursionError (the bug behind the validate_only never-raise breach)."""
        import sys

        from cli_agent_orchestrator.models.workflow import _schema_depth

        nested: dict = {}
        cur = nested
        for _ in range(sys.getrecursionlimit() + 500):
            child: dict = {}
            cur["a"] = child
            cur = child
        # Returns just over the cap without fully descending — no RecursionError.
        assert _schema_depth(nested, _cap=8) > 8
        # And a full (uncapped) descent on the SAME structure WOULD blow the
        # stack — proving the cap is what protects validate_only.
        with pytest.raises(RecursionError):
            _schema_depth(nested)


class TestSizeCaps:
    def test_too_many_steps_fails(self):
        from cli_agent_orchestrator.constants import WORKFLOW_MAX_STEPS

        steps = [
            {"id": f"s{i}", "provider": "p", "agent": "a", "prompt": "x"}
            for i in range(WORKFLOW_MAX_STEPS + 1)
        ]
        with pytest.raises(ValueError, match="steps"):
            WorkflowSpec(name="wf", steps=steps)

    def test_empty_steps_fails(self):
        with pytest.raises(ValueError):
            WorkflowSpec(name="wf", steps=[])


class TestNotBuiltYetError:
    def test_defined_but_not_raised_in_bolt1(self):
        """N1 only DEFINES NotBuiltYetError; the engine (N5) raises it. Confirm
        it exists and is an Exception subclass — nothing in Bolt 1 raises it."""
        assert issubclass(NotBuiltYetError, Exception)


class TestBolt3AdditiveModelChanges:
    """B3-BR-3/B3-BR-12/B3-BR-13: the retries field + CANCELLED enum are additive
    and regression-safe — a spec without retries behaves exactly as before."""

    def test_spec_without_retries_unchanged(self):
        """A spec that omits ``retries`` parses exactly as before (retries None)."""
        spec = WorkflowSpec(**_spec_kwargs())
        assert spec.steps[0].retries is None
        # validate_only still classifies a plain sequential spec as pass.
        assert validate_only(_SEQ_YAML).status == "pass"

    def test_retries_in_range_accepted(self):
        for n in (0, 1, 10):
            spec = WorkflowSpec(
                **_spec_kwargs(
                    steps=[
                        {
                            "id": "s1",
                            "provider": "kiro_cli",
                            "agent": "dev",
                            "prompt": "go",
                            "retries": n,
                        }
                    ]
                )
            )
            assert spec.steps[0].retries == n

    def test_retries_out_of_range_aggregated_error(self):
        with pytest.raises(ValueError, match="out of range"):
            WorkflowSpec(
                **_spec_kwargs(
                    steps=[
                        {
                            "id": "s1",
                            "provider": "kiro_cli",
                            "agent": "dev",
                            "prompt": "go",
                            "retries": 11,
                        }
                    ]
                )
            )

    def test_retries_out_of_range_float_or_huge_rejected(self):
        """A retries value above the cap is an aggregated grammar error even when
        well-typed (guards the B3-PERF-4 worst-case ceiling)."""
        with pytest.raises(ValueError, match="out of range"):
            WorkflowSpec(
                **_spec_kwargs(
                    steps=[
                        {
                            "id": "s1",
                            "provider": "kiro_cli",
                            "agent": "dev",
                            "prompt": "go",
                            "retries": 100,
                        }
                    ]
                )
            )

    def test_run_state_cancelled_is_purely_additive(self):
        from cli_agent_orchestrator.models.workflow import RunState

        # Existing values are unchanged; CANCELLED is the only new member.
        assert RunState.RUNNING.value == "running"
        assert RunState.COMPLETED.value == "completed"
        assert RunState.FAILED.value == "failed"
        assert RunState.CANCELLED.value == "cancelled"
        assert {s.value for s in RunState} == {"running", "completed", "failed", "cancelled"}
