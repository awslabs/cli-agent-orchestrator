"""AG-UI 1.0 capabilities for CAO's run plane (#1192).

AG-UI 1.0 (``docs/spec/1.0/basic/capabilities.mdx``) defines the shape of a
capabilities declaration, ``AgentCapabilities``, and leaves retrieval to the
implementation: "An implementation that exposes capabilities by any means MUST
use this shape for them." CAO serves it at ``GET /agui/v1/capabilities``.

The declaration follows two rules from that page. "A producer SHOULD declare
only what it does", and "An omitted field means undeclared, not unsupported", so
a field CAO cannot vouch for is left out rather than set to ``false``:

- ``humanInTheLoop.approveWithEdits`` is omitted. The schema defines it for
  *tool-call* interrupts whose ``editedArgs`` replace tool arguments; CAO's
  interrupts carry no ``toolCallId``, its ``editedArgs`` is answer text, and
  edit is offered per provider. Each interrupt's ``responseSchema`` says whether
  edit is accepted for it.
- ``transport.resumable`` is never declared: the HTTP + SSE binding has no
  stream resumption.
- ``tools.clientProvided`` is declared ``false`` explicitly: the run plane does
  not read ``RunAgentInput.tools``.

The object is a plain dict so the route works without the ``[agui]`` extra; the
tests validate it against ``#/$defs/AgentCapabilities`` in the vendored 1.0 JSON
Schema, which closes every group, rather than the SDK models, which allow extras.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Any, Dict

AGENT_NAME = "cli-agent-orchestrator"


def _cao_version() -> str:
    try:
        return version(AGENT_NAME)
    except PackageNotFoundError:  # pragma: no cover - source tree without metadata
        return "unknown"


def build_capabilities() -> Dict[str, Any]:
    """Return the run plane's ``AgentCapabilities`` declaration (camelCase keys)."""
    return {
        "identity": {"name": AGENT_NAME, "version": _cao_version()},
        "transport": {"streaming": True},
        "tools": {"clientProvided": False},
        "state": {"snapshots": True},
        "humanInTheLoop": {"supported": True, "approvals": True, "interrupts": True},
    }


__all__ = ["AGENT_NAME", "build_capabilities"]
