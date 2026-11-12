"""#1199: the local decisions.match catalog: validation, canonical form, hash and check."""

import copy
import hashlib
import hmac
import json
import os
import subprocess
import sys

import pytest

from cli_agent_orchestrator.decisions import catalog as cat
from cli_agent_orchestrator.decisions.catalog import MatchCatalog, ModelFacts, load_catalog

TIERS = {
    "claude_code": {"medium": "claude-sonnet-5-5", "large": "claude-opus-5-5"},
    "codex": {"medium": "gpt-5.5", "large": "gpt-5.5-pro"},
}


def block(**overrides):
    value = {
        "version": 1,
        "long_context_tokens": 500000,
        "models": {
            "claude_code": {
                "claude-sonnet-5-5": {"context_tokens": 200000, "cost": "medium", "good_for": []},
                "claude-opus-5-5": {
                    "context_tokens": 1000000,
                    "cost": "high",
                    "good_for": ["deep_reasoning"],
                },
            },
            "codex": {
                "gpt-5.5": {"context_tokens": 400000, "cost": "low", "good_for": ["read_only"]},
            },
        },
    }
    value.update(overrides)
    return value


def load(value, tiers=TIERS):
    return load_catalog({"decisions": {"match": value}}, tiers)


def test_a_valid_catalog_loads_every_declaration():
    loaded = load(block())
    assert loaded.valid and loaded.version == 1 and loaded.warnings == ()
    assert loaded.long_context_tokens == 500000
    assert loaded.facts("claude_code", "claude-opus-5-5") == ModelFacts(
        1000000, "high", ("deep_reasoning",)
    )
    assert loaded.facts("codex", "gpt-5.5") == ModelFacts(400000, "low", ("read_only",))
    assert loaded.model_count() == 3


@pytest.mark.parametrize(
    "raw",
    [{}, {"decisions": None}, {"decisions": {}}, {"decisions": {"points": {}}}],
)
def test_an_absent_block_is_a_valid_empty_catalog(raw):
    loaded = load_catalog(raw, TIERS)
    assert loaded == MatchCatalog()
    assert loaded.valid and loaded.models == {} and loaded.long_context_tokens == 500000


def test_defaults_fill_optional_keys():
    loaded = load({"version": 1})
    assert loaded.valid and loaded.models == {} and loaded.ignore_features == ()
    assert loaded.long_context_tokens == 500000
    value = block()
    del value["models"]["claude_code"]["claude-sonnet-5-5"]["good_for"]
    assert load(value).facts("claude_code", "claude-sonnet-5-5").good_for == ()


