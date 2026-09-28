"""The E2E prerequisite classifier skips only environmental failures.

``skip_if_provider_unusable`` used to skip any 5xx whose body contained the
provider name, so a genuine defect like "sqlite error while creating
claude_code terminal" was silently skipped instead of failing. The classifier
must key only on explicit environmental phrases.
"""

from test.fixtures.cao_server import is_provider_prerequisite_failure


def test_internal_error_mentioning_provider_does_not_skip():
    body = "sqlite error while creating claude_code terminal"
    assert is_provider_prerequisite_failure(500, body) is False


def test_command_not_found_skips():
    assert is_provider_prerequisite_failure(500, "kimi: command not found") is True


def test_not_installed_skips():
    assert is_provider_prerequisite_failure(503, "provider CLI is not installed") is True


def test_non_5xx_never_skips():
    assert is_provider_prerequisite_failure(404, "command not found") is False
