"""Every bearer ``Authorization`` header is built by ``security.bearer``.

A source line that joins the scheme to a token in one literal (``f"Bearer {token}"``)
is written out in exactly one place, ``security.bearer.authorization_header``, as a
named constant plus the token. Some review tooling redacts text shaped like
``Bearer <value>`` before reading source, which made correct helpers read as if they
sent a placeholder. Keeping the shape out of the tree avoids that and keeps one
builder for the header.
"""

import pathlib
import re

# A string literal opening with the scheme and a space, or the scheme followed by
# whitespace and an interpolated value.
_INLINE_BEARER = re.compile(r"""["']Bearer\s|Bearer\s+\{""")

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCAN_ROOTS = [
    _REPO_ROOT / "src",
    _REPO_ROOT / "examples" / "cao-clusters",
]


def _python_sources():
    for root in _SCAN_ROOTS:
        if root.exists():
            yield from root.rglob("*.py")


def test_no_source_builds_a_bearer_header_inline():
    offenders = []
    for path in _python_sources():
        rel = path.relative_to(_REPO_ROOT).as_posix()
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if _INLINE_BEARER.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "build the Authorization header with security.bearer.authorization_header:\n  "
        + "\n  ".join(offenders)
    )
