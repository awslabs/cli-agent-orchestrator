"""Text that came from a terminal, made safe to show."""

from __future__ import annotations

import unicodedata


def strip_invisible(text: str) -> str:
    """Remove characters that change how text reads without showing: Unicode format characters
    (category Cf: bidi overrides and isolates U+202A to U+202E and U+2066 to U+2069, zero-width
    U+200B to U+200F, the byte-order mark, soft hyphen) and the C1 controls U+0080 to U+009F.
    Line separators U+2028 and U+2029 become a space. Everything else is kept."""
    out = []
    for ch in text:
        code = ord(ch)
        if 0x80 <= code <= 0x9F or unicodedata.category(ch) == "Cf":
            continue
        out.append(" " if code in (0x2028, 0x2029) else ch)
    return "".join(out)
