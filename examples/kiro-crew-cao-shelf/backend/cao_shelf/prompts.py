"""What a waiting terminal is asking, and the answers that are safe to offer as buttons.

Only prompt shapes that were recorded are recognised, and only for the provider they were recorded
on. Unknown text or another provider gives no suggestions, never a guess. v1 knows the ``mock_cli``
approval line; real providers are added from recordings only. Terminal text is shown with bidi,
zero-width and C1 control characters removed (``text.strip_invisible``).
"""

from __future__ import annotations

import re
from typing import Dict, List

from .text import strip_invisible

TAIL_LINES = 20
LINE_CHARS = 400

_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
_MOCK_APPROVAL = re.compile(r"APPROVAL_REQUIRED: \S.*")
APPROVAL_PROVIDERS = frozenset({"mock_cli"})  # where the APPROVAL_REQUIRED line was observed


def clean_tail(output: str) -> List[str]:
    """The last 20 non-trailing-blank lines, 400 chars each; ANSI, control, bidi and C1 removed."""
    text = strip_invisible(_ANSI_RE.sub("", output.replace("\r\n", "\n").replace("\r", "\n")))
    lines = [
        "".join(c if ord(c) >= 32 and ord(c) != 127 else " " if c == "\t" else "" for c in ln)
        for ln in text.split("\n")
    ]
    while lines and not lines[-1].strip():
        lines.pop()
    return [ln[:LINE_CHARS] for ln in lines[-TAIL_LINES:]]


def suggest(tail: List[str], provider: str) -> List[Dict[str, str]]:
    """Quick answers for a recognised yes/no prompt of this provider on the last line."""
    if provider not in APPROVAL_PROVIDERS:
        return []
    last = next((ln for ln in reversed(tail) if ln.strip()), "")
    if _MOCK_APPROVAL.fullmatch(last.strip()):
        return [{"label": "Approve", "send": "y"}, {"label": "Deny", "send": "n"}]
    return []
