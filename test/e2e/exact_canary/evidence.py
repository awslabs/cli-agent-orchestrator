"""Sanitation and bounded writing for installed canary evidence."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_BEARER_RE = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/-]{12,}=*")
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
_TEMP_PATH_RE = re.compile(r"(?:/private)?/var/folders/[^\s\"']+|/tmp/[^\s\"']+")
_NAMED_SECRET_RE = re.compile(
    r'(?i)("?(?:access_token|refresh_token|api_key|authorization|launch_nonce)"?'
    r'\s*[:=]\s*"?)([^"\s,}]+)'
)
_NAMED_SECRET_KEYS = frozenset(
    {"access_token", "refresh_token", "api_key", "authorization", "launch_nonce"}
)


class EvidenceSanitizer:
    """Apply known-value and conservative secret-shape redaction recursively."""

    def __init__(self, redactions: Mapping[str, str] | None = None) -> None:
        self._redactions: list[tuple[str, str]] = []
        for source, replacement in (redactions or {}).items():
            self.add(source, replacement)

    def add(self, source: str | None, replacement: str) -> None:
        if source and len(source) >= 3:
            self._redactions.append((source, replacement))
            # macOS exposes the same temporary directory through both the
            # /var and /private/var spellings.  Redact the longer alias first
            # so exact replacement cannot leave a stray "/private" prefix.
            if source.startswith("/private/var/folders/"):
                self._redactions.append((source.removeprefix("/private"), replacement))
            elif source.startswith("/var/folders/"):
                self._redactions.append((f"/private{source}", replacement))

    def sanitize(self, text: str) -> str:
        sanitized = text
        for source, replacement in sorted(self._redactions, key=lambda item: -len(item[0])):
            sanitized = sanitized.replace(source, replacement)
        for replacement in {item[1] for item in self._redactions}:
            private_alias = f"/private{replacement}"
            while private_alias in sanitized:
                sanitized = sanitized.replace(private_alias, replacement)
        sanitized = _EMAIL_RE.sub("<ACCOUNT>", sanitized)
        sanitized = _BEARER_RE.sub(r"\1<SECRET>", sanitized)
        sanitized = _OPENAI_KEY_RE.sub("<SECRET>", sanitized)
        sanitized = _NAMED_SECRET_RE.sub(r"\1<SECRET>", sanitized)
        sanitized = _TEMP_PATH_RE.sub("<TEMP_PATH>", sanitized)
        return sanitized

    def sanitize_json(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.sanitize(value)
        if isinstance(value, list):
            return [self.sanitize_json(item) for item in value]
        if isinstance(value, tuple):
            return [self.sanitize_json(item) for item in value]
        if isinstance(value, Mapping):
            return {
                str(key): (
                    "<SECRET>"
                    if str(key).lower() in _NAMED_SECRET_KEYS
                    else self.sanitize_json(item)
                )
                for key, item in value.items()
            }
        return value

    def write_text(self, path: Path, content: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.sanitize(content), encoding="utf-8")
        return path

    def write_json(self, path: Path, value: Any) -> Path:
        content = json.dumps(self.sanitize_json(value), indent=2, sort_keys=True) + "\n"
        return self.write_text(path, content)
