"""The ``params`` block of an agent profile: the params a workflow step must fill.

``AgentProfile.params`` is CAO-native frontmatter. These tests pin the model's rules and
the JSON shape ``GET /agents/profiles/{name}`` returns for it. The profile validator and
the JSON-Schema are held to the same rules in
``test/services/test_profile_params_validation.py``.
"""

from __future__ import annotations

import json
import logging
import random

import pytest
from pydantic import ValidationError

from cli_agent_orchestrator.models.agent_profile import (
    AgentProfile,
    ProfileParamDecl,
    param_declaration_problems,
)
from cli_agent_orchestrator.models.workflow import _has_placeholder
from cli_agent_orchestrator.services.profile_validator import validate_frontmatter
from cli_agent_orchestrator.utils.agent_profiles import parse_agent_profile_text

_MODEL_LOGGER = "cli_agent_orchestrator.models.agent_profile"

# Every line break other than "\n" and "\r": the rest of what ``str.splitlines()`` splits on.
OTHER_LINE_BREAKS = {
    "VT": "\v",
    "FF": "\f",
    "FS": "\x1c",
    "GS": "\x1d",
    "RS": "\x1e",
    "NEL": "\x85",
    "LS": "\u2028",
    "PS": "\u2029",
}


def _profile(params: object) -> AgentProfile:
    return AgentProfile(name="orders-agent", description="Reads orders", params=params)


