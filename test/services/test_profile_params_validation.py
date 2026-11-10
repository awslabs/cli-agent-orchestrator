"""The profile validator and the JSON-Schema hold ``params`` to the model's rules.

Contract section 1: the Pydantic model, ``schemas/agent_profile.schema.json`` and the
profile validator must agree. The validator runs the schema, then the rules the schema
does not state, taken from the model's own check: a default is one of the enum values, an
int value is not written with a decimal point, and a string default or enum value has no
``{{...}}`` placeholder. These tests pin the agreement case by case, against the same
cases the model tests use.

``validate_profile_text_with_params`` also returns the block as the model reads it, for
``POST /agents/profiles/validate``. ``TestTheBlockTheValidatorReads`` pins when it does.
"""

from __future__ import annotations

from test.models.test_agent_profile_params import INVALID

import pytest
from jsonschema import Draft202012Validator

from cli_agent_orchestrator.constants import (
    PROFILE_MAX_PARAMS,
    PROFILE_PARAM_DESCRIPTION_MAX_CHARS,
    PROFILE_PARAM_MAX_ENUM_VALUES,
    WORKFLOW_STEP_PARAM_KEY_RE,
    WORKFLOW_STEP_PARAM_VALUE_MAX_CHARS,
)
from cli_agent_orchestrator.models.agent_profile import AgentProfile, ProfileParamDecl
from cli_agent_orchestrator.services.profile_validator import (
    _OMISSION_MESSAGE,
    ValidationMessage,
    load_profile_schema,
    validate_frontmatter,
    validate_profile_text,
    validate_profile_text_with_params,
)
from cli_agent_orchestrator.utils.agent_profiles import parse_agent_profile_text

# The rules the schema does not state, so only the model's check reports them. The
# placeholder rule is left out of the schema on purpose: it uses the step grammar's bounded
# check, and a schema pattern would be a second, unbounded copy of the engine's regex.
_BEYOND_THE_SCHEMA = {
    "default not in the enum",
    "int default not in the enum",
    "int default with a decimal point",
    "string default with a placeholder",
    "placeholder default with an enum",
    "string enum value with a placeholder",
}


def _meta(params: object) -> dict:
    return {"name": "orders-agent", "description": "Reads orders", "params": params}


def _errors(params: object) -> list:
    return [f for f in validate_frontmatter(_meta(params)) if f.severity == "error"]


def _model_accepts(params: object) -> bool:
    try:
        AgentProfile(**_meta(params))
    except ValueError:
        return False
    return True


def _schema_errors(params: object) -> list:
    return list(Draft202012Validator(load_profile_schema()).iter_errors(_meta(params)))


VALID = {
    "contract example": {
        "table_name": {"description": "DynamoDB table name", "required": True},
        "partition_key_type": {
            "description": "DynamoDB key type",
            "required": True,
            "enum": ["S", "N", "B"],
        },
        "limit": {"description": "Max items to return", "type": "int", "default": 1},
    },
    "empty block": {},
    "bare entry": {"region": {}},
    "int enum with default": {"limit": {"type": "int", "enum": [1, 5, 10], "default": 5}},
    "bool default": {"dry_run": {"type": "bool", "default": False}},
    "optional string default": {"queue": {"default": "example-queue"}},
    "empty string default": {"sort_key": {"default": ""}},
    "dotted name": {"orders.table": {"enum": ["orders"], "default": "orders"}},
    "32 entries": {f"p{i}": {} for i in range(32)},
    "json-shaped default": {"payload": {"default": '{"a": 1}'}},
    "single braces in a default and an enum": {
        "payload": {"enum": ['{"a": 1}', "{orders}", "{{}}"], "default": "{orders}"}
    },
    "a placeholder in a description": {"table": {"description": "Fills {{table}} in the prompt"}},
}


