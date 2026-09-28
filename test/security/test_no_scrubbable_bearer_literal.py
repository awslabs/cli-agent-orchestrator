"""No source builds a bearer ``Authorization`` value from a ``Bearer {`` literal.

Copilot's review pipeline redacts token-shaped text (``Bearer <something>``) to
``******`` before the model reads it, so every helper that spelled the header
as ``f"Bearer {token}"`` was re-reported as "sends the literal ``******``"
round after round. The code was correct; the model's input was scrubbed. This
guard fails on any line that reintroduces the scrubbable shape, so the shared
builder in ``security.bearer`` stays the only place the scheme is written.

Scope: the source Stream P owns and converted. The remaining pre-existing
bearer builders (``mcp_server/*``, ``services/agui/*``, ``services/
memory_gateway.py``, ``ops_mcp_server/server.py``) are owned by other streams;
they are listed in ``_OWNED_BY_OTHER_STREAMS`` and reported for their owners to
route through the same builder.
"""

import pathlib
import re

# quote, Bearer, space  -- a string literal opening with the scheme; and
# Bearer followed by whitespace and a ``{`` -- an f-string joining the scheme
# to an interpolated token. Either is the shape Copilot's redaction bites on.
_SCRUB_BAIT = re.compile(r"""["']Bearer\s|Bearer\s+\{""")

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCAN_ROOTS = [
    _REPO_ROOT / "src",
    _REPO_ROOT / "examples" / "cao-clusters",
]

# Pre-existing bearer builders owned by other PR #802 streams. Not Stream P's
# to edit; each must be routed through ``security.bearer.authorization_header``
# (or a local scheme constant, for the standalone broker image) by its owner.
_OWNED_BY_OTHER_STREAMS = {
    "src/cli_agent_orchestrator/mcp_server/utils.py",
    "src/cli_agent_orchestrator/mcp_server/app_tools.py",
    "src/cli_agent_orchestrator/ops_mcp_server/server.py",
    "src/cli_agent_orchestrator/services/agui/base.py",
    "src/cli_agent_orchestrator/services/agui/stream_reader.py",
    "src/cli_agent_orchestrator/services/memory_gateway.py",
}


def _python_sources():
    for root in _SCAN_ROOTS:
        if root.exists():
            yield from root.rglob("*.py")


def test_no_scrubbable_bearer_literal_in_owned_sources():
    offenders = []
    for path in _python_sources():
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel in _OWNED_BY_OTHER_STREAMS:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if _SCRUB_BAIT.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "a bearer Authorization value is built from a scrubbable 'Bearer {' "
        "literal; use security.bearer.authorization_header instead:\n  " + "\n  ".join(offenders)
    )
