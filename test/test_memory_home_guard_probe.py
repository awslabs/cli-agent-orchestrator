"""Probe nodes selected by the subprocess tests in test_home_isolation.py."""

import pytest


def test_unmarked_real_home_memory_probe():
    assert True


@pytest.mark.real_home_memory
def test_marked_real_home_memory_probe():
    assert True
