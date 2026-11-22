"""Tests for the opt-in, reproducible pytest collection shuffle (#1350)."""

import os
import random
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from test import conftest
from types import SimpleNamespace

import pytest


class _Item:
    def __init__(self, nodeid: str):
        self.nodeid = nodeid


def _items():
    return [_Item(f"test_sample.py::test_{letter}") for letter in "abcdef"]


def _nodeids(items):
    return [item.nodeid for item in items]


def test_shuffle_is_byte_for_byte_unchanged_when_unset():
    items = _items()
    original = list(items)

    conftest.pytest_collection_modifyitems(SimpleNamespace(_cao_test_shuffle_seed=None), items)

    assert items == original


def test_seeded_shuffle_is_a_deterministic_permutation():
    first = _items()
    second = _items()
    original = _nodeids(first)

    conftest.pytest_collection_modifyitems(SimpleNamespace(_cao_test_shuffle_seed=1350), first)
    conftest.pytest_collection_modifyitems(SimpleNamespace(_cao_test_shuffle_seed=1350), second)

    assert _nodeids(first) == _nodeids(second)
    assert sorted(_nodeids(first)) == sorted(original)
    assert _nodeids(first) != original


def test_seed_is_printed_at_the_start_and_end_of_a_shuffled_run():
    config = SimpleNamespace(_cao_test_shuffle_seed=1350)
    reporter = SimpleNamespace(lines=[])
    reporter.write_line = reporter.lines.append

    assert conftest.pytest_report_header(config) == "CAO_TEST_SHUFFLE_SEED: 1350"
    conftest.pytest_terminal_summary(reporter, 0, config)

    assert reporter.lines == ["CAO_TEST_SHUFFLE_SEED: 1350"]


def test_bad_seed_is_refused_with_a_clear_message():
    with pytest.raises(pytest.UsageError, match="CAO_TEST_SHUFFLE_SEED must be an integer"):
        conftest._test_shuffle_seed({"CAO_TEST_SHUFFLE_SEED": "not-a-number"})


def test_shuffle_seed_reaches_pytest_configure_and_changes_collection():
    repo_root = Path(__file__).resolve().parents[1]
    probe_dir = repo_root / "test" / "_support" / f"shuffle_probe_{uuid.uuid4().hex}"
    probe_dir.mkdir()
    test_file = probe_dir / "test_collected.py"
    plugin_name = f"shuffle_probe_plugin_{uuid.uuid4().hex}"
    plugin_file = probe_dir / f"{plugin_name}.py"
    test_file.write_text(
        "\n".join(f"def test_{letter}(): pass" for letter in "abcdef"),
        encoding="utf-8",
    )
    # A plugin loaded with -p registers before test/conftest.py, so without
    # trylast=True the shuffle would run before this reordering hook.
    plugin_file.write_text(
        """
def pytest_collection_modifyitems(items):
    items.reverse()
""",
        encoding="utf-8",
    )

    def collect(seed: str) -> tuple[list[str], str]:
        env = os.environ.copy()
        env["CAO_TEST_SHUFFLE_SEED"] = seed
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(probe_dir), env.get("PYTHONPATH")]))
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "-p",
                plugin_name,
                "-p",
                "no:cacheprovider",
                "-o",
                "addopts=",
                str(test_file),
            ],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return (
            [line for line in result.stdout.splitlines() if "::test_" in line],
            result.stdout,
        )

    try:
        order, output = collect("0")
    finally:
        test_file.unlink(missing_ok=True)
        plugin_file.unlink(missing_ok=True)
        shutil.rmtree(probe_dir)

    expected = [f"test/_support/{probe_dir.name}/test_collected.py::test_{letter}" for letter in "fedcba"]
    random.Random(0).shuffle(expected)

    assert order == expected
    assert order != list(reversed(expected))
    assert "CAO_TEST_SHUFFLE_SEED: 0" in output
