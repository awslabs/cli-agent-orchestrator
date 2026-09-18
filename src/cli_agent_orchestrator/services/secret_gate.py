"""Credential pattern gate for memory writes and archive export.

Pure module — no I/O, no logging, no state. ``scan_for_secrets`` matches
the supplied content against a fixed, ordered list of named regexes and
returns the NAME of the first matching pattern (or ``None`` if clean).
``redact_secrets`` replaces every match of every pattern with a
``[REDACTED:<name>]`` marker for the export ``--redact`` path (#345, D5).

``scan_for_secrets`` is used ONLY to reject credentials on
``scope="federated"`` writes — the machine-wide shared tier. This is a
heuristic deny-list, not entropy scoring; it errs toward catching common
credential shapes.
"""

import re
from typing import Any, List, Optional, Pattern, Tuple

# Ordered (name, compiled_regex) pairs. First match wins, so ordering is
# stable and reproducible across calls. No entropy scoring. Vendor-specific
# shapes come before the generic assignment patterns so the returned name is
# the most specific one that applies.
_SECRET_PATTERNS: List[Tuple[str, Pattern[str]]] = [
    # AWS access key IDs — long-lived (AKIA) and temporary/STS (ASIA).
    ("aws_access_key", re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}")),
    # AWS secret access keys: 40 base64 chars with an ``aws ... secret|access``
    # context nearby (the gitleaks shape) or a ``SecretAccessKey`` key as in
    # STS/IAM JSON responses. A bare 40-char match would also hit every 40-hex
    # git SHA, so context is required, and the run must mix upper and lower
    # case so a 40-digit id or a lowercase hex digest near the word "aws" does
    # not fire.
    (
        "aws_secret_access_key",
        re.compile(
            r"(?i)(?:aws.{0,20}(?:secret|access).{0,20}['\"=:\s]"
            r"|secret_?access_?key['\"]?\s*[:=]\s*['\"]?)"
            # (?-i:...) turns case-folding back off: the mixed-case lookaheads
            # are meaningless under the leading (?i).
            r"(?-i:(?=[A-Za-z0-9/+]{0,39}[a-z])(?=[A-Za-z0-9/+]{0,39}[A-Z])"
            r"[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+]))"
        ),
    ),
    # PEM-encoded private keys (RSA / EC / OPENSSH / generic).
    (
        "pem_private_key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)?PRIVATE KEY-----"),
    ),
    # Anthropic API keys.
    ("anthropic_api_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    # OpenAI keys: project / service-account / admin prefixes, and the legacy
    # form whose middle carries the fixed ``T3BlbkFJ`` marker.
    (
        "openai_api_key",
        re.compile(
            r"sk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,}"
            r"|sk-[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20}"
        ),
    ),
    # GitHub fine-grained personal access tokens (fixed 22 + 59 segment shape).
    ("github_fine_grained_pat", re.compile(r"github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}")),
    # GitHub classic PATs (ghp_), OAuth (gho_), user-to-server (ghu_),
    # server-to-server (ghs_) and refresh (ghr_) tokens.
    ("github_pat", re.compile(r"gh[posur]_[A-Za-z0-9]{36,}")),
    # GitLab personal access tokens.
    ("gitlab_pat", re.compile(r"glpat-[A-Za-z0-9_-]{20,}")),
    # Slack bot / user / app-level / refresh / session tokens. Every issued
    # form has a numeric segment right after the prefix (``xoxb-1234...-``,
    # ``xoxe-1-...``), which keeps documentation paths like
    # ``/xoxb-example-token`` out.
    ("slack_token", re.compile(r"xox[abeprs]-[0-9]+-[A-Za-z0-9-]{10,}")),
    # JSON Web Tokens: header and payload are base64url JSON objects, so both
    # begin with ``eyJ``; the three-part dotted shape keeps prose out.
    (
        "jwt",
        re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ),
    # Bearer / api-key / token assignments with a long value. The separator
    # may be ':'/'=' OR whitespace, so the canonical HTTP header form
    # 'Authorization: Bearer <token>' (Bearer followed by a space) is caught.
    (
        "bearer_token",
        re.compile(r"(?i)(?:bearer|api[_-]?key|token)[\s:=]+\S{16,}"),
    ),
    # Generic secret/password assignments.
    (
        "secret_assignment",
        re.compile(r"(?i)(?:password|passwd|secret|pwd)\s*[:=]\s*\S{6,}"),
    ),
]

# Zero-width code points (space, non-joiner, joiner, word joiner, BOM) render
# as nothing, so ``AK\u200bIA...`` reads as a key to a human and to the
# consumer that eventually uses it, while defeating every prefix above.
# ``scan_for_secrets`` judges the content with them removed. ``redact_secrets``
# removes them only from inside spans that match once they are gone, so a
# U+200D joining an emoji sequence elsewhere in the same string survives.
_ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\u2060\ufeff]")


