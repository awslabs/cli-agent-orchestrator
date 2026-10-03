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
    # Provider-native chrome captured from real CLI sessions (see the fixtures below):
    # Codex renders upstream refusals on its own `■` / `⚠️ stream error` chrome.
    ("codex", '■ unexpected status 400 Bad Request: {"code":20015}'),
    ("codex", '⚠️ stream error: unexpected status 400 Bad Request: {"code":20015}'),
)

_NON_REFUSALS = (
    # A legitimate ANSWER quoting an error signature — not the FIRST line, so the
    # anchoring guard rejects it before the signature table runs.
    ("codex", "Here is the handler you asked for:\n\nAPI Error: 401 invalid_api_key"),
    # ... and ordinary prose about errors, without the provider chrome.
    ("codex", "API Error handling should preserve context."),
    ("codex", "API Error: none found - all 42 endpoints return 2xx."),
    ("codex", "API Error: 200 endpoints were audited; none failed."),
    ("codex", "API Error: 123 is not an HTTP status class."),
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
    # The `unexpected status` signature is provider-native chrome, not vocabulary:
    # without the `■`/`⚠️` bullet it is not provider-owned evidence.
    ("codex", "unexpected status 400 Bad Request: bad model"),
    # A leading assistant marker is not the error chrome (Codex never prefixes one).
    ("codex", "• ■ unexpected status 400 Bad Request: bad model"),
    # Non-error statuses are not refusals even under the real chrome.
    ("codex", "■ unexpected status 200 OK"),
    ("kiro_cli", "API Error: 401 invalid_api_key"),
    ("q_cli", "API Error: 401 invalid_api_key"),
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


def test_provider_scope_is_enforced_through_classifier():
    """The production entry point, not only ``applies_to``, enforces the scope."""
    output = "API Error: 401 invalid_api_key"
    assert classify_provider_error("codex", output) is not None
    assert classify_provider_error("kiro_cli", output) is None
    assert classify_provider_error("q_cli", output) is None


def test_markerless_provider_context_is_conservative():
    """A provider whose answers are unmarked cannot establish provider ownership.

    The two-argument API still classifies a trusted signature; the production
    three-argument path must degrade to the pre-fix behaviour because an answer
    and provider chrome can be textually identical for grok_cli.
    """
    model_error = "Unknown model: a model type absent from the serializer registry."
    auth_error = "Authentication failed: no credentials configured"

    assert classify_provider_error("grok_cli", model_error) is not None
    assert (
        classify_provider_error("grok_cli", model_error, script_output=f"{model_error}\n>") is None
    )
    assert classify_provider_error("grok_cli", auth_error, script_output=f"{auth_error}\n>") is None


def test_marker_aware_three_arg_path_distinguishes_provider_chrome():
    """mcode and kimi now contribute their real response markers to ownership."""
    mcode_error = "Authentication failed: no credentials configured"
    assert (
        classify_provider_error(
            "mcode", mcode_error, script_output=f"› task\n{mcode_error}\n└ Completed in 1s"
        )
        is not None
    )
    assert (
        classify_provider_error(
            "mcode", mcode_error, script_output=f"› task\n● {mcode_error}\n└ Completed in 1s"
        )
        is None
    )

    kimi_error = "ConnectionError: upstream refused the connection"
    assert (
        classify_provider_error("kimi_cli", kimi_error, script_output=f"{kimi_error}\n💫")
        is not None
    )
    assert (
        classify_provider_error("kimi_cli", kimi_error, script_output=f"• {kimi_error}\n💫") is None
    )


def test_context_distinguishes_provider_error_from_assistant_prose():
    """Ownership is decided from the provider's OWN error signal, not marker absence.

    Codex renders an upstream refusal unmarked (on its ``■`` / ``⚠️ stream error``
    chrome), so the same text on the ``•`` assistant marker is an answer.  Claude Code
    instead renders API errors on the same ``⏺``/``●`` bullet it uses for answers, so
    there the ``API Error:`` chrome itself is the signal — a marked occurrence is a
    refusal, not an answer."""
    refusal = "API Error: 404 is the response for an unknown route."

    codex_provider = f"› inspect routes\n{refusal}\n›"
    codex_answer = f"› inspect routes\n• {refusal}\n›"
    assert classify_provider_error("codex", refusal, script_output=codex_provider) is not None
    assert classify_provider_error("codex", refusal, script_output=codex_answer) is None

    # Real Claude Code renders the API error on the response bullet it also uses for
    # answers (anthropics/claude-code#91345 / #92316), so the marker cannot veto the
    # classification; the narrow ``API Error:`` signature is what keeps this narrow.
    claude_provider = f"⏺ {refusal}\n❯"
    assert (
        classify_provider_error("claude_code", refusal, script_output=claude_provider) is not None
    )


def test_context_uses_the_latest_turn_not_stale_error_history():
    """Multi-turn stability: the old turn's refusal must not taint a later answer,
    and a later provider failure must still be classified after an earlier answer."""
    refusal = "API Error: 404 is the response for an unknown route."

    later_answer = f"› first\n{refusal}\n› retry\n• {refusal}\n›"
    assert classify_provider_error("codex", refusal, script_output=later_answer) is None

    later_failure = f"› first\n• {refusal}\n› retry\n{refusal}\n›"
    assert classify_provider_error("codex", refusal, script_output=later_failure) is not None


def test_real_adapters_extract_and_preserve_ownership():
    """Cross the real extraction boundary the review reproduced, including two turns."""
    from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
    from cli_agent_orchestrator.providers.codex import CodexProvider

    refusal = "API Error: 404 is the response for an unknown route."
    codex = CodexProvider("terminal", "session", "window")
    claude = ClaudeCodeProvider("terminal", "session", "window")

    provider_raw = f"› inspect routes\n{refusal}\n› "
    extracted_provider_error = codex.extract_last_message_from_script(provider_raw)
    assert extracted_provider_error == refusal
    assert (
        classify_provider_error("codex", extracted_provider_error, script_output=provider_raw)
        is not None
    )

    codex_multi_turn = f"› first\n{refusal}\n› retry\n• {refusal}\n› "
    extracted_codex_answer = codex.extract_last_message_from_script(codex_multi_turn)
    assert extracted_codex_answer == f"• {refusal}"
    assert (
        classify_provider_error("codex", extracted_codex_answer, script_output=codex_multi_turn)
        is None
    )

    # Claude Code renders provider errors on the same response bullet as answers
    # (anthropics/claude-code#91345 / #92316), so the extractor returns the raw
    # ``API Error:`` text and it must still classify.
    claude_multi_turn = f"⏺ first answer\n❯ \n⏺ {refusal}\n❯ "
    extracted_claude_error = claude.extract_last_message_from_script(claude_multi_turn)
    assert extracted_claude_error == refusal
    assert (
        classify_provider_error(
            "claude_code", extracted_claude_error, script_output=claude_multi_turn
        )
        is not None
    )


# Verbatim terminal renders captured from real CLI sessions.  The review asked for
# fixtures captured from a live session of each CLI rather than hand-rolled output;
# these are the exact renderings reported upstream, i.e. the shapes CAO's own
# adapters extract from, so they pin the classifier against the real chrome:
#   - Claude Code prints API errors on its ⏺/● response bullet
#     (anthropics/claude-code#91345, anthropics/claude-code#92316);
#   - Codex prints upstream refusals on its own ■ / ⚠️ stream error chrome
#     (openai/codex#6933, openai/codex#4270).
_REAL_CLAUDE_API_ERROR_91345 = (
    "⏺ API Error: 400 Claude Code 2.1.236 does not support this model; version 2.1.251 or\n"
    "  newer is required. Run 'claude update', or update the Claude desktop app, then\n"
    "  try again.\n"
    "❯ "
)
_REAL_CLAUDE_API_ERROR_92316 = (
    "● API Error: 400 invalid params, messages.4.content.1.tool_use.input: "
    "Input should be a valid dictionary (2013)\n"
    "❯ "
)
_REAL_CODEX_UNEXPECTED_STATUS_6933 = (
    "› inspect routes\n"
    "■ unexpected status 400 Bad Request: {\n"
    '  "error": {\n'
    '    "message": "Missing required parameter: \'input[10].id\'.",\n'
    '    "type": "invalid_request_error"\n'
    "  }\n"
    "}\n"
    "› "
)
_REAL_CODEX_STREAM_ERROR_4270 = (
    "› inspect routes\n"
    "⚠️ stream error: unexpected status 400 Bad Request: "
    '{"code":20015,"message":"\\"messages\\" in request are illegal.","data":null}; '
    "retrying 1/5 in 196ms…\n"
    "› "
)


@pytest.mark.parametrize(
    ("provider", "raw_capture", "expected_line_start"),
    (
        ("claude_code", _REAL_CLAUDE_API_ERROR_91345, "API Error: 400 Claude Code"),
        ("claude_code", _REAL_CLAUDE_API_ERROR_92316, "API Error: 400 invalid params"),
        ("codex", _REAL_CODEX_UNEXPECTED_STATUS_6933, "■ unexpected status 400"),
        ("codex", _REAL_CODEX_STREAM_ERROR_4270, "⚠️ stream error: unexpected status 400"),
    ),
)
def test_real_cli_captures_are_classified_through_the_adapters(
    provider, raw_capture, expected_line_start
):
    """Cross the REAL extractor boundary with captures of the real CLI chrome.

    This is the regression the review reproduced: the refusal text sits where the
    model's answer belongs, so an ownership guard keyed on marker ABSENCE misses the
    real renderings.  Claude Code marks the error on its response bullet; Codex uses
    its own ``■`` / ``⚠️ stream error`` chrome.  Both must classify.
    """
    from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
    from cli_agent_orchestrator.providers.codex import CodexProvider

    adapter = (
        ClaudeCodeProvider("terminal", "session", "window")
        if provider == "claude_code"
        else CodexProvider("terminal", "session", "window")
    )
    extracted = adapter.extract_last_message_from_script(raw_capture)

    match = classify_provider_error(provider, extracted, script_output=raw_capture)

    assert match is not None
    assert match.slug in ("api_error", "unexpected_status")
    assert match.line.startswith(expected_line_start)


@pytest.mark.parametrize(
    "answer",
    (
        "API Error handling should preserve context.",
        "API Error: none found - all 42 endpoints return 2xx.",
        "API Error: 200 endpoints were audited; none failed.",
        "Unknown model types use the fallback serializer.",
        "Rate limiting protects APIs from burst traffic.",
    ),
)
def test_marked_ordinary_answers_are_not_refusals(answer):
    """Claude Code marks answers and errors the same way, so the marker never proves a
    refusal: an ordinary answer on the response bullet must stay an answer."""
    raw = f"⏺ {answer}\n❯ "
    assert classify_provider_error("claude_code", answer, script_output=raw) is None


def test_codex_stream_error_retry_that_succeeds_is_not_a_refusal():
    """A ``⚠️ stream error`` banner is chrome for a RETRYABLE failure (openai/codex#4270).
    When a later turn answers on the ``•`` marker the step completed, so the transient
    banner must not be mistaken for a terminal refusal."""
    from cli_agent_orchestrator.providers.codex import CodexProvider

    raw = (
        "› do the task\n"
        '⚠️ stream error: unexpected status 400 Bad Request: {"code":20015}; '
        "retrying 1/5 in 196ms…\n"
        "• Here is the finished answer.\n"
        "› "
    )
    extracted = CodexProvider("terminal", "session", "window").extract_last_message_from_script(raw)
    assert extracted == "• Here is the finished answer."
    assert classify_provider_error("codex", extracted, script_output=raw) is None


def test_codex_stream_error_retries_exhausted_is_a_refusal():
    """With no later ``•`` answer the same banner is the terminal refusal."""
    from cli_agent_orchestrator.providers.codex import CodexProvider

    raw = (
        "› do the task\n"
        '⚠️ stream error: unexpected status 400 Bad Request: {"code":20015}; '
        "retrying 5/5 in 3.116s…\n"
        '■ unexpected status 400 Bad Request: {"code":20015}\n'
        "› "
    )
    extracted = CodexProvider("terminal", "session", "window").extract_last_message_from_script(raw)
    assert classify_provider_error("codex", extracted, script_output=raw) is not None
