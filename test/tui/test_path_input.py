"""Unit tests for the ``cao tui`` path field validator (``path_input.py``, U5).

:class:`PathInput` owns no validation logic — it delegates to the shared
:func:`cli_agent_orchestrator.utils.path_validation.resolve_and_validate_path`
(SC-3 / FR-8.1). These tests therefore verify *delegation and error
translation*, not path policy: a valid directory canonicalizes, whatever the
shared validator rejects (blocked system dir, missing target, no valid
ancestor) surfaces as a renderable :class:`PathInputError`, and ``allow_create``
is forwarded so a not-yet-existing target under a good ancestor passes. A spy
test asserts the call is forwarded to the shared validator verbatim.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.tui.path_input import PathInput, PathInputError

# -- happy path -------------------------------------------------------------


def test_valid_existing_dir_returns_canonical_path(tmp_path) -> None:
    """A real existing directory returns its canonicalized absolute path."""

    result = PathInput().validate(str(tmp_path))

    # The shared validator canonicalizes via os.path.realpath; the delegate
    # must return exactly that (e.g. /var -> /private/var on macOS).
    assert result == os.path.realpath(str(tmp_path))
    assert os.path.isdir(result)


def test_allow_create_under_good_ancestor_returns_target(tmp_path) -> None:
    """``allow_create`` permits a missing target under a valid ancestor."""

    target = tmp_path / "new_output_dir"
    assert not target.exists()

    result = PathInput("Output path").validate(str(target), allow_create=True)

    assert result == os.path.realpath(str(target))


# -- error translation (delegated rejections) -------------------------------


def test_blocked_system_dir_raises_field_error() -> None:
    """A blocked system directory (/etc) is rejected as a PathInputError."""

    with pytest.raises(PathInputError) as exc_info:
        PathInput().validate("/etc")

    # The validator's decision/message is preserved for inline rendering.
    assert "not allowed" in str(exc_info.value)


def test_relative_nonexistent_path_raises_field_error() -> None:
    """A relative, nonexistent path surfaces as a PathInputError."""

    with pytest.raises(PathInputError):
        PathInput().validate("some/relative/path-that-does-not-exist")


def test_nonexistent_absolute_path_raises_field_error(tmp_path) -> None:
    """A nonexistent target without ``allow_create`` is rejected."""

    missing = tmp_path / "definitely" / "missing"

    with pytest.raises(PathInputError) as exc_info:
        PathInput().validate(str(missing))

    assert "does not exist" in str(exc_info.value)


def test_field_error_is_a_valueerror() -> None:
    """PathInputError subclasses ValueError so existing handlers still catch it."""

    assert issubclass(PathInputError, ValueError)


# -- delegation (SC-3: no local validation) ---------------------------------


def test_validate_delegates_to_shared_validator(tmp_path) -> None:
    """``validate`` forwards verbatim to the shared validator (no re-check)."""

    with patch(
        "cli_agent_orchestrator.tui.path_input.resolve_and_validate_path",
        return_value="/canonical/result",
    ) as spy:
        result = PathInput("Working directory").validate(str(tmp_path), allow_create=True)

    assert result == "/canonical/result"
    spy.assert_called_once_with(
        str(tmp_path),
        allow_create=True,
        allow_file=False,
        description="Working directory",
    )


def test_validate_wraps_validator_valueerror(tmp_path) -> None:
    """A ValueError from the shared validator becomes a PathInputError, chained."""

    boom = ValueError("Working directory not allowed: boom")
    with patch(
        "cli_agent_orchestrator.tui.path_input.resolve_and_validate_path",
        side_effect=boom,
    ):
        with pytest.raises(PathInputError) as exc_info:
            PathInput().validate("/whatever")

    assert str(exc_info.value) == "Working directory not allowed: boom"
    assert exc_info.value.__cause__ is boom
