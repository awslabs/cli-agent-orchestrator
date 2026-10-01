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
# Each case names the adapter that actually emits the shape; the classifier must
# not treat common vocabulary in another adapter's answer as provider chrome.
_REFUSALS = (
    (
        "codex",
        "API Error (openai.gpt-5.6-terra): 400 Invocation of model ID "
        "openai.gpt-5.6-terra isn't supported.",
    ),
    ("codex", "400 Invocation of model ID gpt-5.6-terra isn't supported."),
    ("codex", "API Error: 401 invalid_api_key"),
    ("grok_cli", "Authentication failed: no credentials configured"),
    ("codex", "Rate limit exceeded, retry after 30s"),
    ("codex", "429: rate limit exceeded"),
    ("kimi_cli", "ConnectionError: upstream refused the connection"),
    ("codex", "Unknown model: gpt-5.6-terra"),
    ("codex", "Invalid model: gpt-5.6-terra"),
    ("codex", "Unsupported model: gpt-5.6-terra"),
    ("codex", "Model gpt-5.6-terra is not supported by this account"),
)

_NON_REFUSALS = (
    # A legitimate ANSWER quoting an error signature — not the FIRST line, so the
    # anchoring guard rejects it before the signature table runs.
    ("codex", "Here is the handler you asked for:\n\nAPI Error: 401 invalid_api_key"),
    # ... and ordinary prose about errors, without the provider chrome.
    ("codex", "API Error handling should preserve context."),
    ("codex", "API Error: none found - all 42 endpoints return 2xx."),
    ("codex", "429"),
    ("claude_code", "Rate limiting protects APIs from burst traffic."),
    ("codex", "Unknown model types use the fallback serializer."),
    ("claude_code", "Rate limit exceeded."),
    ("claude_code", "Quota exceeded."),
    ("codex", "rate_limit = 100"),
    ("codex", "Unknown model 'placeholder' is a useful teaching example."),
    ("claude_code", "Authentication failed."),
    ("claude_code", "Authentication failed is an expected unit-test outcome."),
    ("claude_code", "Invalid API key is the message users see."),
    ("codex", "Model weights not found."),
    ("codex", "Model validation is not supported."),
    ("codex", "Model gpt-5 is not supported because this sentence is fictional."),
    ("claude_code", "Unknown model 'gpt-4o-mini'"),
    ("codex", "The function returns None when the request fails."),
    (None, ""),
    (None, None),
)


@pytest.mark.parametrize(("provider", "output"), _REFUSALS)
def test_known_provider_refusals_are_classified(provider, output):
    match = classify_provider_error(provider, output)
    assert match is not None
    assert match.kind == KIND_PROVIDER_ERROR
    assert match.line  # the raw matched line travels for the failure message


@pytest.mark.parametrize(("provider", "output"), _NON_REFUSALS)
def test_non_refusals_are_never_classified(provider, output):
    assert classify_provider_error(provider or "codex", output) is None


def test_long_output_is_out_of_scope_however_it_begins():
    """Acceptance criterion 4: the length bound is a guard in its own right."""
    long_answer = "API Error: " + ("detail " * 200)
    assert len(long_answer) > PROVIDER_ERROR_MAX_CHARS
    assert classify_provider_error("codex", long_answer) is None


def test_only_the_first_non_empty_line_can_match():
    provider, refusal = _REFUSALS[0]
    assert classify_provider_error(provider, "\n\n" + refusal) is not None
    assert classify_provider_error(provider, "ok\n" + refusal) is None


def test_full_bounded_output_travels_as_detail():
    output = "API Error: 401 invalid_api_key\nsecond line keeps provider detail"
    match = classify_provider_error("codex", output)
    assert match is not None
    assert match.line == "API Error: 401 invalid_api_key"
    assert match.detail == output


def test_a_provider_scoped_row_is_only_eligible_for_its_provider():
    """``providers=`` is reachable, so a row can be narrowed to one adapter."""
    scoped = ProviderErrorSignature("scoped", re.compile(r"NOPE"), ("one",))
    assert scoped.applies_to("one") is True
    assert scoped.applies_to("two") is False
