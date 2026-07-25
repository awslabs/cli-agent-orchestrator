"""U2: the CAO_ENABLE_KAS_LAUNCH opt-in constant.

Traces to FR-102, ADR-007, BR-U2-1..5.
"""

import importlib
import os
import pathlib
import re
from typing import Iterator
from unittest.mock import patch

import pytest

import cli_agent_orchestrator.constants as constants


def _resolve(env: dict[str, str]) -> bool:
    """Reload constants under a controlled environment and read the flag.

    The constant is resolved once at import (BR-U2-5), so the only way to
    exercise a different environment is a reload — never a late `os.environ`
    mutation, which would silently no-op.
    """
    env_copy = os.environ.copy()
    env_copy.pop("CAO_ENABLE_KAS_LAUNCH", None)
    env_copy.update(env)
    with patch.dict("os.environ", env_copy, clear=True):
        importlib.reload(constants)
        return constants.ENABLE_KAS_LAUNCH


@pytest.fixture(autouse=True)
def restore_constants() -> Iterator[None]:
    """Reload constants against the ambient environment after each case."""
    yield
    importlib.reload(constants)


def test_flag_defaults_off_when_unset() -> None:
    """BR-U2-1: an absent variable takes the same path as an explicit "false"."""
    assert _resolve({}) is False


def test_exact_lowercase_true_enables() -> None:
    assert _resolve({"CAO_ENABLE_KAS_LAUNCH": "true"}) is True


def test_case_insensitive_true_enables() -> None:
    assert _resolve({"CAO_ENABLE_KAS_LAUNCH": "TRUE"}) is True
    assert _resolve({"CAO_ENABLE_KAS_LAUNCH": "True"}) is True


@pytest.mark.parametrize(
    "value",
    ["1", "yes", "on", "TRUE ", " true", "", "false", "no", "0", "kas", "y"],
)
def test_every_other_value_leaves_the_flag_off(value: str) -> None:
    """BR-U2-2: the narrow truth test — nothing but the exact word enables."""
    assert _resolve({"CAO_ENABLE_KAS_LAUNCH": value}) is False


def test_only_constants_module_reads_the_environment_variable() -> None:
    """BR-U2-3: one read site, so no caller needs `os.environ` at the decision.

    Matches an environment *read* of the variable, not a prose mention: the
    launch guard names it in an operator-facing message and a docstring, which
    is not a second resolution point.
    """
    read_re = re.compile(r"environ(?:\.get\(|\[)\s*[\"']CAO_ENABLE_KAS_LAUNCH")
    src = pathlib.Path(constants.__file__).resolve().parent
    readers = [
        str(path.relative_to(src))
        for path in src.rglob("*.py")
        if read_re.search(path.read_text(encoding="utf-8"))
    ]
    assert readers == ["constants.py"], (
        "FR-102 requires CAO_ENABLE_KAS_LAUNCH to be resolved once in constants.py; "
        f"these modules also read it from the environment: {readers}"
    )