# Every rule in contract section 1, one case each: the params block, the path of the
# problem below ``params``, and a fragment of the message.
INVALID = {
    "33 params": ({f"p{i}": {} for i in range(33)}, (), "declares 33 params (max 32)"),
    "not a mapping": (["table_name"], (), "must be a mapping of names to declarations"),
    "name starts with a digit": ({"1table": {}}, (), "param name '1table' is invalid"),
    "name with a space": ({"table name": {}}, (), "param name 'table name' is invalid"),
    "name of 65 characters": ({"t" * 65: {}}, (), "is invalid"),
    "name with a trailing newline": ({"table\n": {}}, (), "is invalid"),
    "name not a string": ({1: {}}, (), "param name 1 is invalid"),
    "entry not a mapping": ({"table": "orders"}, ("table",), "must be a mapping, not a string"),
    "unknown key": ({"table": {"label": "x"}}, ("table",), "key 'label' is not supported"),
    "pattern key": ({"table": {"pattern": "^o"}}, ("table",), "key 'pattern' is not supported"),
    "minimum key": ({"n": {"type": "int", "minimum": 1}}, ("n",), "key 'minimum' is not"),
    "maximum key": ({"n": {"type": "int", "maximum": 9}}, ("n",), "key 'maximum' is not"),
    "maxLength key": ({"t": {"maxLength": 9}}, ("t",), "key 'maxLength' is not supported"),
    "unknown type": ({"t": {"type": "float"}}, ("t", "type"), "type must be string, int or bool"),
    "type not a string": ({"t": {"type": 1}}, ("t", "type"), "type must be string, int or bool"),
    "required as a string": (
        {"t": {"required": "yes"}},
        ("t", "required"),
        "required must be true or false, not a string",
    ),
    "required as a number": (
        {"t": {"required": 1}},
        ("t", "required"),
        "required must be true or false, not a whole number",
    ),
    "required with a default": (
        {"t": {"required": True, "default": "orders"}},
        ("t", "default"),
        "a required param cannot have a default",
    ),
    "string default not a string": (
        {"t": {"default": 5}},
        ("t", "default"),
        "default must be a string, not a whole number (quote the value)",
    ),
    "string default of 513 characters": (
        {"t": {"default": "o" * 513}},
        ("t", "default"),
        "default is 513 characters (max 512)",
    ),
    "string default with a line break": (
        {"t": {"default": "a\nb"}},
        ("t", "default"),
        "default must be one line",
    ),
    "explicit null default": (
        {"t": {"default": None}},
        ("t", "default"),
        "default must be a string, not null",
    ),
    "int default as a string": (
        {"n": {"type": "int", "default": "1"}},
        ("n", "default"),
        "default must be a whole number, not a string",
    ),
    "int default as a YAML bool": (
        {"n": {"type": "int", "default": True}},
        ("n", "default"),
        "default must be a whole number, not a boolean",
    ),
    "int default with a decimal point": (
        {"n": {"type": "int", "default": 1.0}},
        ("n", "default"),
        "default must be a whole number, not a decimal number",
    ),
    "int default of 19 digits": (
        {"n": {"type": "int", "default": 10**18}},
        ("n", "default"),
        "at most 18 digits",
    ),
    "bool default as a string": (
        {"f": {"type": "bool", "default": "true"}},
        ("f", "default"),
        "default must be true or false, not a string",
    ),
    "bool default as a number": (
        {"f": {"type": "bool", "default": 1}},
        ("f", "default"),
        "default must be true or false, not a whole number",
    ),
    "default not in the enum": (
        {"k": {"enum": ["S", "N"], "default": "B"}},
        ("k", "default"),
        "default must be one of the enum values",
    ),
    "int default not in the enum": (
        {"n": {"type": "int", "enum": [1, 2], "default": 3}},
        ("n", "default"),
        "default must be one of the enum values",
    ),
    "description not a string": (
        {"t": {"description": 5}},
        ("t", "description"),
        "description must be a string",
    ),
    "description of 201 characters": (
        {"t": {"description": "d" * 201}},
        ("t", "description"),
        "description is 201 characters (max 200)",
    ),
    "description with a line break": (
        {"t": {"description": "line one\nline two"}},
        ("t", "description"),
        "description must be one line",
    ),
    "description with a carriage return": (
        {"t": {"description": "line one\rline two"}},
        ("t", "description"),
        "description must be one line",
    ),
    "enum not a list": ({"k": {"enum": "S"}}, ("k", "enum"), "enum must be a list, not a string"),
    "empty enum": ({"k": {"enum": []}}, ("k", "enum"), "enum has 0 values (it must have 1 to 32)"),
    "enum of 33 values": (
        {"k": {"enum": [f"v{i}" for i in range(33)]}},
        ("k", "enum"),
        "enum has 33 values",
    ),
    "repeated enum value": (
        {"k": {"enum": ["S", "N", "S"]}},
        ("k", "enum"),
        "enum lists a value more than once",
    ),
    "enum on a bool param": (
        {"f": {"type": "bool", "enum": [True, False]}},
        ("f", "enum"),
        "a bool param cannot have an enum",
    ),
    "string enum value not a string": (
        {"k": {"enum": ["S", 1]}},
        ("k", "enum", 1),
        "enum[1] must be a string, not a whole number",
    ),
    "string enum value of 513 characters": (
        {"k": {"enum": ["o" * 513]}},
        ("k", "enum", 0),
        "enum[0] is 513 characters (max 512)",
    ),
    "string enum value with a line break": (
        {"k": {"enum": ["a\nb"]}},
        ("k", "enum", 0),
        "enum[0] must be one line",
    ),
    "int enum value as a string": (
        {"n": {"type": "int", "enum": [1, "2"]}},
        ("n", "enum", 1),
        "enum[1] must be a whole number, not a string",
    ),
    "int enum value as a YAML bool": (
        {"n": {"type": "int", "enum": [True]}},
        ("n", "enum", 0),
        "enum[0] must be a whole number, not a boolean",
    ),
    # A string default or enum value has no {{...}} placeholder (profile params editor
    # contract, section 1b). The builder copies it into a step's params.
    "string default with a placeholder": (
        {"t": {"default": "orders-{{run_id}}"}},
        ("t", "default"),
        "default contains a {{...}} placeholder",
    ),
    "placeholder default with an enum": (
        {"k": {"enum": ["S", "N"], "default": "{{kind}}"}},
        ("k", "default"),
        "default contains a {{...}} placeholder",
    ),
    "string enum value with a placeholder": (
        {"k": {"enum": ["S", "{{ kind }}"]}},
        ("k", "enum", 1),
        "enum[1] contains a {{...}} placeholder",
    ),
}


