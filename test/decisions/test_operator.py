import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

ROOT = Path(__file__).resolve().parents[2]


def test_cli_controls(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CAO_HOME_DIR", str(tmp_path / "cao"))
    from cli_agent_orchestrator.services import settings_service

    monkeypatch.setattr(settings_service, "CAO_HOME_DIR", tmp_path)
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", tmp_path / "settings.json")
    from cli_agent_orchestrator.cli.commands.decisions import decisions
    from cli_agent_orchestrator.decisions import operator

    monkeypatch.setattr(operator, "records", lambda **kwargs: [])
    monkeypatch.setattr(operator, "purge", lambda **kwargs: 3)
    runner = CliRunner()
    commands = [
        ["set", "model.route", "shadow", "--decider", "fixed_table"],
        ["tier", "codex", "small", "model-x"],
        ["tier", "codex", "small", "--unset"],
        ["table", "model.route", "--profile", "worker", "small"],
        ["exclude", "--add", "worker"],
        ["exclude", "--remove", "worker"],
        ["tune", "--on-timeout-ms", "60"],
        ["status"],
        ["list"],
        ["purge", "--all", "--rotate-key"],
        ["purge", "--before", "2026-01-01"],
    ]
    for args in commands:
        result = runner.invoke(decisions, args)
        assert result.exit_code == 0, (args, result.output, result.exception)
    for args in (
        ["tier", "invalid", "small", "x"],
        ["purge"],
        ["purge", "--all", "--before", "2026-01-01"],
        ["tune"],
        ["tier", "codex", "small", "x", "--unset"],
    ):
        assert runner.invoke(decisions, args).exit_code != 0


def test_boundary_with_installed_plugin(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from cli_agent_orchestrator.decisions.targets import TargetKind, profile_source

    assert profile_source("reserved-looking-name") == TargetKind.INSTALLED
    assert profile_source(None) == TargetKind.INSTALLED
    assert profile_source("Reviewer-audit_logs-3f9a") == TargetKind.EPHEMERAL
    distribution = tmp_path / "boundary_fixture-1.0.dist-info"
    distribution.mkdir()
    (distribution / "METADATA").write_text("Name: boundary_fixture\nVersion: 1.0\n")
    (distribution / "entry_points.txt").write_text(
        "[cao.plugins]\nboundary_fixture = boundary_fixture:FixturePlugin\n"
    )
    (tmp_path / "boundary_fixture.py").write_text(
        'from cli_agent_orchestrator.plugins.base import CaoPlugin\nclass FixturePlugin(CaoPlugin):\n    name = "boundary_fixture"\n    def on_mcp_server(self, mcp):\n        @mcp.tool\n        def fixture_ping() -> str:\n            return "ok"\n'
    )
    code = """import asyncio, json, sys
from pathlib import Path
from cli_agent_orchestrator.mcp_server.server import mcp
from test.fixtures.decision_boundary import assert_agent_boundary
assert not any(name.startswith('cli_agent_orchestrator.decisions') for name in sys.modules)
async def inspect():
    tools = await mcp.list_tools()
    assert any(t.name == 'fixture_ping' for t in tools)
    base = json.loads(Path('test/fixtures/decision_boundary_base.json').read_text())
    definitions = [{'name': t.name, 'description': t.description,
                    'input': t.parameters, 'output': t.output_schema} for t in tools]
    for tool in definitions:
        if tool['name'] != 'workflow_resume':
            continue
        schema = tool['input']['properties']['decisions']
        # Before 3.11, get_type_hints wraps a None default in Optional.
        if (set(schema) == {'anyOf', 'default'}
                and isinstance(schema['anyOf'], list)
                and len(schema['anyOf']) == 2
                and isinstance(schema['anyOf'][0], dict)
                and set(schema['anyOf'][0]) == {'anyOf', 'description'}
                and isinstance(schema['anyOf'][0]['anyOf'], list)
                and schema['anyOf'][1] == {'type': 'null'}):
            hoisted = {**schema['anyOf'][0], 'default': schema['default']}
            if hoisted == base['workflow_resume_input_decisions']:
                tool['input']['properties']['decisions'] = hoisted
    assert_agent_boundary(definitions, base)
asyncio.run(inspect())
"""
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "CAO_HOME_DIR": str(tmp_path / "cao"),
        "PYTHONPATH": str(tmp_path) + os.pathsep + str(ROOT / "src") + os.pathsep + str(ROOT),
    }
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, cwd=ROOT
    )
    assert result.returncode == 0, result.stderr
    paths = (
        list((ROOT / "src/cli_agent_orchestrator/mcp_server").glob("*.py"))
        + [ROOT / "src/cli_agent_orchestrator/utils/orchestration.py"]
        + list((ROOT / "src/cli_agent_orchestrator/plugins/builtin").glob("*.py"))
    )
    for path in paths:
        from test.fixtures.decision_boundary import assert_no_decision_imports

        package = ".".join(path.relative_to(ROOT / "src").parts[:-1])
        assert_no_decision_imports(path.read_text(), package)
    from cli_agent_orchestrator.api.main import app

    assert not any("decision" in getattr(route, "path", "") for route in app.routes)


