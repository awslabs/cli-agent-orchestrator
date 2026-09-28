"""``skip_if_provider_unusable`` must not skip genuine internal errors (#802 #14).

Behavioral counterpart to ``test_provider_prerequisite_classifier``: it drives
the public skip helper (which existed before the fix) and asserts a 5xx that
merely names the provider does NOT turn into a skip.
"""

from test.fixtures.cao_server import skip_if_provider_unusable

import pytest
from _pytest.outcomes import Skipped


def test_internal_error_naming_provider_is_not_skipped():
    try:
        skip_if_provider_unusable(
            500, "sqlite error while creating claude_code terminal", "claude_code"
        )
    except Skipped:
        pytest.fail("a genuine internal error was misclassified as a skippable prerequisite")


def test_command_not_found_is_still_skipped():
    with pytest.raises(Skipped):
        skip_if_provider_unusable(500, "kimi: command not found", "kimi_cli")