def _strip_zero_width(content: str) -> str:
    return _ZERO_WIDTH.sub("", content)


def _strip_zero_width_inside_matches(content: str) -> str:
    """Drop zero-width characters that sit inside a credential, and only those.

    Patterns are run over a zero-width-free copy; every match span is mapped
    back to the original string and the zero-width characters within that
    span are removed. Zero-width characters outside every span are kept.
    """
    kept: List[str] = []
    index_map: List[int] = []
    for i, ch in enumerate(content):
        if not _ZERO_WIDTH.match(ch):
            kept.append(ch)
            index_map.append(i)
    stripped = "".join(kept)
    drop = set()
    for _name, pattern in _SECRET_PATTERNS:
        for m in pattern.finditer(stripped):
            if m.end() <= m.start():
                continue
            lo, hi = index_map[m.start()], index_map[m.end() - 1]
            drop.update(j for j in range(lo, hi + 1) if _ZERO_WIDTH.match(content[j]))
    if not drop:
        return content
    return "".join(ch for j, ch in enumerate(content) if j not in drop)


def scan_for_secrets(content: str) -> Optional[str]:
    """Return the NAME of the first credential pattern that matches.

    Returns ``None`` when no pattern matches. The caller must not echo the
    matched bytes — only the returned pattern name is safe to log.
    """
    if not content:
        return None
    content = _strip_zero_width(content)
    for name, pattern in _SECRET_PATTERNS:
        if pattern.search(content):
            return name
    return None


def redact_secrets(content: str) -> Tuple[str, List[str]]:
    """Replace every match of every pattern with ``[REDACTED:<name>]``.

    Returns ``(redacted_text, fired)`` where ``fired`` is the list of
    pattern names that matched at least once, in ``_SECRET_PATTERNS``
    order, deduplicated. The caller must not echo the original matched
    bytes — only the redacted text and pattern names are safe to emit.

    Redaction cascades: patterns run in sequence over the already-redacted
    text, so a later pattern may re-match an earlier ``[REDACTED:<name>]``
    marker and ``fired`` can include a pattern that only matched a marker.
    This is fail-safe — it never leaks original bytes.

    Zero-width characters inside a credential are removed so the pattern can
    match the whole token; zero-width characters anywhere else are kept.
    """
    if not content:
        return content, []
    if _ZERO_WIDTH.search(content):
        # A zero-width character may be hiding a credential from the patterns;
        # remove the ones inside any such credential so the loop below sees,
        # and redacts, the whole token. Zero-width characters elsewhere stay.
        content = _strip_zero_width_inside_matches(content)
    fired: List[str] = []
    for name, pattern in _SECRET_PATTERNS:
        content, count = pattern.subn(f"[REDACTED:{name}]", content)
        if count:
            fired.append(name)
    return content, fired


def redact_json_leaves(node: Any) -> Any:
    """Recursively :func:`redact_secrets` every string inside a parsed JSON document.

    Dict KEYS are redacted alongside values. A credential is as capable of landing in
    a key as in a value, and no unredacted credential may be persisted; the accepted
    cost is that two keys differing only inside a redacted span collapse into one,
    which loses a member but cannot produce an invalid document. Non-string scalars
    pass through untouched — there is nothing in an ``int`` for a pattern to match.

    PROMOTED HERE from ``script_runner._redact_json_leaves`` by issue #583 Bolt 2, unit
    ``manifest-envelope``, so that BOTH the step-output path and the execution-manifest
    envelope share ONE definition. Two copies of this function would drift, and the
    drift would be silent and security-relevant in the worst direction: one path would
    keep persisting a credential class the other had already learned to catch.

    THIS MODULE IS THE RIGHT HOME because it is a LEAF — it imports only ``re`` and
    ``typing``. ``services/execution_manifest.py`` can therefore depend on it and remain
    a leaf itself, which importing from ``script_runner`` (a heavyweight module) would
    have prevented by inverting the layering that ``step_result.py`` and
    ``step_fingerprint.py`` established.

    OPERATE ON THE PARSED TREE, NEVER ON THE SERIALISED STRING. Redacting a JSON string
    would replace text inside string literals and could span a quote or an escape
    sequence, producing an unparseable document — written successfully and failing on
    every read. Walking parsed values and re-serialising afterwards makes output
    validity hold by construction.
    """
    if isinstance(node, str):
        redacted, _fired = redact_secrets(node)
        return redacted
    if isinstance(node, dict):
        return {redact_json_leaves(k): redact_json_leaves(v) for k, v in node.items()}
    if isinstance(node, list):
        return [redact_json_leaves(v) for v in node]
    return node
