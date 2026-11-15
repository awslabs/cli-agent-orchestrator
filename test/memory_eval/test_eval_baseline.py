"""Regression contract for the synthetic memory-evaluation harness."""

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
    assert_temporary_home,
    evaluate,
)

import pytest


@pytest.fixture(scope="module")
def baseline_report(tmp_path_factory):
    started = time.monotonic()
    report = evaluate(tmp_path_factory.mktemp("memory-eval") / "memory-eval-home")
    return report, time.monotonic() - started


def test_fixture_corpus_has_required_synthetic_shapes():
    notes = json.loads((FIXTURE_DIR / "notes.json").read_text(encoding="utf-8"))
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
    assert report["arms"]["default"]["kw_p1"] <= report["arms"]["p3_bm25"]["kw_p1"]
    assert report["arms"]["default"]["multihop_full"] < 1
    assert report["arms"]["default"]["current_first"] < 0.85
    assert report["arms"]["default"]["tokens_per_recall"] > 800
    assert elapsed < 60


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
