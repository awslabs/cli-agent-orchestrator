"""In-band provider-error classification (issue #638).

A provider that cannot load a model answers the *transport* perfectly: the CLI
prints the refusal, exits cleanly, and the terminal reaches COMPLETED. The refusal
text then sits exactly where the model's answer belongs, so a step whose "output"
is a provider error reads as a successful step and a user-facing deliverable gets
garbage. Nothing in the step's own self-report distinguishes "the model answered"
from "the model refused to load", so the RUNTIME classifies it: CAO launched the
CLI and knows its error signatures.

DELIBERATELY NARROW (issue #638, criterion 4). A MISSED classification degrades to
today's behaviour; a false one would fail a step that really answered. So five
independent guards: only the FIRST non-empty line is tested; any output longer than
:data:`PROVIDER_ERROR_MAX_CHARS` is out of scope; each row applies only to the
provider adapters that emit that chrome; the pattern must consume the WHOLE line;
and raw adapter context, when available, must show provider chrome rather than an
assistant-marker-owned rendering. Common words, status-code values, or error
vocabulary in another adapter's ordinary prose are therefore answers, not refusals.
The runtime-side companion of
:meth:`BaseProvider.get_error_message`, which asks the same question of a provider
*instance* holding a live terminal buffer.
"""

from __future__ import annotations

import re
from typing import Any, NamedTuple, Optional, Tuple

from cli_agent_orchestrator.providers.claude_code import EXTRACTION_RESPONSE_PATTERN
from cli_agent_orchestrator.providers.codex import ASSISTANT_PREFIX_PATTERN
from cli_agent_orchestrator.providers.kimi_cli import KIMI_RESPONSE_MARKER_RE
from cli_agent_orchestrator.providers.minimax_code import ASSISTANT_MARKER_PATTERN
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

# Only outputs at most this long are candidates: two orders of magnitude above a
# refusal banner, far below a real answer.
PROVIDER_ERROR_MAX_CHARS = 512

# Lands in ``StepExecutionError.kind`` and the durable ``workflow_run_step.error_kind``
# column, so an operator can tell a provider refusal from a timeout or a crash.
KIND_PROVIDER_ERROR = "provider_error"


