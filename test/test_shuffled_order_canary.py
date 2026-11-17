"""Tests for the opt-in, reproducible pytest collection shuffle (#1350)."""

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