# AC11: a bad entry is dropped, with a warning that names its key path.
BAD_DECLARATIONS = [
    (
        "unknown_key",
        {"context_tokens": 1, "cost": "low", "notes": "x"},
        "decisions.match.models.codex.gpt-x.notes is not a known key; declaration dropped",
    ),
    (
        "cost_cheap",
        {"context_tokens": 1, "cost": "cheap"},
        "decisions.match.models.codex.gpt-x.cost must be low, medium or high; declaration dropped",
    ),
    (
        "missing_cost",
        {"context_tokens": 1},
        "decisions.match.models.codex.gpt-x.cost is required; declaration dropped",
    ),
    (
        "missing_context",
        {"cost": "low"},
        "decisions.match.models.codex.gpt-x.context_tokens is required; declaration dropped",
    ),
    (
        "context_zero",
        {"context_tokens": 0, "cost": "low"},
        "decisions.match.models.codex.gpt-x.context_tokens must be an integer from 1 to "
        "100000000; declaration dropped",
    ),
    (
        "context_too_big",
        {"context_tokens": 100000001, "cost": "low"},
        "decisions.match.models.codex.gpt-x.context_tokens must be an integer from 1 to "
        "100000000; declaration dropped",
    ),
    (
        "context_bool",
        {"context_tokens": True, "cost": "low"},
        "decisions.match.models.codex.gpt-x.context_tokens must be an integer from 1 to "
        "100000000; declaration dropped",
    ),
    (
        "context_text",
        {"context_tokens": "1000", "cost": "low"},
        "decisions.match.models.codex.gpt-x.context_tokens must be an integer from 1 to "
        "100000000; declaration dropped",
    ),
    (
        "good_for_long_context",
        {"context_tokens": 1, "cost": "low", "good_for": ["long_context"]},
        "decisions.match.models.codex.gpt-x.good_for must list distinct deep_reasoning or "
        "read_only; declaration dropped",
    ),
    (
        "good_for_network",
        {"context_tokens": 1, "cost": "low", "good_for": ["needs_network"]},
        "decisions.match.models.codex.gpt-x.good_for must list distinct deep_reasoning or "
        "read_only; declaration dropped",
    ),
    (
        "good_for_duplicate",
        {"context_tokens": 1, "cost": "low", "good_for": ["read_only", "read_only"]},
        "decisions.match.models.codex.gpt-x.good_for must list distinct deep_reasoning or "
        "read_only; declaration dropped",
    ),
    (
        "good_for_text",
        {"context_tokens": 1, "cost": "low", "good_for": "read_only"},
        "decisions.match.models.codex.gpt-x.good_for must list distinct deep_reasoning or "
        "read_only; declaration dropped",
    ),
    (
        "not_an_object",
        ["context_tokens", 1],
        "decisions.match.models.codex.gpt-x must be an object; declaration dropped",
    ),
]


@pytest.mark.parametrize(
    "case,declaration,warning", BAD_DECLARATIONS, ids=[c[0] for c in BAD_DECLARATIONS]
)
def test_AC11_bad_declaration_dropped_with_key_path(case, declaration, warning, caplog):
    value = block()
    value["models"]["codex"]["gpt-x"] = declaration
    load(block())
    caplog.clear()
    loaded = load(value)
    assert loaded.valid
    assert loaded.facts("codex", "gpt-x") is None
    # The good declarations around it survive.
    assert loaded.model_count() == 3
    assert warning in loaded.warnings
    assert any(warning in record.getMessage() for record in caplog.records)


def _warned(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("Decision match")]


def test_a_problem_warns_once_while_it_lasts_and_again_when_it_comes_back(caplog):
    broken = block()
    broken["models"]["codex"]["gpt-x"] = {"context_tokens": 0, "cost": "low"}
    warning = (
        "decisions.match.models.codex.gpt-x.context_tokens must be an integer from 1 to "
        "100000000; declaration dropped"
    )
    load(block())
    caplog.clear()
    load(broken)
    assert _warned(caplog) == ["Decision match catalog: " + warning]
    # The same catalog, read on every launch, warns once.
    caplog.clear()
    load(broken)
    load(broken)
    assert _warned(caplog) == []
    # Fixed, then broken again the same way: it warns again.
    load(block())
    caplog.clear()
    load(broken)
    assert _warned(caplog) == ["Decision match catalog: " + warning]
    # A structural error, fixed and back, too; and an absent block counts as fixed.
    caplog.clear()
    load(block(version=2))
    load(block(version=2))
    load_catalog({}, TIERS)
    load(block(version=2))
    assert (
        _warned(caplog)
        == [
            "Decision match catalog: decisions.match.version must be 1; matching is off for every "
            "launch"
        ]
        * 2
    )
    # A second problem warns alone; the one still there doesn't warn again.
    twice = copy.deepcopy(broken)
    twice["models"]["codex"]["gpt-y"] = {"context_tokens": 1, "cost": "free"}
    load(broken)
    caplog.clear()
    load(twice)
    assert _warned(caplog) == [
        "Decision match catalog: decisions.match.models.codex.gpt-y.cost must be low, medium or "
        "high; declaration dropped"
    ]


