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
from typing import List, Optional, Pattern, Tuple

# Ordered (name, compiled_regex) pairs. First match wins, so ordering is
# stable and reproducible across calls. No entropy scoring.
_SECRET_PATTERNS: List[Tuple[str, Pattern[str]]] = [
    # AWS access key IDs — long-lived (AKIA) and temporary/STS (ASIA).
    ("aws_access_key", re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}")),
    # PEM-encoded private keys (RSA / EC / OpenSSH / generic).
    (
        "pem_private_key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)?PRIVATE KEY-----"),
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
    # GitHub personal access tokens (ghp_ / ghs_ ...).
    ("github_pat", re.compile(r"gh[ps]_[A-Za-z0-9]{36,}")),
    # GitLab personal access tokens.
    ("gitlab_pat", re.compile(r"glpat-[A-Za-z0-9_-]{20,}")),
]


def scan_for_secrets(content: str) -> Optional[str]:
    """Return the NAME of the first credential pattern that matches.

    Returns ``None`` when no pattern matches. The caller must not echo the
    matched bytes — only the returned pattern name is safe to log.
    """
    if not content:
        return None
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
    """
    if not content:
        return content, []
    fired: List[str] = []
    for name, pattern in _SECRET_PATTERNS:
        content, count = pattern.subn(f"[REDACTED:{name}]", content)
        if count:
            fired.append(name)
    return content, fired