class TestInvalidDeclarations:
    @pytest.mark.parametrize("label", list(INVALID))
    def test_each_broken_rule_is_named_at_its_path(self, label: str) -> None:
        params, path, fragment = INVALID[label]

        problems = param_declaration_problems(params)

        assert [p for p, _ in problems] == [path], problems
        assert fragment in problems[0][1]

    @pytest.mark.parametrize("label", list(INVALID))
    def test_the_model_drops_each_one_with_a_warning(
        self, label: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The profile still loads, so an agent that worked before ``params`` still launches."""
        params, _path, fragment = INVALID[label]

        with caplog.at_level(logging.WARNING, logger=_MODEL_LOGGER):
            profile = _profile(params)

        assert profile.params is None
        assert "agent profile 'orders-agent': its params block is ignored" in caplog.text
        assert fragment in caplog.text

    def test_problems_in_several_entries_are_all_reported_in_order(self) -> None:
        problems = param_declaration_problems(
            {
                "table": {"required": True, "default": "orders"},
                "ok": {"description": "fine"},
                "limit": {"type": "int", "default": "ten"},
            }
        )

        assert [path for path, _ in problems] == [("table", "default"), ("limit", "default")]

    def test_a_block_over_the_cap_is_reported_without_reading_its_entries(self) -> None:
        """Bounded work: 33 broken entries give the count error alone."""
        problems = param_declaration_problems({f"p{i}": "broken" for i in range(33)})

        assert problems == [((), "params declares 33 params (max 32)")]

    def test_a_long_unknown_key_is_quoted_short(self) -> None:
        problems = param_declaration_problems({"t": {"k" * 5000: 1}})

        assert len(problems[0][1]) < 300


class TestValidDeclarations:
    def test_the_contract_example_loads(self) -> None:
        profile = _profile(
            {
                "table_name": {"description": "DynamoDB table name", "required": True},
                "partition_key_type": {
                    "description": "DynamoDB key type",
                    "required": True,
                    "enum": ["S", "N", "B"],
                },
                "limit": {"description": "Max items to return", "type": "int", "default": 1},
            }
        )

        assert profile.params is not None
        assert list(profile.params) == ["table_name", "partition_key_type", "limit"]
        assert profile.params["limit"].default == 1
        assert profile.params["partition_key_type"].enum == ["S", "N", "B"]

    @pytest.mark.parametrize(
        "decl",
        [
            {},
            {"type": "string"},
            {"required": False, "default": ""},
            {"default": "o" * 512},
            {"description": "d" * 200},
            {"type": "int", "default": -999999999999999999},
            {"type": "int", "enum": [1, 2, 3], "default": 2},
            {"type": "bool", "default": False},
            {"type": "bool", "required": True},
            {"enum": ["S"]},
            {"enum": [f"v{i}" for i in range(32)]},
            {"enum": ["S", "s"]},
            {"default": '{"a": 1}'},
        ],
    )
    def test_declarations_within_the_rules_load(self, decl: dict) -> None:
        assert param_declaration_problems({"p": decl}) == []
        assert _profile({"p": decl}).params is not None

    def test_32_params_load(self) -> None:
        assert len(_profile({f"p{i}": {} for i in range(32)}).params or {}) == 32

    @pytest.mark.parametrize("name", ["a", "table.name", "aws-profile_1", "A" + "b" * 63])
    def test_names_follow_the_step_param_rule(self, name: str) -> None:
        assert param_declaration_problems({name: {}}) == []

    def test_no_params_is_none(self) -> None:
        assert AgentProfile(name="a", description="d").params is None
        assert _profile(None).params is None

    def test_a_declaration_built_on_its_own_is_checked_too(self) -> None:
        assert ProfileParamDecl(type="int", default=3).default == 3
        with pytest.raises(ValidationError, match="a bool param cannot have an enum"):
            ProfileParamDecl(type="bool", enum=[True])

    def test_a_built_declaration_passes_through_a_profile(self) -> None:
        decl = ProfileParamDecl(type="int", default=3)

        assert _profile({"limit": decl}).params == {"limit": decl}


class TestOneLine:
    """ "One line" means no line break of any kind, not only no "\\n" or "\\r"."""

    @pytest.mark.parametrize("line_break", OTHER_LINE_BREAKS.values(), ids=OTHER_LINE_BREAKS)
    def test_a_default_enum_value_or_description_with_one_is_refused(self, line_break: str) -> None:
        problems = param_declaration_problems(
            {
                "d": {"default": f"a{line_break}b"},
                "e": {"enum": [f"a{line_break}b"]},
                "t": {"description": f"a{line_break}b"},
            }
        )

        assert problems == [
            (("d", "default"), "param 'd': default must be one line (no line breaks)"),
            (("e", "enum", 0), "param 'e': enum[0] must be one line (no line breaks)"),
            (("t", "description"), "param 't': description must be one line (no line breaks)"),
        ]

    def test_a_tab_and_other_control_characters_are_not_line_breaks(self) -> None:
        assert param_declaration_problems({"d": {"default": "a\tb\x1fc\x07d"}}) == []


class TestAProfileThatUsedParamsBefore:
    """A profile whose ``params`` key predates the declaration block (review of #1291).

    On main the unknown key was dropped and the profile loaded. It must still load and
    launch, while ``cao profile validate`` and the write routes keep refusing the block.
    """

    TEXT = "---\nname: legacy\ndescription: old\nparams:\n  region: us-east-1\n---\nDo the work.\n"

    def test_it_loads_with_no_params_and_one_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=_MODEL_LOGGER):
            profile = parse_agent_profile_text(self.TEXT, "legacy")

        assert profile.name == "legacy"
        assert profile.system_prompt == "Do the work."
        assert profile.params is None
        warnings = [r for r in caplog.records if r.name == _MODEL_LOGGER]
        assert len(warnings) == 1
        assert warnings[0].levelno == logging.WARNING
        assert warnings[0].getMessage() == (
            "agent profile 'legacy': its params block is ignored because it breaks 1 rule(s), "
            "first: param 'region' must be a mapping, not a string. "
            "Run 'cao profile validate' to see them all."
        )

    def test_the_warning_never_echoes_a_value(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger=_MODEL_LOGGER):
            parse_agent_profile_text(self.TEXT, "legacy")

        assert "us-east-1" not in caplog.text

    def test_the_validator_still_refuses_it(self) -> None:
        findings = validate_frontmatter(
            {"name": "legacy", "description": "old", "params": {"region": "us-east-1"}}
        )

        assert [f.severity for f in findings if f.path and f.path.startswith("params")] == ["error"]


class TestWhatTheUiReads:
    """``GET /agents/profiles/{name}`` returns ``model_dump(exclude_none=True)``."""

    def test_type_and_required_always_appear_and_the_rest_only_when_set(self) -> None:
        dumped = _profile(
            {
                "table_name": {"description": "DynamoDB table name", "required": True},
                "region": {},
            }
        ).model_dump(exclude_none=True)

        assert dumped["params"] == {
            "table_name": {
                "type": "string",
                "required": True,
                "description": "DynamoDB table name",
            },
            "region": {"type": "string", "required": False},
        }

    def test_entries_keep_declaration_order_in_json(self) -> None:
        names = ["zeta", "alpha", "mid.name", "beta"]
        dumped = _profile({name: {} for name in names}).model_dump_json(exclude_none=True)

        assert list(json.loads(dumped)["params"]) == names

    def test_an_int_default_is_a_json_number_and_a_bool_default_a_json_bool(self) -> None:
        dumped = json.loads(
            _profile(
                {
                    "limit": {"type": "int", "default": 1, "enum": [1, 5]},
                    "dry_run": {"type": "bool", "default": False},
                }
            ).model_dump_json(exclude_none=True)
        )

        assert dumped["params"]["limit"] == {
            "type": "int",
            "required": False,
            "default": 1,
            "enum": [1, 5],
        }
        assert dumped["params"]["dry_run"] == {"type": "bool", "required": False, "default": False}

    def test_a_profile_without_params_has_no_params_key(self) -> None:
        assert "params" not in AgentProfile(name="a", description="d").model_dump(exclude_none=True)


_DEFAULT_MESSAGE = (
    "default contains a {{...}} placeholder. A default is copied into workflow steps, "
    "where values are fixed when the workflow is built."
)
_ENUM_MESSAGE = (
    "contains a {{...}} placeholder. An enum value is copied into the workflow steps "
    "that pick it, where values are fixed when the workflow is built."
)


class TestPlaceholders:
    """A string default or enum value has no ``{{...}}`` placeholder.

    The workflow builder copies a default, or the enum value a step picks, into the step's
    params, and the step grammar refuses a placeholder there. The rule uses the grammar's
    own bounded check, ``_has_placeholder``, so a value the profile accepts is one a step
    accepts.
    """

    @pytest.mark.parametrize(
        "value",
        ["{{workflow.inputs.table}}", "orders-{{ steps.read.output.count }}", "{{ }}", "{{{x}}}"],
    )
    def test_a_default_or_enum_value_with_one_is_refused(self, value: str) -> None:
        problems = param_declaration_problems(
            {"table": {"default": value}, "kind": {"enum": ["orders", value]}}
        )

        assert problems == [
            (("table", "default"), f"param 'table': {_DEFAULT_MESSAGE}"),
            (("kind", "enum", 1), f"param 'kind': enum[1] {_ENUM_MESSAGE}"),
        ]

    @pytest.mark.parametrize(
        "value", ['{"a": 1}', "{orders}", "{{}}", "{{a}", "{a}}", "}}{{", "{ {a} }", "{{a{b}}"]
    )
    def test_single_braces_and_unclosed_pairs_are_allowed(self, value: str) -> None:
        params = {"table": {"default": value}, "kind": {"enum": ["orders", value]}}

        assert param_declaration_problems(params) == []
        assert _profile(params).params is not None

    def test_a_declaration_built_on_its_own_is_refused_too(self) -> None:
        with pytest.raises(ValidationError, match="placeholder"):
            ProfileParamDecl(default="{{workflow.inputs.table}}")

    def test_a_value_of_the_wrong_type_gets_the_type_error_alone(self) -> None:
        problems = param_declaration_problems({"n": {"type": "int", "default": "{{x}}"}})

        assert problems == [
            (("n", "default"), "param 'n': default must be a whole number, not a string")
        ]

    def test_the_message_does_not_echo_the_value(self) -> None:
        problems = param_declaration_problems(
            {"t": {"default": "{{workflow.inputs.secret_table}}"}}
        )

        assert "secret_table" not in problems[0][1]

    def test_it_is_the_step_grammars_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The rule calls ``_has_placeholder``, never the engine's raw pattern."""
        from cli_agent_orchestrator.models import workflow

        seen: list = []

        def spy(value: str) -> bool:
            seen.append(value)
            return True

        monkeypatch.setattr(workflow, "_has_placeholder", spy)

        problems = param_declaration_problems({"t": {"enum": ["orders"], "default": "orders"}})

        assert seen == ["orders", "orders"]
        assert [path for path, _ in problems] == [("t", "enum", 0), ("t", "default")]

    def test_it_agrees_with_the_step_check_on_generated_values(self) -> None:
        rng = random.Random(1145)
        alphabet = ["{", "}", " ", "\t", "\u00a0", "\u3000", "a", ".", "-"]
        for _ in range(5_000):
            value = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 16)))
            refused = param_declaration_problems({"t": {"default": value}}) != []
            assert refused == _has_placeholder(value), repr(value)

    @pytest.mark.parametrize(
        "value",
        [
            "{{" + " " * 510,
            "{{" + " " * 505 + "{}}",
            ("{{" + " " * 60 + "{") * 7 + "}}",
        ],
    )
    def test_long_whitespace_runs_get_the_step_checks_answer(self, value: str) -> None:
        """The shapes where the engine's raw pattern backtracks most."""
        refused = param_declaration_problems({"t": {"default": value}}) != []

        assert refused == _has_placeholder(value)
