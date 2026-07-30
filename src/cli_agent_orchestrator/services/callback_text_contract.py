"""Canonical cross-repository conduct-report callback text contract."""

from __future__ import annotations

import re

SCHEMA = "cao-conduct-report-text-v1"
MAX_LENGTH = 900
VALID_STATUSES = frozenset({"done", "blocked", "failed"})

_VALUE_PATTERNS = (
    (
        "pem-private-key",
        re.compile(
            r"(-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----)"
            r"([A-Za-z0-9+/=\s]+?)"
            r"(-----END [A-Z0-9 ]*PRIVATE KEY-----)"
        ),
        2,
    ),
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"), 0),
    ("openai-project-key", re.compile(r"sk-proj-[A-Za-z0-9_\-]{16,}"), 0),
    ("openai-key", re.compile(r"sk-[A-Za-z0-9]{24,}"), 0),
    ("github-pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), 0),
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), 0),
    ("google-key", re.compile(r"AIza[0-9A-Za-z_\-]{35}"), 0),
    (
        "jwt",
        re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
        0,
    ),
    ("zai-key", re.compile(r"\b[0-9a-f]{32}\.[A-Za-z0-9]{10,}\b"), 0),
    (
        "bearer",
        re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._\-]{16,})"),
        1,
    ),
    (
        "cookie",
        re.compile(
            r"(?i)\b(?:session(?:id)?|sid|csrf[_-]?token|auth[_-]?session)="
            r"(?!cao-[A-Za-z0-9_-]+(?![A-Za-z0-9%._-]))"
            r"([A-Za-z0-9%._\-]{10,})"
        ),
        1,
    ),
)
_SENSITIVE_KEY = re.compile(
    r"(?i)(api[_-]?key|auth[_-]?token|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|secret[_-]?key|[_-]?secret|password|passwd|"
    r"private[_-]?key|zai[_-]?api[_-]?key|"
    r"anthropic[_-]?(?:api[_-]?key|auth[_-]?token)|"
    r"openai[_-]?api[_-]?key|codex[_-]?api[_-]?key|"
    r"moonshot[_-]?api[_-]?key)"
)
_KV_ASSIGN = re.compile(
    r"(?P<key>[A-Za-z_][A-Za-z0-9_-]*)\s*(?P<sep>[:=])\s*" r"(?P<q>[\"']?)(?P<val>[^\s,\"']+)(?P=q)"
)


def _redact(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text

    def redact_key(match: re.Match[str]) -> str:
        key = match.group("key")
        if _SENSITIVE_KEY.search(key):
            return f"{key}{match.group('sep')}{match.group('q')}" f"<redacted>{match.group('q')}"
        return match.group(0)

    text = _KV_ASSIGN.sub(redact_key, text)
    for name, pattern, group_index in _VALUE_PATTERNS:

        def replace(
            match: re.Match[str],
            replacement_name: str = name,
            secret_group: int = group_index,
        ) -> str:
            replacement = f"<redacted:{replacement_name}>"
            if secret_group == 0:
                return replacement
            return match.group(0).replace(match.group(secret_group), replacement, 1)

        text = pattern.sub(replace, text)
    return text


def canonical_callback_text(
    *,
    status: str,
    task_id: str,
    report_path: str,
    summary: str,
) -> str:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid report status {status!r}")
    if not report_path or not report_path.strip():
        raise ValueError("report_path is required in the conduct-report line")
    summary_line = canonical_summary_text(summary)
    prefix = f"[conduct-report] status={status} task={task_id} " f"report={report_path} summary="
    return (prefix + summary_line)[:MAX_LENGTH]


def canonical_summary(summary: str) -> str:
    """The first non-empty source line used by both command and digest."""
    first_line = ""
    for line in summary.splitlines():
        if line.strip():
            first_line = line.strip()
            break
    return first_line


def canonical_summary_text(summary: str) -> str:
    return re.sub(r"\s+", " ", _redact(canonical_summary(summary))).strip()