class TestAgreement:
    """For every case, the validator reports an error exactly when the model refuses."""

    @pytest.mark.parametrize("label", list(INVALID))
    def test_the_validator_refuses_what_the_model_refuses(self, label: str) -> None:
        params, _path, _fragment = INVALID[label]

        assert not _model_accepts(params)
        assert _errors(params), f"{label}: the validator accepted what the model refuses"

    @pytest.mark.parametrize("label", list(VALID))
    def test_the_validator_accepts_what_the_model_accepts(self, label: str) -> None:
        assert _model_accepts(VALID[label])
        assert validate_frontmatter(_meta(VALID[label])) == []

    @pytest.mark.parametrize("label", sorted(set(INVALID) - _BEYOND_THE_SCHEMA))
    def test_the_schema_alone_refuses_every_other_rule(self, label: str) -> None:
        """A schema-driven client that validates locally sees the same rules."""
        params, _path, _fragment = INVALID[label]

        assert _schema_errors(params), f"{label}: the schema alone accepted it"

    @pytest.mark.parametrize("label", sorted(_BEYOND_THE_SCHEMA))
    def test_the_rules_beyond_the_schema_come_from_the_models_check(self, label: str) -> None:
        params, path, fragment = INVALID[label]

        assert _schema_errors(params) == []
        errors = _errors(params)
        assert [(e.path, fragment in e.message) for e in errors] == [
            (".".join(str(part) for part in ("params", *path)), True)
        ]

    @pytest.mark.parametrize("label", list(VALID))
    def test_the_schema_accepts_every_valid_case(self, label: str) -> None:
        assert _schema_errors(VALID[label]) == []


class TestFindings:
    def test_a_finding_carries_the_path_of_the_broken_key(self) -> None:
        errors = _errors({"limit": {"type": "int", "default": "ten"}})

        assert [e.path for e in errors] == ["params.limit.default"]

    @pytest.mark.parametrize(
        "decl,message",
        [
            ({"required": True, "default": "orders"}, "A required param cannot have a default."),
            ({"type": "bool", "enum": [True]}, "A bool param cannot have an enum."),
            (
                {"description": "line one\nline two"},
                "A param description is one line of at most 200 characters.",
            ),
            ({"default": "a\nb"}, "A string value is one line of at most 512 characters."),
        ],
    )
    def test_a_failed_not_reads_as_the_rule_it_checks(self, decl: dict, message: str) -> None:
        """jsonschema's own wording for ``not`` names neither the rule nor the fix."""
        errors = _errors({"table": decl})

        assert [e.message for e in errors] == [message]
        assert not any("should not be valid under" in e.message for e in errors)

    def test_a_mistake_is_reported_once(self) -> None:
        """The model's check skips a declaration the schema already reported."""
        errors = _errors({"limit": {"type": "int", "default": "ten"}, "ok": {}})

        assert len(errors) == 1

    def test_a_schema_error_in_one_entry_does_not_hide_a_rule_in_another(self) -> None:
        errors = _errors(
            {
                "limit": {"type": "int", "default": "ten"},
                "key_type": {"enum": ["S", "N"], "default": "B"},
            }
        )

        assert [e.path for e in errors] == ["params.limit.default", "params.key_type.default"]

    def test_a_dotted_name_is_matched_to_its_own_schema_error(self) -> None:
        """A param name may contain a dot, so paths are compared as parts, not as text."""
        errors = _errors({"a.b": {"type": "int", "default": "ten"}, "a": {"enum": ["S"]}})

        assert [e.path for e in errors] == ["params.a.b.default"]

    def test_a_long_enum_skips_the_uniqueness_check(self) -> None:
        """uniqueItems runs only within maxItems, so its pairwise fallback stays bounded."""
        errors = _errors({"k": {"enum": ["S"] * (PROFILE_PARAM_MAX_ENUM_VALUES + 1)}})

        assert [e.path for e in errors] == ["params.k.enum"]
        assert "too long" in errors[0].message

    def test_a_large_mixed_type_enum_is_answered(self) -> None:
        """Ints and strings cannot be sorted, which is jsonschema's quadratic case."""
        enum = [value for i in range(20_000) for value in (i, f"s{i}")]

        errors = _errors({"k": {"enum": enum}})

        assert any(e.path == "params.k.enum" for e in errors)

    def test_profile_text_with_params_validates(self) -> None:
        text = (
            "---\n"
            "name: orders-agent\n"
            "description: Reads orders\n"
            "params:\n"
            "  table_name:\n"
            "    description: DynamoDB table name\n"
            "    required: true\n"
            "  limit:\n"
            "    type: int\n"
            "    default: 1\n"
            "---\n\nRead the orders table.\n"
        )

        assert validate_profile_text(text) == []