def test_source_tokens_are_neutral():
    denied = {
        "9e66a118b9a0fb8cada5eb0f357806a21cc067fa4b9d9f76eb9773a24e022438",
        "bc74d4c225d5b7d9149b370ab12c1f4aba41322dcb527d7ea1fa941028f176e8",
    }
    # Compare hashed tokens so private integration labels are never published here.
    texts = [
        p.read_bytes().decode("utf-8", errors="ignore")
        for p in (ROOT / "src").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    ]
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib

    texts.append(
        json.dumps(
            tomllib.loads((ROOT / "pyproject.toml").read_text())["project"].get("entry-points", {})
        )
    )
    assert not any(
        hashlib.sha256(token.lower().encode()).hexdigest() in denied
        for text in texts
        for token in re.findall(r"[A-Za-z0-9_]+", text)
    )


def test_server_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CAO_HOME_DIR", str(tmp_path / "cao"))
    from cli_agent_orchestrator.api import main

    monkeypatch.setenv("CAO_DECISION_MODEL_ROUTE", "off")
    monkeypatch.setattr(sys, "argv", ["cao-server", "--decision", "model.route=shadow"])
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    main.main()
    assert os.environ["CAO_DECISION_MODEL_ROUTE"] == "shadow"


@pytest.mark.parametrize(
    "mutation",
    [
        "input_capture",
        "resume_description",
        "memory_description",
        "other_name",
        "other_description",
        "other_input",
        "other_output",
    ],
)
def test_boundary_scan_rejects_changes_and_new_vocabulary(mutation):
    import copy
    from test.fixtures.decision_boundary import assert_agent_boundary

    base = json.loads((ROOT / "test/fixtures/decision_boundary_base.json").read_text())
    tools = [
        {
            "name": "workflow_resume",
            "description": base["workflow_resume_description"],
            "input": {
                "properties": {"decisions": copy.deepcopy(base["workflow_resume_input_decisions"])}
            },
            "output": {},
        },
        {
            "name": "memory_store",
            "description": base["memory_store_description"],
            "input": {},
            "output": {},
        },
        {"name": "fixture_ping", "description": "Ping", "input": {}, "output": {}},
    ]
    assert_agent_boundary(tools, base)
    if mutation == "input_capture":
        tools[0]["input"]["properties"]["decisions"]["description"] += " changed"
    elif mutation == "resume_description":
        tools[0]["description"] += " changed"
    elif mutation == "memory_description":
        tools[1]["description"] += " changed"
    elif mutation == "other_name":
        tools[2]["name"] = "read_decisions"
    elif mutation == "other_description":
        tools[2]["description"] = "Read model_tiers"
    elif mutation == "other_input":
        tools[2]["input"] = {"properties": {"exclude_profiles": {"type": "array"}}}
    else:
        tools[2]["output"] = {"properties": {"points": {"type": "array"}}}
    with pytest.raises(AssertionError):
        assert_agent_boundary(tools, base)


