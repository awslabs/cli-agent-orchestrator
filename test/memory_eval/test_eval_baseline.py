"""Regression contract for the synthetic memory-evaluation harness."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from test.memory_eval.harness import (
    ARMS,
    FIXTURE_DIR,
    _evaluate_arm,
    assert_temporary_home,
    evaluate,
)

import pytest

from cli_agent_orchestrator.services.config_service import MemoryConfig


@pytest.fixture(scope="module")
def baseline_report(tmp_path_factory):
    started = time.monotonic()
    report = evaluate(tmp_path_factory.mktemp("memory-eval") / "memory-eval-home")
    return report, time.monotonic() - started


def test_fixture_corpus_has_required_synthetic_shapes():
    notes = json.loads((FIXTURE_DIR / "notes.json").read_text(encoding="utf-8"))
    questions = json.loads((FIXTURE_DIR / "questions.json").read_text(encoding="utf-8"))
    assert len(notes) >= 60
    assert {note["scope"] for note in notes} >= {"global", "project", "session"}
    assert sum(note["shape"] == "compiled_without_heading" for note in notes) == 6
    assert any(note["shape"] == "minute_precision" for note in notes)
    assert any(len(note["key"]) == 61 for note in notes)
    assert sum("supersedes" in note for note in notes) == 3
    assert {note["key"]: note["supersedes"] for note in notes if "supersedes" in note} == {
        "revision-01-current": "revision-01-stale",
        "revision-02-current": "revision-02-stale",
        "revision-03-current": "revision-03-stale",
    }
    assert sum(note.get("contradiction", False) for note in notes) == 2

    hidden_shape_notes = {
        note["key"]: note["content"] for note in notes if note["shape"] != "normal"
    }
    assert len(set(hidden_shape_notes.values())) == len(hidden_shape_notes)
    question_gold = {key for question in questions for key in question["gold"]}
    assert hidden_shape_notes.keys() <= question_gold


def _assert_non_temporary_home_rejected():
    non_temporary_home = Path(tempfile.gettempdir()).resolve().parent / "not-a-temp-home"
    with pytest.raises(ValueError, match="temporary"):
        assert_temporary_home(non_temporary_home)


def test_harness_refuses_non_temporary_home():
    _assert_non_temporary_home_rejected()


def test_non_temporary_home_guard_from_checkout_under_tmp(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.chdir(checkout)

    _assert_non_temporary_home_rejected()


def test_default_baseline_preserves_known_failure_shapes(baseline_report):
    report, elapsed = baseline_report

    assert set(ARMS) <= set(report["arms"])
    assert report["arms"]["default"]["invisible_notes"] == 7
    assert (
        report["arms"]["p1a_visibility"]["invisible_notes"]
        <= report["arms"]["default"]["invisible_notes"]
    )
    assert report["arms"]["default"]["kw_p1"] < report["arms"]["bm25"]["kw_p1"]
    assert report["arms"]["bm25"]["kw_p1"] == pytest.approx(0.56, abs=0.005)
    assert report["arms"]["default"]["kw_p1"] <= report["arms"]["p3_bm25"]["kw_p1"]
    assert report["arms"]["default"]["multihop_full"] < 1
    assert report["arms"]["default"]["current_first"] < 0.85
    assert report["arms"]["default"]["tokens_per_recall"] > 800
    assert elapsed < 60


def test_p1a_arm_does_not_regress_retrieval_when_parser_setting_exists(baseline_report):
    if "parser" not in MemoryConfig.model_fields:
        pytest.skip("requires memory.parser.lenient from #1331")

    report = baseline_report[0]["arms"]
    default = report["default"]
    p1a = report["p1a_visibility"]
    for metric in ("kw_p1", "kw_p3", "nl_p1", "nl_p3"):
        assert p1a[metric] >= default[metric]
    assert p1a["kw_p1"] > default["kw_p1"]
    assert p1a["nl_p3"] > default["nl_p3"]


def test_arm_settings_are_isolated(tmp_path, monkeypatch, baseline_report):
    baseline = baseline_report[0]["arms"]["default"]
    monkeypatch.setattr(
        "test.memory_eval.harness.ARMS",
        {
            "unknown_setting": {
                "settings": {"memory": {"unknown_eval_setting": True}},
                "recall": {
                    "search_mode": "hybrid",
                    "sort_by": "recency",
                    "include_related": False,
                    "limit": 10,
                },
            },
            "default": ARMS["default"],
        },
    )

    report = evaluate(tmp_path / "with-unknown-setting")

    assert report["arms"]["default"] == baseline


def test_results_are_stable_across_python_hash_seeds(tmp_path):
    outputs = []
    for seed in range(8):
        home = tmp_path / f"seed-{seed}"
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = str(seed)
        env["CAO_HOME_DIR"] = str(home)
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "test.memory_eval.harness",
                "--home",
                str(home),
                "--arm",
                "default",
            ],
            check=True,
            capture_output=True,
            env=env,
            text=True,
        )
        outputs.append(completed.stdout)

    assert len(set(outputs)) == 1


@pytest.mark.parametrize("arm_name", ARMS)
def test_current_first_is_independent_of_wiki_listing_order(tmp_path, monkeypatch, arm_name):
    original_rglob = Path.rglob

    def current_first(reverse: bool) -> float:
        def ordered_rglob(path, pattern):
            return iter(sorted(original_rglob(path, pattern), key=str, reverse=reverse))

        with monkeypatch.context() as context:
            context.setattr(Path, "rglob", ordered_rglob)
            metrics = asyncio.run(_evaluate_arm(tmp_path / f"ordered-{reverse}", ARMS[arm_name]))
        return metrics["current_first"]

    expected = 0.0 if arm_name in {"bm25", "p4_relationships"} else 0.4
    assert current_first(reverse=False) == current_first(reverse=True) == expected


def test_cli_leaves_process_home_untouched(tmp_path):
    process_home = tmp_path / "process-home"
    process_home.mkdir()
    fixture_home = tmp_path / "fixture-home"
    env = os.environ.copy()
    env["HOME"] = str(process_home)
    env.pop("CAO_HOME_DIR", None)

    subprocess.run(
        [
            sys.executable,
            "-m",
            "test.memory_eval.harness",
            "--home",
            str(fixture_home),
            "--arm",
            "default",
        ],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )

    assert list(process_home.iterdir()) == []