class TestSchemaShape:
    """The schema's own numbers are the model's constants."""

    def test_the_schema_states_the_constants(self) -> None:
        params = load_profile_schema()["properties"]["params"]
        entry = params["additionalProperties"]

        assert params["maxProperties"] == PROFILE_MAX_PARAMS
        assert params["propertyNames"]["pattern"] == WORKFLOW_STEP_PARAM_KEY_RE
        assert entry["additionalProperties"] is False
        assert set(entry["properties"]) == {"type", "required", "default", "description", "enum"}
        assert entry["properties"]["type"]["enum"] == ["string", "int", "bool"]
        assert entry["properties"]["description"]["maxLength"] == (
            PROFILE_PARAM_DESCRIPTION_MAX_CHARS
        )
        assert entry["properties"]["enum"]["maxItems"] == PROFILE_PARAM_MAX_ENUM_VALUES
        string_branch = entry["allOf"][0]["then"]["properties"]
        assert string_branch["default"]["maxLength"] == WORKFLOW_STEP_PARAM_VALUE_MAX_CHARS
        assert string_branch["enum"]["items"]["maxLength"] == WORKFLOW_STEP_PARAM_VALUE_MAX_CHARS

    def test_no_author_regex_or_bound_is_accepted(self) -> None:
        entry = load_profile_schema()["properties"]["params"]["additionalProperties"]

        for key in ("pattern", "minimum", "maximum", "maxLength"):
            assert key not in entry["properties"]

    def test_uniqueness_is_checked_only_under_the_length_bound(self) -> None:
        entry = load_profile_schema()["properties"]["params"]["additionalProperties"]
        guarded = [
            branch for branch in entry["allOf"] if "uniqueItems" in str(branch.get("then", {}))
        ]

        assert "uniqueItems" not in entry["properties"]["enum"]
        assert len(guarded) == 1
        assert guarded[0]["if"]["properties"]["enum"]["maxItems"] == PROFILE_PARAM_MAX_ENUM_VALUES


class TestPlaceholderFindings:
    """The placeholder rule as ``cao profile validate`` and the validate route report it."""

    def test_each_value_is_named_at_its_path(self) -> None:
        text = (
            "---\n"
            "name: orders-agent\n"
            "description: Reads orders\n"
            "params:\n"
            "  table_name:\n"
            '    default: "orders-{{run_id}}"\n'
            "  key_type:\n"
            '    enum: [S, "{{kind}}"]\n'
            "---\n\nRead the orders table.\n"
        )

        findings = validate_profile_text(text)

        assert [(f.severity, f.path, f.message) for f in findings] == [
            (
                "error",
                "params.table_name.default",
                "param 'table_name': default contains a {{...}} placeholder. A default is "
                "copied into workflow steps, where values are fixed when the workflow is built.",
            ),
            (
                "error",
                "params.key_type.enum.1",
                "param 'key_type': enum[1] contains a {{...}} placeholder. An enum value is "
                "copied into the workflow steps that pick it, where values are fixed when the "
                "workflow is built.",
            ),
        ]

    def test_single_braces_pass(self) -> None:
        text = (
            "---\n"
            "name: orders-agent\n"
            "description: Reads orders\n"
            "params:\n"
            "  payload:\n"
            "    default: '{\"a\": 1}'\n"
            "    enum: ['{\"a\": 1}', '{orders}']\n"
            "---\n\nRead the orders table.\n"
        )

        assert validate_profile_text(text) == []


