"""Regression contract for the synthetic memory-evaluation harness."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from test.memory_eval.harness import (
    ARMS,
    FIXTURE_DIR,
    assert_temporary_home,
    evaluate,
)

import pytest


def test_fixture_corpus_has_required_synthetic_shapes():
    notes = json.loads((FIXTURE_DIR / "notes.json").read_text(encoding="utf-8"))
    assert len(notes) >= 60
    assert {note["scope"] for note in notes} >= {"global", "project", "session"}
    assert sum(note["shape"] == "compiled_without_heading" for note in notes) == 6
    assert any(note["shape"] == "minute_precision" for note in notes)
    assert any(len(note["key"]) == 61 for note in notes)
    assert sum("supersedes" in note for note in notes) == 3
    assert sum(note.get("contradiction", False) for note in notes) == 2


def test_harness_refuses_non_temporary_home():
    with pytest.raises(ValueError, match="temporary"):
        assert_temporary_home(Path.cwd())


def test_default_baseline_preserves_known_failure_shapes(tmp_path):
    started = time.monotonic()
    report = evaluate(tmp_path / "memory-eval-home")

    assert set(ARMS) <= set(report["arms"])
    assert report["arms"]["default"]["invisible_notes"] == 7
    assert (
        report["arms"]["p1a_visibility"]["invisible_notes"]
        <= report["arms"]["default"]["invisible_notes"]
    )
    assert report["arms"]["default"]["kw_p1"] < report["arms"]["bm25"]["kw_p1"]
    assert report["arms"]["default"]["kw_p1"] <= report["arms"]["p3_bm25"]["kw_p1"]
    assert report["arms"]["default"]["multihop_full"] < 1
    assert time.monotonic() - started < 60


def test_arm_settings_are_isolated(tmp_path, monkeypatch):
    baseline = evaluate(tmp_path / "baseline")["arms"]["default"]
    monkeypatch.setitem(
        ARMS,
        "unknown_setting",
        {
            "settings": {"memory": {"unknown_eval_setting": True}},
            "recall": {
                "search_mode": "hybrid",
                "sort_by": "recency",
                "include_related": False,
                "limit": 10,
            },
        },
    )

    report = evaluate(tmp_path / "with-unknown-setting")

    assert report["arms"]["default"] == baseline


def test_results_are_stable_across_python_hash_seeds(tmp_path):
    script = (
        "import json, sys; "
        "from pathlib import Path; "
        "from test.memory_eval.harness import evaluate; "
        "print(json.dumps(evaluate(Path(sys.argv[1])), sort_keys=True))"
    )
    outputs = []
    for seed in range(8):
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = str(seed)
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(tmp_path / f"seed-{seed}"),
            ],
            check=True,
            capture_output=True,
            env=env,
            text=True,
        )
        outputs.append(completed.stdout)

    assert len(set(outputs)) == 1
