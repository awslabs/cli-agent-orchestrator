"""Unit tests for the in-band provider-error classifier (issue #638).

The classifier exists so a step whose "output" is a provider refusal is reported
FAILED instead of COMPLETED. Its whole value is its CONSERVATISM, so most of what
follows pins the negatives: what it must refuse to classify.
"""

from __future__ import annotations

import re

import pytest

from cli_agent_orchestrator.services.provider_error_classifier import (
    KIND_PROVIDER_ERROR,
    PROVIDER_ERROR_MAX_CHARS,
    ProviderErrorSignature,
    classify_provider_error,
)

# The verbatim shape from the issue report, plus one per shipped refusal family.
_REFUSALS = (
    "API Error (openai.gpt-5.6-terra): 400 Invocation of model ID "
    "openai.gpt-5.6-terra isn't supported.",
    "400 Invocation of model ID gpt-5.6-terra isn't supported.",
    "API Error: 401 invalid_api_key",
    "Authentication failed: no credentials configured",
    "Rate limit exceeded, retry after 30s",
    "ConnectionError: upstream refused the connection",
    "Unknown model 'gpt-5.6-terra'",
    "Model gpt-5.6-terra is not supported by this account",
)

_NON_REFUSALS = (
    # A legitimate ANSWER quoting an error signature — not the FIRST line, so the
    # anchoring guard rejects it before the signature table runs.
    "Here is the handler you asked for:\n\nAPI Error: 401 invalid_api_key",
    # ... and ordinary prose about errors, without the provider chrome.
    "The function returns None when the request fails.",
    "",
    None,
)


@pytest.mark.parametrize("output", _REFUSALS)
def test_known_provider_refusals_are_classified(output):
    match = classify_provider_error("codex", output)
    assert match is not None
    assert match.kind == KIND_PROVIDER_ERROR
    assert match.line  # the raw matched line travels for the failure message


@pytest.mark.parametrize("output", _NON_REFUSALS)
def test_non_refusals_are_never_classified(output):
    assert classify_provider_error("codex", output) is None


def test_long_output_is_out_of_scope_however_it_begins():
    """Acceptance criterion 4: the length bound is a guard in its own right."""
    long_answer = "API Error: " + ("detail " * 200)
    assert len(long_answer) > PROVIDER_ERROR_MAX_CHARS
    assert classify_provider_error("codex", long_answer) is None


def test_only_the_first_non_empty_line_can_match():
    assert classify_provider_error("codex", "\n\n" + _REFUSALS[0]) is not None
    assert classify_provider_error("codex", "ok\n" + _REFUSALS[0]) is None


def test_a_provider_scoped_row_is_only_eligible_for_its_provider():
    """``providers=`` is reachable, so a row can be narrowed to one adapter."""
    scoped = ProviderErrorSignature("scoped", re.compile(r"NOPE"), ("one",))
    assert scoped.applies_to("one") is True
    assert scoped.applies_to("two") is False
