"""#1199: `cao decisions match show`, `check` and `explain`, read-only views of decisions.match."""

import asyncio
import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.cli.commands.decisions import decisions
from cli_agent_orchestrator.decisions.types import FEATURE_POINTS, DecisionAnswer
from cli_agent_orchestrator.services import settings_service

TIERS = {
    "claude_code": {
        "small": "claude-haiku-5-5",
        "medium": "claude-sonnet-5-5",
        "large": "claude-opus-5-5",
    },
    "codex": {"small": "gpt-5.5-mini", "medium": "gpt-5.5", "large": "gpt-5.5-pro"},
}
CATALOG = {
    "version": 1,
    "long_context_tokens": 500000,
    "models": {
        "claude_code": {
            "claude-sonnet-5-5": {"context_tokens": 200000, "cost": "medium"},
            "claude-opus-5-5": {
                "context_tokens": 1000000,
                "cost": "high",
                "good_for": ["deep_reasoning"],
            },
        },
        "codex": {
            "gpt-5.5": {"context_tokens": 256000, "cost": "low", "good_for": ["read_only"]},
            "gpt-5.5-pro": {"context_tokens": 2000000, "cost": "medium", "good_for": ["read_only"]},
        },
    },
}
EPHEMERAL = {"min_tier": "small", "default_tier": "medium", "max_tier": "large"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    from cli_agent_orchestrator import constants

    monkeypatch.setattr(constants, "DB_DIR", tmp_path / "db")
    (tmp_path / "db").mkdir()
    settings_service._save(
        {"model_tiers": TIERS, "ephemeral": EPHEMERAL, "decisions": {"match": CATALOG}}
    )
    return tmp_path


def run(*args):
    return CliRunner().invoke(decisions, ["match", *args])


def test_show_prints_the_catalog_and_says_when_there_is_no_key_yet(home):
    result = run("show")
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == (
        "catalog v1 · 4 models · long_context_tokens 500000 · ignore_features: none"
    )
    assert lines[1].startswith("hash unavailable: no records key yet")
    assert "claude_code  claude-opus-5-5    1000000  high    deep_reasoning" in result.output
    assert not (home / "db" / "decision-hash.key").exists()  # never created by show


def test_show_json_carries_the_records_hash_once_the_key_exists(home):
    from cli_agent_orchestrator.decisions.catalog import load_catalog
    from cli_agent_orchestrator.decisions.hashing import KeyCache

    expected = KeyCache(home / "db" / "decision-hash.key").digest(
        load_catalog({"decisions": {"match": CATALOG}}).canonical()
    )
    result = run("show", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert (data["catalog_hash"], data["hash_key_id"]) == expected
    assert data["valid"] and data["version"] == 1 and data["warnings"] == []
    assert data["models"]["codex"]["gpt-5.5"] == {
        "context_tokens": 256000,
        "cost": "low",
        "good_for": ["read_only"],
    }
    assert "hash hmac:" + expected[0] in run("show").output


def test_show_names_an_invalid_catalog_and_its_warnings(home):
    settings_service._save({"model_tiers": TIERS, "decisions": {"match": {"version": 2}}})
    result = run("show")
    assert result.exit_code == 0
    assert result.output.strip() == (
        "catalog invalid: decisions.match.version must be 1. Matching is off for every launch; "
        "launches run as today."
    )
    bad = json.loads(json.dumps(CATALOG))
    bad["models"]["codex"]["gpt-5.5"]["cost"] = "cheap"
    settings_service._save({"model_tiers": TIERS, "decisions": {"match": bad}})
    data = json.loads(run("show", "--json").output)
    assert data["warnings"] == [
        "decisions.match.models.codex.gpt-5.5.cost must be low, medium or high; declaration dropped"
    ]


def test_AC8_check_reports_an_unreachable_declaration_and_exits_1(home):
    catalog = json.loads(json.dumps(CATALOG))
    catalog["models"]["codex"]["gpt-old"] = {"context_tokens": 1, "cost": "low"}
    settings_service._save({"model_tiers": TIERS, "decisions": {"match": catalog}})
    result = run("check")
    assert result.exit_code == 1, result.output
    assert (
        "WARN  codex/gpt-old is unreachable: no model_tiers.codex tier maps it, so it is never used"
        in result.output.splitlines()
    )


def test_check_passes_a_clean_catalog_with_notes(home):
    result = run("check")
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert (
        "note  claude_code/small maps claude-haiku-5-5, which has no declaration, so it covers nothing"
        in lines
    )
    assert (
        "note  long_context (500000): covered by claude_code/claude-opus-5-5, codex/gpt-5.5-pro"
        in lines
    )


def test_check_fails_an_invalid_catalog(home):
    settings_service._save({"decisions": {"match": {"version": 1, "notes": "x"}}})
    result = run("check")
    assert result.exit_code == 1
    assert (
        result.output.strip() == "error catalog invalid: decisions.match.notes is not a known key"
    )


def test_show_and_check_print_each_catalog_problem_once(home, caplog, monkeypatch):
    from cli_agent_orchestrator.decisions import catalog as cat

    bad = json.loads(json.dumps(CATALOG))
    bad["models"]["codex"]["gpt-5.5"]["cost"] = "cheap"
    bad["models"]["codex"]["gpt-old"] = {"context_tokens": 1, "cost": "low"}
    settings_service._save(
        {"model_tiers": TIERS, "ephemeral": EPHEMERAL, "decisions": {"match": bad}}
    )
    dropped = "decisions.match.models.codex.gpt-5.5.cost must be low, medium or high"
    unreachable = "codex/gpt-old is unreachable"

    def invoke(*args):
        # A load warns only about problems the previous load didn't report: start each fresh.
        monkeypatch.setattr(cat, "_last_problems", frozenset())
        caplog.clear()
        with caplog.at_level("WARNING", logger=cat.logger.name):
            result = run(*args)
        logged = [r.getMessage() for r in caplog.records if r.name == cat.logger.name]
        return result, logged

    result, logged = invoke("check")
    assert result.exit_code == 1, result.output
    assert logged == []
    lines = result.stdout.splitlines()
    assert [line for line in lines if dropped in line] == [f"WARN  {dropped}; declaration dropped"]
    assert [line for line in lines if unreachable in line] == [
        f"WARN  {unreachable}: no model_tiers.codex tier maps it, so it is never used"
    ]
    result, logged = invoke("show")
    assert result.exit_code == 0 and logged == []
    assert sum(dropped in line for line in result.stdout.splitlines()) == 1
    result, logged = invoke("show", "--json")
    assert logged == [] and len(json.loads(result.stdout)["warnings"]) == 2
    # explain prints no warnings of its own, so its load still logs them: the log is live.
    result, logged = invoke(
        "explain", "--needs", "read_only", "--size", "medium", "--fallback", "codex"
    )
    assert result.exit_code == 0 and len(logged) == 2
    assert all(dropped not in line for line in result.stdout.splitlines())
    # A quiet load doesn't count as reported: the next load still warns.
    from cli_agent_orchestrator.decisions import settings as decision_settings

    monkeypatch.setattr(cat, "_last_problems", frozenset())
    run("check")
    caplog.clear()
    with caplog.at_level("WARNING", logger=cat.logger.name):
        decision_settings.load_settings()
    assert len([r for r in caplog.records if r.name == cat.logger.name]) == 2


def test_the_quick_start_settings_cover_long_context_at_large(home):
    doc = (Path(__file__).resolve().parents[2] / "docs" / "decisions.md").read_text()
    section = doc[doc.index("### Quick start") : doc.index("### An example with Claude Code")]
    (block,) = re.findall(r"```json\n(.*?)```", section, re.S)
    settings_service._save(json.loads(block))
    args = ["explain", "--needs", "long_context", "--size", "medium", "--options", "claude_code"]
    result = run(*args, "--tier-auto")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-1] == (
        "rule: covered (long_context) · tie-break: only · pick: claude_code/large"
    )
    assert f"`cao decisions match {' '.join(args)} --tier-auto`" in section
    from cli_agent_orchestrator.services import ephemeral_service

    ephemeral_service.validate_block(settings_service.get_ephemeral_settings())


def test_explain_prints_the_candidate_table_and_the_pick(home):
    result = run(
        "explain",
        "--needs",
        "long_context",
        "--size",
        "medium",
        "--tier-auto",
        "--fallback",
        "claude_code",
    )
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[1] == (
        "options: claude_code (hard) · tiers: medium..large (size medium, ceiling large) · "
        "fallback claude_code"
    )
    assert lines[3].split() == ["candidate", "model", "context", "cost", "long_context"]
    assert lines[4].split() == ["claude_code/medium", "claude-sonnet-5-5", "200000", "medium", "-"]
    assert lines[5].split() == [
        "claude_code/large",
        "claude-opus-5-5",
        "1000000",
        "high",
        "yes",
        "◄",
        "pick",
    ]
    assert lines[-1] == "rule: covered (long_context) · tie-break: only · pick: claude_code/large"


def test_explain_on_todays_table_rejects_an_advisory_codex_pick(home):
    # docs/decisions.md says so: on the real capability table Codex is advisory, so the match
    # refuses to pair it with hard Claude Code and the default runs.
    args = ["explain", "--needs", "long_context,read_only", "--size", "medium", "--tier-auto"]
    args += ["--options", "claude_code,codex"]
    result = run(*args)
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[1].startswith("options: claude_code (hard), codex (advisory) · ")
    assert lines[-1] == "rule: rejected · runs the default: claude_code/medium"
    data = json.loads(run(*args, "--json").output)
    assert (data["rule"], data["pick"], data["runs"]) == ("rejected", None, "claude_code/medium")


def test_explain_without_tier_auto_keeps_the_size(home):
    data = json.loads(
        run(
            "explain",
            "--needs",
            "long_context",
            "--size",
            "medium",
            "--fallback",
            "claude_code",
            "--json",
        ).output
    )
    assert (data["rule"], data["pick"], data["runs"], data["tiers"]) == (
        "no_cover",
        None,
        "claude_code/medium",
        ["medium"],
    )


@pytest.mark.parametrize(
    "args,error",
    [
        (["--needs", "needs_network"], "needs_network is advisory only and never routes"),
        (["--needs", "speed"], "unknown need: speed"),
        (["--options", "grok"], "unknown option: grok"),
        (
            ["--options", "claude_code", "--fallback", "codex"],
            "--fallback must be one of --options",
        ),
        ([], "provide --options or --fallback"),
        (["--needs", "read_only,read_only", "--fallback", "codex"], "need must not repeat"),
    ],
)
def test_explain_refuses_bad_inputs(home, args, error):
    result = run("explain", "--size", "medium", *args)
    assert result.exit_code == 2
    assert error in result.output


class Decider:
    name = "feat"
    version = "1"
    points = frozenset({"model.route", "effort.route", *FEATURE_POINTS})

    def __init__(self, answers):
        self.answers = answers

    async def decide(self, request, config):
        return {p: DecisionAnswer(self.answers[p], {self.answers[p]: 0.9}) for p in request.points}


@pytest.mark.parametrize("options", [("claude_code",), ("claude_code", "codex")])
def test_AC16_explain_prints_the_records_pick_rule_tiebreak_and_hash(home, monkeypatch, options):
    from dataclasses import replace

    from cli_agent_orchestrator.clients.database import DecisionRecordModel
    from cli_agent_orchestrator.decisions.engine import DecisionEngine, DelegationRequest
    from cli_agent_orchestrator.decisions.match import MATCH_DECIDER
    from cli_agent_orchestrator.decisions.policy import PolicyBounds
    from cli_agent_orchestrator.decisions.registry import DeciderRegistry
    from cli_agent_orchestrator.decisions.settings import PointSettings, load_settings
    from cli_agent_orchestrator.decisions.shadow import ShadowRunner
    from cli_agent_orchestrator.decisions.store import DecisionStore
    from cli_agent_orchestrator.decisions.targets import TargetKind
    from cli_agent_orchestrator.decisions.types import PointState, ProviderOptions
    from cli_agent_orchestrator.utils import ephemeral_capabilities as caps

    # As #1174's tests do: the contained Codex launch's class, so the two can share a set.
    monkeypatch.setitem(
        caps.CAPABILITIES, "codex", replace(caps.CAPABILITIES["codex"], enforcement=caps.SANDBOXED)
    )
    db = create_engine("sqlite:///" + str(home / "state.db"))
    DecisionRecordModel.__table__.create(db)
    store = DecisionStore(
        sessionmaker(bind=db), home / "db" / "decision-hash.key", emit=lambda _: None
    )
    answers = {
        "model.route": "medium",
        "effort.route": "high",
        "task.long_context": "yes",
        "task.deep_reasoning": "no",
        "task.read_only": "yes",
        "task.needs_network": "no",
    }
    decider = Decider(answers)
    snapshot = load_settings(environment={})
    snapshot = replace(
        snapshot,
        points={
            "provider.route": PointSettings(PointState.ON, MATCH_DECIDER),
            "model.route": PointSettings(PointState.ON, "feat"),
            "effort.route": PointSettings(PointState.OFF, "feat"),
            "task.features": PointSettings(PointState.ON, "feat"),
        },
    )
    engine = DecisionEngine(
        DeciderRegistry({"feat": lambda: decider}),
        store,
        ShadowRunner(store),
        settings_loader=lambda: snapshot,
        honors_model=lambda *a: True,
        honors_effort=lambda *a: True,
    )
    req = DelegationRequest(
        "assign",
        "claude_code",
        "Ramones-code_reading-3f9a",
        None,
        "Read the whole payments service and map every caller of the charge API.",
        target_kind=TargetKind.EPHEMERAL,
        field_states={"provider.route": "auto", "model.route": "auto"},
        policy=PolicyBounds(
            min_tier="small",
            default_tier="medium",
            max_tier="large",
            allowed_providers=frozenset(TIERS),
        ),
        purpose="code_reading",
        description="Reads a large codebase end to end",
        provider_options=ProviderOptions(options, {p: caps.enforcement_label(p) for p in options}),
        launch_tier="medium",
        network_granted=False,
    )
    asyncio.run(engine.prepare_launch(req))
    (record,) = [row for row in store.list() if row["point"] == "model.route"]
    assert record["match_rule"] == "covered"
    result = run(
        "explain",
        "--needs",
        ",".join(record["match_needs"]),
        "--size",
        record["match_from"],
        "--options",
        ",".join(options),
        "--fallback",
        "claude_code",
        "--tier-auto",
        "--json",
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert (data["pick"], data["rule"], data["tiebreak"], data["catalog_hash"]) == (
        record["match_pick"],
        record["match_rule"],
        record["match_tiebreak"],
        record["catalog_hash"],
    )
    assert data["hash_key_id"] == record["hash_key_id"]
    assert data["pick"] == ("claude_code/large" if len(options) == 1 else "codex/large")