def test_bad_model_ids_and_providers_are_dropped_without_echoing_them():
    value = block()
    value["models"]["codex"]["bad id; rm -rf"] = {"context_tokens": 1, "cost": "low"}
    value["models"]["not_a_cli"] = {"m": {"context_tokens": 1, "cost": "low"}}
    value["models"]["copilot_cli"] = ["not", "an", "object"]
    loaded = load(value)
    assert loaded.valid and loaded.model_count() == 3
    assert loaded.warnings == (
        "decisions.match.models.codex.<invalid> is not a valid model id; declaration dropped",
        "decisions.match.models.not_a_cli is not a CAO provider; dropped",
        "decisions.match.models.copilot_cli must be an object; dropped",
    )
    assert all("rm -rf" not in warning for warning in loaded.warnings)


STRUCTURAL = [
    ("not_an_object", ["version", 1], "decisions.match must be an object"),
    ("version_2", block(version=2), "decisions.match.version must be 1"),
    ("version_missing", {"models": {}}, "decisions.match.version must be 1"),
    ("version_text", block(version="1"), "decisions.match.version must be 1"),
    ("version_bool", block(version=True), "decisions.match.version must be 1"),
    (
        "unknown_top_key",
        block(ignore_feature=["read_only"]),
        "decisions.match.ignore_feature is not a known key",
    ),
    ("models_list", block(models=[]), "decisions.match.models must be an object"),
    (
        "threshold_low",
        block(long_context_tokens=999),
        "decisions.match.long_context_tokens must be an integer from 1000 to 100000000",
    ),
    (
        "threshold_text",
        block(long_context_tokens="500000"),
        "decisions.match.long_context_tokens must be an integer from 1000 to 100000000",
    ),
    (
        "ignore_network",
        block(ignore_features=["needs_network"]),
        "decisions.match.ignore_features must list distinct long_context, deep_reasoning or "
        "read_only",
    ),
    (
        "ignore_text",
        block(ignore_features="read_only"),
        "decisions.match.ignore_features must list distinct long_context, deep_reasoning or "
        "read_only",
    ),
]


@pytest.mark.parametrize("case,value,reason", STRUCTURAL, ids=[c[0] for c in STRUCTURAL])
def test_AC11_structural_errors_disable_matching(case, value, reason):
    loaded = load(value)
    assert not loaded.valid
    assert loaded.invalid == reason
    assert loaded.version is None
    assert loaded.models == {} and loaded.canonical() is None
    assert loaded.facts("claude_code", "claude-opus-5-5") is None


def test_AC8_an_unreachable_declaration_is_kept_and_warned():
    value = block()
    value["models"]["codex"]["gpt-old"] = {"context_tokens": 9000000, "cost": "low"}
    loaded = load(value)
    assert loaded.facts("codex", "gpt-old") == ModelFacts(9000000, "low", ())
    assert (
        "decisions.match.models.codex.gpt-old is unreachable: no model_tiers.codex tier maps "
        "it, so it is never used"
    ) in loaded.warnings
    assert cat.unreachable(loaded.models, TIERS) == [("codex", "gpt-old")]


def test_ignore_features_are_kept_in_routing_order():
    loaded = load(block(ignore_features=["read_only", "long_context"]))
    assert loaded.ignore_features == ("long_context", "read_only")


# AC12: no field accepts arbitrary text. The canary has spaces, so it is never id-shaped:
# warnings may name an id-shaped key path, never free text.
CANARY = "CANARY 1199 free text"


def _positions():
    yield "version", lambda v: v.__setitem__("version", CANARY)
    yield "long_context_tokens", lambda v: v.__setitem__("long_context_tokens", CANARY)
    yield "ignore_features", lambda v: v.__setitem__("ignore_features", [CANARY])
    yield "models", lambda v: v.__setitem__("models", CANARY)
    yield "provider_key", lambda v: v["models"].__setitem__(CANARY, {})
    yield "model_key", lambda v: v["models"]["codex"].__setitem__(
        CANARY + " with spaces", {"context_tokens": 1, "cost": "low"}
    )
    for key in ("context_tokens", "cost"):
        yield key, lambda v, key=key: v["models"]["codex"]["gpt-5.5"].__setitem__(key, CANARY)
    yield "good_for", lambda v: v["models"]["codex"]["gpt-5.5"].__setitem__("good_for", [CANARY])
    yield "extra_key", lambda v: v["models"]["codex"]["gpt-5.5"].__setitem__("note", CANARY)
    yield "extra_top_key", lambda v: v.__setitem__("note", CANARY)