@pytest.mark.asyncio
async def test_ops_controls_and_local_record_queries(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from cli_agent_orchestrator.clients.database import DecisionRecordModel
    from cli_agent_orchestrator.decisions import operator
    from cli_agent_orchestrator.decisions.store import DecisionStore
    from cli_agent_orchestrator.ops_mcp_server.decision_tools import register_decision_tools
    from cli_agent_orchestrator.services import settings_service

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CAO_HOME_DIR", str(tmp_path / "cao"))
    monkeypatch.setattr(settings_service, "CAO_HOME_DIR", tmp_path)
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", tmp_path / "settings.json")
    db = create_engine("sqlite:///" + str(tmp_path / "state.db"))
    DecisionRecordModel.__table__.create(db)
    store = DecisionStore(
        sessionmaker(bind=db), tmp_path / "decision-hash.key", emit=lambda row: None
    )
    monkeypatch.setattr(operator, "init_db", lambda: None)
    monkeypatch.setattr(operator, "DecisionStore", lambda: store)

    class Surface:
        def __init__(self):
            self.tools = {}

        def tool(self, fn):
            self.tools[fn.__name__] = fn
            return fn

    surface = Surface()
    register_decision_tools(surface)
    tools = surface.tools
    assert tools["decisions_set_point"]("model.route", "on")["success"]
    assert tools["decisions_set_tier"]("codex", "small", "model-x")["success"]
    assert tools["decisions_set_exclusions"](add="worker")["success"]
    assert tools["decisions_status"]()["data"]["points"]["model.route"]["state"] == "on"
    assert not tools["decisions_set_point"]("model.route", "invalid")["success"]
    assert not tools["decisions_purge"]()["success"]
    row = dict(
        point="model.route",
        state="on",
        kind="assign",
        provider="codex",
        fallback_source="none",
        outcome="fallback",
    )
    store.insert(row, "message")
    assert (
        len(tools["decisions_list"](point="model.route", since="2026-01-01", limit=10)["data"]) == 1
    )
    assert tools["decisions_purge"](before="2020-01-01")["data"] == 0
    assert tools["decisions_purge"](all_records=True, rotate_key=True)["data"] == 1
    from cli_agent_orchestrator.ops_mcp_server.server import mcp

    assert set(tools) <= {tool.name for tool in await mcp.list_tools()}


@pytest.mark.parametrize(
    "source",
    [
        "import cli_agent_orchestrator.decisions.store",
        "from cli_agent_orchestrator import decisions",
        "from ..decisions.engine import X",
        "from .. import decisions",
        "importlib.import_module('cli_agent_orchestrator.decisions.engine')",
        "import_module('cli_agent_orchestrator.decisions')",
        "__import__('cli_agent_orchestrator.decisions.settings')",
        "importlib.import_module('..decisions.engine', __package__)",
        "importlib.import_module('.decisions', package='cli_agent_orchestrator')",
        "importlib.import_module(name='cli_agent_orchestrator.decisions')",
        "__import__('cli_agent_orchestrator', fromlist=['decisions'])",
        "__import__('cli_agent_orchestrator', None, None, ('decisions',))",
    ],
)
def test_ast_guard_catches_all_decision_import_forms(source):
    from test.fixtures.decision_boundary import assert_no_decision_imports

    with pytest.raises(AssertionError, match="decision import"):
        assert_no_decision_imports(
            "def lazy():\n    " + source + "\n", "cli_agent_orchestrator.mcp_server"
        )


@pytest.mark.parametrize(
    "source",
    [
        "importlib.import_module('cli_agent_orchestrator.services.terminal_service')",
        "importlib.import_module(name)",
        "__import__('json')",
        "registry.import_module",
        "importlib.import_module('..services.terminal_service', __package__)",
        "__import__('cli_agent_orchestrator', fromlist=['services'])",
    ],
)
def test_ast_guard_allows_other_dynamic_imports(source):
    from test.fixtures.decision_boundary import assert_no_decision_imports

    assert_no_decision_imports(
        "def lazy():\n    " + source + "\n", "cli_agent_orchestrator.mcp_server"
    )


def test_decision_catalog_help_and_group_order():
    import re

    from cli_agent_orchestrator.cli.commands.decisions import decisions

    text = (ROOT / "tui/src/catalog.rs").read_text()
    rows = re.findall(r"CommandId::Decisions\w+ => Command \{(.*?)\n        \},", text, re.S)
    assert len(rows) == 8
    for row in rows:
        leaf = re.search(r'leaf_name: "([^"]+)"', row).group(1)
        summary = re.search(r'summary: "([^"]+)"', row).group(1)
        assert summary == decisions.commands[leaf].help.splitlines()[0]
    display = text.split("pub(crate) const DISPLAY_ORDER:", 1)[1].split("];", 1)[0]
    names = re.findall(r"CommandId::(\w+)", display)
    expected = [
        "DecisionsExclude",
        "DecisionsList",
        "DecisionsPurge",
        "DecisionsSet",
        "DecisionsStatus",
        "DecisionsTable",
        "DecisionsTier",
        "DecisionsTune",
    ]
    assert [name for name in names if name.startswith("Decisions")] == expected
    assert (
        names.index("ConfigSet")
        < names.index("DecisionsExclude")
        < names.index("DecisionsTune")
        < names.index("EnvGet")
    )
