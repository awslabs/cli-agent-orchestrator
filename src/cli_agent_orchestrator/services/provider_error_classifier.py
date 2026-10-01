"""In-band provider-error classification (issue #638).

A provider that cannot load a model answers the *transport* perfectly: the CLI
prints the refusal, exits cleanly, and the terminal reaches COMPLETED. The refusal
text then sits exactly where the model's answer belongs, so a step whose "output"
is a provider error reads as a successful step and a user-facing deliverable gets
garbage. Nothing in the step's own self-report distinguishes "the model answered"
from "the model refused to load", so the RUNTIME classifies it: CAO launched the
CLI and knows its error signatures.

DELIBERATELY NARROW (issue #638, criterion 4). A MISSED classification degrades to
today's behaviour; a false one would fail a step that really answered. So three
independent guards: only the FIRST non-empty line is tested; any output longer than
:data:`PROVIDER_ERROR_MAX_CHARS` is out of scope; and only a fixed table of chrome
the providers actually print can match. The runtime-side companion of
:meth:`BaseProvider.get_error_message`, which asks the same question of a provider
*instance* holding a live terminal buffer.
"""

from __future__ import annotations

import re
from typing import NamedTuple, Optional, Tuple

# Only outputs at most this long are candidates: two orders of magnitude above a
# refusal banner, far below a real answer.
PROVIDER_ERROR_MAX_CHARS = 512

# Lands in ``StepExecutionError.kind`` and the durable ``workflow_run_step.error_kind``
# column, so an operator can tell a provider refusal from a timeout or a crash.
KIND_PROVIDER_ERROR = "provider_error"


class ProviderErrorSignature(NamedTuple):
    """One start-anchored provider-error signature.

    ``slug`` names the refusal *family* for diagnostics only; it is NOT what travels
    as ``kind``, because the recovery decision (retry, never replay) is identical for
    every family. ``providers`` (``None`` = any adapter) is the escape hatch for a
    shape that is a refusal under exactly ONE adapter, so the table stays
    per-adapter-capable without duplicating the common rows.
    """

    slug: str
    pattern: "re.Pattern[str]"
    providers: Optional[Tuple[str, ...]] = None

    def applies_to(self, provider: str) -> bool:
        """Whether this signature is eligible for ``provider``."""
        return self.providers is None or provider in self.providers


# The ordered table; first match wins. Each row is (slug, start-anchored pattern,
# adapters-or-None), applied with ``re.match`` against the first non-empty line.
_ROWS = (
    # Require the provider chrome's colon; short prose such as "API Error
    # handling should preserve context." is an answer, not a refusal.
    ("api_error", r"API ?Error(?:\s*\([^)\n]{1,80}\))?\s*:", None),
    # Model rejection, with or without the leading HTTP status code.
    ("model_not_available", r"(?:\d{3}\s+)?Invocation of model ID\b", None),
    (
        "model_not_available",
        r"(?:Unknown|Unsupported|Invalid|Undefined) model\b\s*[:'\"`]",
        None,
    ),
    (
        "model_not_available",
        r"Model\b.{0,80}?\b(?:isn't supported|is not supported|not found|unavailable)",
        None,
    ),
    # Credential / quota refusals — the provider started but cannot reach a model.
    (
        "auth_or_quota",
        r"(?:Authentication failed|Not authenticated|Sign in required|Invalid API key|"
        r"invalid_api_key|insufficient_quota|quota exceeded)",
        None,
    ),
    # Throttling: the upstream refused the call, so the step produced no answer.
    (
        "rate_limited",
        r"(?:Rate limit exceeded\b|rate_limit\b|429\s*:\s*rate limit\b|429\s+Too Many Requests\b)",
        None,
    ),
    # Transport failures the provider reports IN BAND, as assistant text.
    ("connection_error", r"(?:ConnectionError|APIConnectionError):", None),
)

_SIGNATURES: Tuple[ProviderErrorSignature, ...] = tuple(
    ProviderErrorSignature(slug, re.compile(pattern, re.I), providers)
    for slug, pattern, providers in _ROWS
)


class ProviderErrorMatch(NamedTuple):
    """The verdict: which family matched, and the bounded raw detail."""

    provider: str
    slug: str
    line: str
    detail: str
    kind: str = KIND_PROVIDER_ERROR


def classify_provider_error(provider: str, output: Optional[str]) -> Optional[ProviderErrorMatch]:
    """Classify ``output`` as an in-band provider error, or return ``None``.

    ``provider`` selects eligible signatures only — it never decides whether the
    output is a refusal at all. ``None`` is returned when the output is empty,
    longer than :data:`PROVIDER_ERROR_MAX_CHARS`, or has a first non-empty line
    matching no signature. The RAW text is never rewritten or truncated here.
    """
    if output is None or len(output) > PROVIDER_ERROR_MAX_CHARS:
        return None

    first_line = next((line.strip() for line in output.splitlines() if line.strip()), "")
    if not first_line:
        return None

    for signature in _SIGNATURES:
        if signature.applies_to(provider) and signature.pattern.match(first_line):
            return ProviderErrorMatch(
                provider=provider, slug=signature.slug, line=first_line, detail=output
            )
    return None


__all__ = [
    "KIND_PROVIDER_ERROR",
    "PROVIDER_ERROR_MAX_CHARS",
    "ProviderErrorMatch",
    "ProviderErrorSignature",
    "classify_provider_error",
]