@pytest.mark.parametrize("position,mutate", list(_positions()), ids=[p for p, _ in _positions()])
def test_AC12_schema_accepts_no_free_text(position, mutate):
    value = copy.deepcopy(block())
    mutate(value)
    loaded = load(value)
    # Wherever text is put, it is refused: the catalog is invalid, or the entry is dropped,
    # and the text survives nowhere in the validated catalog or its canonical form.
    assert CANARY not in json.dumps(loaded.canonical())
    assert CANARY not in repr(loaded.models)
    assert all(CANARY not in warning for warning in loaded.warnings)
    assert CANARY not in str(loaded.invalid)


def test_AC12_the_key_sets_are_pinned():
    assert cat.TOP_KEYS == ("version", "long_context_tokens", "models", "ignore_features")
    assert cat.DECLARATION_KEYS == ("context_tokens", "cost", "good_for")
    assert cat.COSTS == ("low", "medium", "high")
    assert cat.GOOD_FOR == ("deep_reasoning", "read_only")
    assert set(ModelFacts.__dataclass_fields__) == set(cat.DECLARATION_KEYS)


def test_the_canonical_form_ignores_insertion_order_and_drops_bad_entries():
    first = load(block())
    value = block()
    value["models"] = dict(reversed(list(value["models"].items())))
    value["models"]["claude_code"] = dict(reversed(list(value["models"]["claude_code"].items())))
    value["models"]["codex"]["gpt-bad"] = {"context_tokens": 1, "cost": "cheap"}
    second = load(value)
    assert first.canonical() == second.canonical()
    assert json.loads(first.canonical())["models"]["codex"] == {
        "gpt-5.5": {"context_tokens": 400000, "cost": "low", "good_for": ["read_only"]}
    }


def test_the_hash_is_the_stores_hmac_under_the_records_key(tmp_path):
    from cli_agent_orchestrator.decisions.hashing import KeyCache

    key_path = tmp_path / "decision-hash.key"
    assert cat.read_hash_key(key_path) is None
    assert not key_path.exists()  # never created by a reader
    loaded = load(block())
    expected = KeyCache(key_path).digest(loaded.canonical())
    key = cat.read_hash_key(key_path)
    assert key is not None
    assert cat.catalog_hash(loaded, key) == expected
    assert expected[0] == hmac.new(key, loaded.canonical().encode(), hashlib.sha256).hexdigest()
    assert cat.catalog_hash(load(block(version=2)), key) is None
    # A changed declaration changes the hash.
    other = block()
    other["models"]["codex"]["gpt-5.5"]["cost"] = "medium"
    assert cat.catalog_hash(load(other), key) != expected


def test_a_short_key_is_unavailable(tmp_path):
    path = tmp_path / "decision-hash.key"
    path.write_bytes(b"short")
    assert cat.read_hash_key(path) is None


@pytest.mark.parametrize("size", [31, 33, 64])
def test_a_key_of_any_length_but_32_is_unavailable_never_cut(tmp_path, size):
    # The store refuses such a key, so the CLI's hash must not exist under it either.
    path = tmp_path / "decision-hash.key"
    path.write_bytes(bytes(range(size)))
    assert cat.read_hash_key(path) is None
    path.write_bytes(bytes(range(32)))
    assert cat.read_hash_key(path) == bytes(range(32))


