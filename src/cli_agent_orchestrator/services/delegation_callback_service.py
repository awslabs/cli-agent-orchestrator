"""Server-side callback guarantee for assign and send_message delegations.

See openspec/changes/assign-callback-guarantee/design.md. Edges are opened by
assign (terminal create) and send_message (inbox POST), armed when their task
is delivered, and closed by a reply, an auto-forward, or the worker ending.
"""

import logging
from typing import Optional

from cli_agent_orchestrator.clients.database import (
    close_delegation_edge,
    create_delegation_edge,
    find_open_edge,
    get_terminal_metadata,
    list_open_edges_into,
)
from cli_agent_orchestrator.models.delegation import EdgeReason, EdgeSource, EdgeState

logger = logging.getLogger(__name__)

_REASON_TEXT = {
    EdgeReason.NO_CALLBACK: "did not call send_message",
    EdgeReason.NO_SEND_MESSAGE_TOOL: (
        "has no send_message tool (cao-mcp-server is not in its profile)"
    ),
}
_UNREADABLE = "(last response could not be read)"


def build_auto_forward_message(
    worker_id: str, profile: Optional[str], reason: EdgeReason, last_response: Optional[str]
) -> str:
    return (
        f"[CAO auto-forward] Worker {worker_id} (profile {profile or 'unknown'}) "
        "went idle without calling send_message.\n"
        f"Reason: {_REASON_TEXT[reason]}\n"
        "Below is its last response, read from the terminal. It may be partial "
        "or not the final result.\n"
        f'The worker is still running. To continue: send_message(receiver_id="{worker_id}", ...)\n'
        f'When done with it: delete_terminal("{worker_id}")\n'
        "---\n"
        f"{last_response if last_response else _UNREADABLE}"
    )


def build_worker_ended_message(
    worker_id: str, profile: Optional[str], how: str, last_response: Optional[str]
) -> str:
    return (
        f"[CAO worker ended] Worker {worker_id} (profile {profile or 'unknown'}) {how} "
        "before sending you a result.\n"
        "Below is its last response if it could be read.\n"
        "---\n"
        f"{last_response if last_response else _UNREADABLE}"
    )


def on_send_message(sender_id: str, receiver_id: str, message_id: int, expect_reply: bool) -> None:
    """Apply the edge rules for one send_message (design D4). Best-effort."""
    owed = [e for e in list_open_edges_into(sender_id) if e.caller_id == receiver_id]
    if owed:
        for edge in owed:
            close_delegation_edge(edge.id, EdgeState.REPLIED)
        return

    if find_open_edge(sender_id, receiver_id) is not None:
        return

    metadata = get_terminal_metadata(receiver_id) or {}
    if metadata.get("caller_id") == sender_id or expect_reply:
        create_delegation_edge(
            sender_id, receiver_id, EdgeSource.SEND_MESSAGE, message_id=message_id
        )