class ProviderErrorSignature(NamedTuple):
    """One full-line, provider-scoped error signature.

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


# The ordered table; first match wins. Each row is (slug, full-line pattern,
# adapters-or-None), applied with ``re.fullmatch`` against the first non-empty
# line. The full-line requirement is the false-positive guard: matching a common
# prefix inside arbitrary assistant prose is not provider-owned evidence.
_ROWS = (
    # Codex/Claude transport chrome. Require a structured HTTP status after the
    # colon; an ordinary answer opening with "API Error:" is not provider refusal.
    (
        "api_error",
        r"API ?Error(?:\s*\([^)\n]{1,80}\))?\s*:\s*[45]\d{2}\b.*",
        ("claude_code", "codex"),
    ),
    # Model rejection, with or without the leading HTTP status code.
    (
        "model_not_available",
        r"(?:\d{3}\s+)?Invocation of model ID\s+.+?\s+"
        r"(?:isn't supported|is not supported|not found|unavailable|is invalid)\s*[.!]?",
        ("codex",),
    ),
    # Colon/equals forms are provider chrome. Quoted names are deliberately NOT
    # enough: "Unknown model 'x'" is also a plausible one-line answer.
    (
        "model_not_available",
        r"(?:Unknown|Unsupported|Invalid|Undefined)\s+model\s*[:=]\s*\S.*",
        ("claude_code", "codex", "grok_cli"),
    ),
    (
        "model_not_available",
        r"Model\s+(?=[A-Za-z0-9._/-]*[0-9._-])[A-Za-z0-9][A-Za-z0-9._/-]*"
        r"\s+(?:isn't supported|is not supported|not found|unavailable)"
        r"(?:\s+by this account)?\s*[.!]?",
        ("codex",),
    ),
    # Credential / quota refusals from adapters whose documented status detector
    # owns these strings. Require a colon/equals detail; a terminal period alone is
    # indistinguishable from ordinary prose.
    (
        "auth_or_quota",
        r"(?:Authentication failed|Not authenticated|Sign in required|Invalid API key|"
        r"invalid_api_key|insufficient_quota|quota exceeded)"
        r"\s*[:=]\s*\S.+",
        ("grok_cli", "mcode"),
    ),
    # Throttling. The status/diagnostic forms carry enough structure to be chrome;
    # bare "Rate limit exceeded." or "rate_limit = 100" do not.
    (
        "rate_limited",
        r"(?:Rate limit exceeded\s*[,.:]\s*(?:retry|try|please)\b.*|"
        r"rate_limit(?:_error)?\s*[:=]\s*(?:rate limit|too many|exceeded|retry|try)\b.*|"
        r"429\s*:\s*rate limit(?:ing|ed)?(?:\s+exceeded)?(?:\s*[,.:].*)?|"
        r"429\s+Too Many Requests)",
        ("claude_code", "codex"),
    ),
    # Kimi reports transport failures as column-zero terminal chrome.
    ("connection_error", r"(?:ConnectionError|APIConnectionError):\s*\S.*", ("kimi_cli",)),
)

_SIGNATURES: Tuple[ProviderErrorSignature, ...] = tuple(
    ProviderErrorSignature(slug, re.compile(pattern, re.I), providers)
    for slug, pattern, providers in _ROWS
)

# A rendered assistant message owns its text. These are the same adapter-owned
# patterns used to extract that message, not a second private copy that can drift:
# the same first line is a provider refusal when it appears as bare chrome and is
# an ordinary answer when it follows the adapter's response marker.
_ASSISTANT_MARKERS = {
    "claude_code": EXTRACTION_RESPONSE_PATTERN,
    "codex": re.compile(ASSISTANT_PREFIX_PATTERN, re.I),
    "kimi_cli": KIMI_RESPONSE_MARKER_RE,
    "mcode": ASSISTANT_MARKER_PATTERN,
}
_CONTEXT_UNSET: Any = object()


class ProviderErrorMatch(NamedTuple):
    """The verdict: which family matched, and the bounded raw detail."""

    provider: str
    slug: str
    line: str
    detail: str
    kind: str = KIND_PROVIDER_ERROR


def _provider_owns_error_line(provider: str, error_line: str, script_output: str) -> bool:
    """Whether the last matching rendered line is provider chrome, not assistant text.

    The final-message extractor intentionally removes the provider's response marker,
    so the extracted text alone cannot establish ownership.  Walk the raw script
    capture and let the LAST occurrence decide: an unmarked occurrence is provider
    chrome, while one owned by the adapter's assistant marker is an answer.  No
    marker vocabulary for a provider means no positive ownership evidence, so the
    caller must not classify it.
    """
    marker = _ASSISTANT_MARKERS.get(provider)
    if marker is None:
        # No marker vocabulary means no positive ownership evidence. The caller
        # must degrade to the pre-#638 behaviour rather than trust the text alone.
        return False

    found = False
    assistant_owned = False
    for raw_line in strip_terminal_escapes(script_output).splitlines():
        line = raw_line.strip()
        match = marker.match(line)
        if match is not None:
            if line[match.end() :].strip() == error_line:
                found = True
                assistant_owned = True
            continue
        if line == error_line:
            found = True
            assistant_owned = False
    return found and not assistant_owned


def classify_provider_error(
    provider: str,
    output: Optional[str],
    *,
    script_output: Any = _CONTEXT_UNSET,
) -> Optional[ProviderErrorMatch]:
    """Classify ``output`` as an in-band provider error, or return ``None``.

    ``provider`` scopes every signature to adapters that actually emit that chrome.
    ``None`` is returned when the output is empty, longer than
    :data:`PROVIDER_ERROR_MAX_CHARS`, or has a first non-empty line matching no
    eligible signature. Each signature consumes the whole first non-empty line.

    ``script_output`` is the raw adapter capture when production has it.  Supplying
    it activates the ownership check above; omitting it preserves the historical
    two-argument API for callers that already hold independently-trusted chrome.
    The RAW text is never rewritten or truncated here.
    """
    if output is None or len(output) > PROVIDER_ERROR_MAX_CHARS:
        return None

    first_line = next((line.strip() for line in output.splitlines() if line.strip()), "")
    if not first_line:
        return None

    for signature in _SIGNATURES:
        if not signature.applies_to(provider) or not signature.pattern.fullmatch(first_line):
            continue
        if script_output is not _CONTEXT_UNSET and not _provider_owns_error_line(
            provider, first_line, script_output or ""
        ):
            return None
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
