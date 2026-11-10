"""A step param value may not contain a ``{{...}}`` placeholder (contract section 2).

Param values are fixed when the workflow is built. Before this rule, a value such as
``{{workflow.inputs.table}}`` reached the agent as literal text; now the grammar check
refuses it, so it fails at save, validate and run alike. Single braces stay allowed.
"""

from __future__ import annotations

import random
import re

import pytest

from cli_agent_orchestrator.constants import WORKFLOW_PLACEHOLDER_RE
from cli_agent_orchestrator.models.workflow import WorkflowSpec, _has_placeholder, validate_only
from cli_agent_orchestrator.services import workflow_service

_ENGINE = re.compile(WORKFLOW_PLACEHOLDER_RE)


def _spec(value: str, key: str = "table") -> str:
    import json

    return (
        "name: wf\n"
        "steps:\n"
        "  - id: s1\n"
        "    provider: claude_code\n"
        "    agent: orders-agent\n"
        "    prompt: read the orders\n"
        "    params:\n"
        f"      {key}: {json.dumps(value)}\n"
    )


def _message(key: str = "table") -> str:
    return (
        f"step 's1': param '{key}' contains a {{{{...}}}} placeholder. "
        "Param values are fixed when the workflow is built."
    )


class TestThePattern:
    def test_the_constant_is_the_engines_pattern(self) -> None:
        assert WORKFLOW_PLACEHOLDER_RE == workflow_service._PLACEHOLDER_RE.pattern

    @pytest.mark.parametrize(
        "value",
        [
            "{{workflow.inputs.table}}",
            "{{ steps.read.output.count }}",
            "prefix-{{x}}-suffix",
            "{{ }}",
            "{{{x}}}",
            "{{a b}}",
        ],
    )
    def test_the_check_finds_what_the_engine_would_resolve(self, value: str) -> None:
        assert _ENGINE.search(value) is not None
        assert _has_placeholder(value)

    @pytest.mark.parametrize(
        "value",
        ['{"a": 1}', "{a}", "{{}}", "{{a}", "{a}}", "}}{{", "{ {a} }", "{{a{b}}", "plain"],
    )
    def test_the_check_passes_what_the_engine_would_leave(self, value: str) -> None:
        assert _ENGINE.search(value) is None
        assert not _has_placeholder(value)

    def test_the_check_agrees_with_the_engines_pattern_on_generated_values(self) -> None:
        """Whitespace runs are cut before matching; the answer must not change."""
        rng = random.Random(1145)
        alphabet = ["{", "}", " ", "\t", "\u00a0", "\u3000", "a", ".", "-"]
        for _ in range(20_000):
            value = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 16)))
            assert _has_placeholder(value) == (_ENGINE.search(value) is not None), repr(value)

    @pytest.mark.parametrize(
        "value",
        [
            "{{" + " " * 510,
            "{{" + " " * 505 + "{}}",
            ("{{" + " " * 60 + "{") * 7 + "}}",
        ],
    )
    def test_long_whitespace_runs_get_the_same_answer(self, value: str) -> None:
        """The shapes where the raw pattern backtracks most."""
        assert _has_placeholder(value) == (_ENGINE.search(value) is not None)


class TestTheGrammarRule:
    @pytest.mark.parametrize(
        "value", ["{{workflow.inputs.table}}", "orders-{{ steps.a.output.b }}"]
    )
    def test_a_placeholder_fails_with_the_contract_message(self, value: str) -> None:
        result = validate_only(_spec(value))

        assert result.status == "fail"
        assert result.errors == [_message()]

    @pytest.mark.parametrize("value", ['{"a": 1}', "{orders}", "orders"])
    def test_single_braces_pass(self, value: str) -> None:
        assert validate_only(_spec(value)).status == "pass"

    def test_the_message_never_echoes_the_value(self) -> None:
        result = validate_only(_spec("{{workflow.inputs.secret_table}}"))

        assert "secret_table" not in " ".join(result.errors)

    def test_construction_refuses_it_too(self) -> None:
        """Every path that builds a spec (save, validate, run) goes through the grammar."""
        with pytest.raises(ValueError, match="placeholder"):
            WorkflowSpec(
                name="wf",
                steps=[
                    {
                        "id": "s1",
                        "provider": "claude_code",
                        "agent": "orders-agent",
                        "prompt": "go",
                        "params": {"table": "{{workflow.inputs.table}}"},
                    }
                ],
            )

    def test_it_is_aggregated_with_the_other_param_errors(self) -> None:
        text = (
            "name: wf\n"
            "steps:\n"
            "  - id: s1\n"
            "    provider: claude_code\n"
            "    agent: orders-agent\n"
            "    prompt: go\n"
            "    params:\n"
            '      table: "{{x}}"\n'
            "      limit: 5\n"
        )

        errors = validate_only(text).errors

        assert _message() in errors
        assert "step 's1': param 'limit' must be a string, not a number (quote the value)" in (
            errors
        )

    def test_a_prompt_placeholder_is_still_allowed(self) -> None:
        """The rule is about param values only; the prompt keeps its references."""
        text = (
            "name: wf\n"
            "inputs:\n"
            "  table: {type: string}\n"
            "steps:\n"
            "  - id: s1\n"
            "    provider: claude_code\n"
            "    agent: orders-agent\n"
            '    prompt: "read {{workflow.inputs.table}}"\n'
        )

        assert validate_only(text).status == "pass"