def test_check_reports_problems_and_notes():
    value = block()
    value["models"]["codex"]["gpt-old"] = {"context_tokens": 1, "cost": "low"}
    value["models"]["codex"]["gpt-bad"] = {"context_tokens": 1, "cost": "cheap"}
    value["models"]["kiro_cli"] = {"kiro-model": {"context_tokens": 1, "cost": "low"}}
    tiers = {**TIERS, "kiro_cli": {"medium": "kiro-model"}}
    lines, problems = cat.check(load(value, tiers), tiers, ("claude_code", "codex"))
    assert problems
    assert (
        "WARN",
        "codex/gpt-old is unreachable: no model_tiers.codex tier maps it, so it is never used",
    ) in lines
    assert (
        "WARN",
        "decisions.match.models.codex.gpt-bad.cost must be low, medium or high; declaration dropped",
    ) in lines
    assert (
        "ok",
        "claude_code: claude-sonnet-5-5 (medium), claude-opus-5-5 (large) declared and reachable",
    ) in lines
    assert (
        "note",
        "codex/large maps gpt-5.5-pro, which has no declaration, so it covers nothing",
    ) in lines
    assert (
        "note",
        "kiro_cli can't run ephemeral helpers, so its declarations are never candidates",
    ) in lines
    assert ("note", "long_context (500000): covered by claude_code/claude-opus-5-5") in lines


def test_check_passes_a_clean_catalog_and_fails_an_invalid_one():
    tiers = {"claude_code": TIERS["claude_code"], "codex": {"medium": "gpt-5.5"}}
    _, problems = cat.check(load(block(), tiers), tiers, ("claude_code", "codex"))
    assert not problems
    lines, problems = cat.check(load(block(version=2)), tiers, ("claude_code",))
    assert problems and lines == [("error", "catalog invalid: decisions.match.version must be 1")]


def test_status_shows_a_summary_never_the_catalog():
    from dataclasses import asdict

    from cli_agent_orchestrator.decisions.settings import DecisionSettings

    snapshot = DecisionSettings(match_catalog=load(block()))
    view = cat.status_view(asdict(snapshot), snapshot)
    assert "match_catalog" not in view
    assert view["match"] == {
        "catalog_valid": True,
        "catalog_version": 1,
        "models": 3,
        "warnings": 0,
        "ignore_features": [],
    }
    assert "claude-opus-5-5" not in json.dumps(view)


def test_settings_build_the_catalog_from_the_same_read_as_model_tiers():
    from cli_agent_orchestrator.decisions.settings import load_settings
    from cli_agent_orchestrator.services import settings_service

    settings_service._save({"model_tiers": TIERS, "decisions": {"match": block()}})
    loaded = load_settings()
    assert loaded.model_tiers == TIERS
    assert loaded.match_catalog == load(block())


_PROBE = r"""
import json
from cli_agent_orchestrator.decisions import catalog as cat
tiers = {"claude_code": {"medium": "m1", "large": "m2"}, "codex": {"medium": "c1"}}
value = {"version": 1, "ignore_features": ["read_only", "long_context"], "models": {
    "codex": {"c1": {"context_tokens": 5, "cost": "low", "good_for": ["read_only", "deep_reasoning"]},
              "c-old": {"context_tokens": 5, "cost": "low"},
              "c-bad": {"context_tokens": 5, "cost": "low", "zeta": 1, "alpha": 2}},
    "claude_code": {"m2": {"context_tokens": 900000, "cost": "high"}, "m1": {"context_tokens": 1, "cost": "medium"}}}}
loaded = cat.load_catalog({"decisions": {"match": value}}, tiers)
print(json.dumps([loaded.canonical(), loaded.warnings, loaded.ignore_features,
                  cat.check(loaded, tiers, ("claude_code", "codex"))]))
"""


@pytest.mark.slow
def test_catalog_output_is_identical_under_hash_seeds_0_to_7():
    outputs = set()
    for seed in range(8):
        env = {**os.environ, "PYTHONHASHSEED": str(seed)}
        result = subprocess.run(
            [sys.executable, "-B", "-c", _PROBE],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        outputs.add(result.stdout)
    assert len(outputs) == 1, outputs
