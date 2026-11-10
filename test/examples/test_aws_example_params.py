"""The AWS examples declare their params from their template schemas (contract section 4).

Each ``examples/aws/<name>/<name>-agent.md`` declares ``params`` taken from
``templates/aws/<name>/schema.json``: the property keys (also the ``config.json`` keys),
``required`` from the schema's list, the description, ``integer`` as ``int``, the enum
without ``""``, and the default unless it is ``""``. ``pattern``, ``minimum``,
``maximum`` and ``maxLength`` are dropped. The body names the ``${VAR}`` placeholder
each param fills.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import frontmatter
import pytest

from cli_agent_orchestrator.services.profile_validator import validate_profile_text
from cli_agent_orchestrator.utils.agent_profiles import parse_agent_profile_text

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "aws"
TEMPLATES = ROOT / "src" / "cli_agent_orchestrator" / "templates" / "aws"
NAMES = sorted(path.name for path in TEMPLATES.iterdir() if (path / "schema.json").is_file())

# The two config keys whose placeholder is not the key in capitals.
_PLACEHOLDER = {"profile": "AWS_PROFILE", "region": "AWS_REGION"}


def _text(name: str) -> str:
    return (EXAMPLES / name / f"{name}-agent.md").read_text(encoding="utf-8")


def _schema(name: str) -> dict:
    return json.loads((TEMPLATES / name / "schema.json").read_text(encoding="utf-8"))


def _expected(name: str) -> dict:
    """The declaration the contract's mapping rules give for one template schema."""
    schema = _schema(name)
    required = set(schema.get("required", []))
    expected = {}
    for key, prop in schema["properties"].items():
        decl = {
            "type": {"string": "string", "integer": "int"}[prop["type"]],
            "required": key in required,
            "description": prop["description"],
        }
        enum = [value for value in prop.get("enum", []) if value != ""]
        if enum:
            decl["enum"] = enum
        if prop.get("default", "") != "":
            decl["default"] = prop["default"]
        expected[key] = decl
    return expected


def test_there_are_seven_examples() -> None:
    assert NAMES == [
        "cloudwatch-logs",
        "dynamodb-delete",
        "dynamodb-query",
        "sqs-dlq-check",
        "sqs-monitor",
        "sqs-send",
        "stepfunction",
    ]


@pytest.mark.parametrize("name", NAMES)
def test_the_example_parses_and_declares_the_schemas_params(name: str) -> None:
    profile = parse_agent_profile_text(_text(name), f"{name}-agent")

    assert profile.params is not None
    declared = {key: decl.model_dump(exclude_none=True) for key, decl in profile.params.items()}
    assert declared == _expected(name)
    assert list(declared) == list(_schema(name)["properties"]), "schema order is kept"


@pytest.mark.parametrize("name", NAMES)
def test_param_names_are_the_config_json_keys(name: str) -> None:
    config = json.loads((EXAMPLES / name / "config.json").read_text(encoding="utf-8"))

    assert set(parse_agent_profile_text(_text(name), name).params or {}) == set(config)


@pytest.mark.parametrize("name", NAMES)
def test_the_example_passes_the_profile_validator(name: str) -> None:
    assert validate_profile_text(_text(name)) == []


@pytest.mark.parametrize("name", NAMES)
def test_the_params_block_has_no_placeholder(name: str) -> None:
    """``load_agent_profile`` resolves ``${VAR}`` before parsing, block included."""
    meta = frontmatter.loads(_text(name)).metadata

    assert "${" not in json.dumps(meta["params"])


@pytest.mark.parametrize("name", NAMES)
def test_configuration_names_the_placeholder_each_param_fills(name: str) -> None:
    text = _text(name)
    configuration = text.split("## Configuration", 1)[1].split("\n## ", 1)[0]

    for key in _schema(name)["properties"]:
        placeholder = "${" + _PLACEHOLDER.get(key, key.upper()) + "}"
        assert f"- `{key}` fills `{placeholder}`" in configuration
        body = frontmatter.loads(text).content
        assert body.count(placeholder) >= 2, f"{placeholder} is not used past Configuration"


@pytest.mark.parametrize("name", NAMES)
def test_instructions_say_the_step_values_replace_the_placeholders(name: str) -> None:
    instructions = _text(name).split("## Instructions", 1)[1]
    paragraph = re.sub(r"\s+", " ", instructions.split("```", 1)[0])

    assert '"Parameters for this step"' in paragraph
    assert "in place of the `${...}` placeholder" in paragraph
    assert "Outside a workflow, the placeholders work as before." in paragraph