_BLOCK = (
    "params:\n"
    "  table_name:\n"
    "    description: DynamoDB table name\n"
    "    required: true\n"
    "  limit:\n"
    "    default: 1\n"
    "    type: int\n"
)


def _document(*lines: str, block: str = _BLOCK) -> str:
    """A profile with ``lines`` before its ``params`` block."""
    head = "".join(f"{line}\n" for line in lines)
    return (
        f"---\nname: orders-agent\ndescription: Reads orders\n{head}{block}"
        "---\n\nRead the orders table.\n"
    )


def _bomb(levels: int = 40) -> str:
    """Frontmatter lines whose aliases render far past the structural ceiling."""
    lines = ["hooks:", "  a0: &a0 {k: v}"]
    for level in range(1, levels + 1):
        lines.append(f"  a{level}: &a{level} {{x: *a{level - 1}, y: *a{level - 1}}}")
    return "\n".join(lines)


def _dumped(params: dict) -> dict:
    return {name: decl.model_dump(exclude_none=True) for name, decl in params.items()}


class TestTheBlockTheValidatorReads:
    """``validate_profile_text_with_params`` returns the block only when it passed.

    Profile params editor contract, section 1a: an object, possibly empty, only when the
    frontmatter has a ``params`` key, no error finding is at ``params`` or below it, and
    no error stopped the checks before they finished the block. Otherwise None.
    """

    def test_a_valid_block_is_read_as_the_loader_reads_it(self) -> None:
        text = _document()

        result = validate_profile_text_with_params(text)

        assert result.findings == []
        assert result.params is not None
        assert all(isinstance(decl, ProfileParamDecl) for decl in result.params.values())
        loaded = parse_agent_profile_text(text, "orders-agent").params or {}
        assert (
            _dumped(result.params)
            == _dumped(loaded)
            == {
                "table_name": {
                    "type": "string",
                    "required": True,
                    "description": "DynamoDB table name",
                },
                "limit": {"type": "int", "required": False, "default": 1},
            }
        )
        assert list(result.params) == ["table_name", "limit"]

    def test_an_empty_block_is_an_empty_mapping(self) -> None:
        result = validate_profile_text_with_params(_document(block="params: {}\n"))

        assert result.findings == []
        assert result.params == {}

    def test_no_params_key_is_none(self) -> None:
        result = validate_profile_text_with_params(_document(block=""))

        assert result.findings == []
        assert result.params is None

    @pytest.mark.parametrize(
        "block,path",
        [
            ("params:\n", "params"),
            ("params: [table_name]\n", "params"),
            ("params:\n  1table: {}\n", "params"),
            ("params:\n  t:\n    required: true\n    default: orders\n", "params.t"),
            ("params:\n  t:\n    type: int\n    default: ten\n", "params.t.default"),
            ("params:\n  k:\n    enum: [S, N]\n    default: B\n", "params.k.default"),
            ('params:\n  t:\n    default: "{{run_id}}"\n', "params.t.default"),
            ('params:\n  k:\n    enum: [S, "{{kind}}"]\n', "params.k.enum.1"),
        ],
        ids=[
            "null",
            "a list",
            "bad name",
            "required with a default",
            "a schema error",
            "a rule beyond the schema",
            "a placeholder default",
            "a placeholder enum value",
        ],
    )
    def test_a_block_with_an_error_is_none(self, block: str, path: str) -> None:
        result = validate_profile_text_with_params(_document(block=block))

        assert result.params is None
        assert any(f.severity == "error" and f.path == path for f in result.findings)

    @pytest.mark.parametrize(
        "line,path",
        [
            ("allowedTools: [1]", "allowedTools.0"),
            ("engine: v4", "engine"),
            ("unknown_key: 1", "(root)"),
            ("mcpServers: {1: {command: echo}}", "mcpServers.1"),
        ],
        ids=["allowedTools", "engine", "an unknown key", "a non-string key"],
    )
    def test_an_error_outside_the_block_keeps_it(self, line: str, path: str) -> None:
        result = validate_profile_text_with_params(_document(line))

        assert any(f.severity == "error" and f.path == path for f in result.findings)
        assert result.params is not None
        assert list(result.params) == ["table_name", "limit"]

    def test_a_missing_name_keeps_it(self) -> None:
        """A finding at the document root is not an error that stops the checks."""
        text = _document().replace("name: orders-agent\n", "", 1)

        result = validate_profile_text_with_params(text)

        assert [(f.path, f.message) for f in result.findings] == [
            ("(root)", "'name' is a required property")
        ]
        assert result.params is not None

    @pytest.mark.parametrize(
        "lines,block",
        [
            ((_bomb(),), _BLOCK),
            (("toolsSettings: &c {self: *c}",), _BLOCK),
            ((_bomb(),), "params:\n  t:\n    enum: *a40\n"),
        ],
        ids=["renders too large beside the block", "a cycle", "the block is the alias"],
    )
    def test_a_document_past_the_structural_ceilings_is_none(
        self, lines: tuple, block: str
    ) -> None:
        result = validate_profile_text_with_params(_document(*lines, block=block))

        assert result.params is None
        assert [f.severity for f in result.findings] == ["error"]

    def test_a_findings_budget_that_fills_before_the_block_is_checked_is_none(self) -> None:
        # Nested, because python-frontmatter refuses a non-string key at the root.
        keys = "hooks:\n" + "\n".join(f"  {index}: value" for index in range(200))

        result = validate_profile_text_with_params(_document(keys))

        assert result.findings[-1] == ValidationMessage("error", _OMISSION_MESSAGE)
        assert result.params is None

    def test_warnings_that_fill_the_budget_after_the_block_keep_it(self) -> None:
        """Only allowedTools warnings run after the block, so none of them is about it."""
        tools = ", ".join(f"unknown-{index}" for index in range(200))

        result = validate_profile_text_with_params(_document(f"allowedTools: [{tools}]"))

        assert result.findings[-1] == ValidationMessage("warning", _OMISSION_MESSAGE)
        assert not any(f.severity == "error" for f in result.findings)
        assert result.params is not None
        assert list(result.params) == ["table_name", "limit"]

    def test_a_dollar_placeholder_is_read_as_written(self) -> None:
        block = 'params:\n  table_name:\n    description: "Fills ${TABLE_NAME}"\n'

        result = validate_profile_text_with_params(_document(block=block))

        assert result.params is not None
        assert result.params["table_name"].description == "Fills ${TABLE_NAME}"

    @pytest.mark.parametrize(
        "text",
        [
            _document(),
            _document("allowedTools: [1]"),
            _document("role: not-a-real-role"),
            _document(block='params:\n  t:\n    default: "{{run_id}}"\n'),
            _document(_bomb()),
        ],
        ids=["valid", "a schema error", "a warning", "a params error", "a structural error"],
    )
    def test_the_findings_are_the_ones_validate_profile_text_returns(self, text: str) -> None:
        assert validate_profile_text_with_params(text).findings == validate_profile_text(text)

    def test_unparseable_frontmatter_raises_value_error(self) -> None:
        """The validate route maps this to 400, as it did before the block was returned."""
        text = "---\nname: [unclosed\n  bad: : yaml\n---\n\nBody.\n"

        with pytest.raises(ValueError, match="Error reading profile"):
            validate_profile_text_with_params(text)
