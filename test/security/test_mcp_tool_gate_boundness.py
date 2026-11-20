"""Boundness is PRESENCE of ``CAO_TERMINAL_ID``, not truthiness of it (#1062).

``security.mcp_tool_gate.caller_is_bound`` decides which of two populations is
calling an MCP surface, and the two get opposite treatment: an operator context
(a human's MCP host, ``cao assign``) is never gated, while a bound CAO terminal
is gated on its recorded allowlist. Reading that distinction as
``bool(os.environ.get(...))`` collapsed a THIRD case into the operator one -- the
variable present and empty -- and an empty value is reachable from a profile:
until this change four providers preserved a profile-declared ``CAO_TERMINAL_ID``
instead of injecting the launching terminal's own id, so
``env: {CAO_TERMINAL_ID: ""}`` plus no ops grant dispatched every non-exempt ops
tool as an operator call, with no row lookup at all.

The rule these tests pin: the variable PRESENT means a caller claiming an
identity, whatever its value. An empty or whitespace value is then an identity
that cannot be resolved, which is the fail-closed refusal the surfaces already
had for a malformed id -- not permission.

Found by independent review of #1062 (probe: ``EMPTY_ID outcome=allowed
lookup_calls=0``). The per-surface halves of this are
``test/ops_mcp_server/test_ops_tool_surface_allowlist.py::TestAnEmptyIdentityIsNotAnOperatorContext``
and
``test/mcp_server/test_tool_allowlist_enforcement.py::TestAnEmptyIdentityIsNotAnOperatorContext``;
the provider half is
``test/providers/test_terminal_identity_forwarding.py``.
"""

import os
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.security.mcp_tool_gate import caller_is_bound

#: Values a caller can have for ``CAO_TERMINAL_ID`` that resolve to no usable
#: identity. Every one of them is still a CLAIM of identity.
UNUSABLE = ["", " ", "   ", "\t", "\n", "not-a-terminal"]


def _unbound_env():
    return {k: v for k, v in os.environ.items() if k != "CAO_TERMINAL_ID"}


def test_an_absent_variable_is_the_operator_context():
    with patch.dict(os.environ, _unbound_env(), clear=True):
        assert caller_is_bound() is False


def test_a_real_terminal_id_is_bound():
    with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}):
        assert caller_is_bound() is True


@pytest.mark.parametrize("value", UNUSABLE)
def test_a_present_but_unusable_value_is_bound_not_operator(value):
    """The caller claims an identity; that it cannot be resolved is a refusal.

    Deciding boundness by truthiness made ``""`` indistinguishable from the
    operator context, so the gate returned "allow" before any lookup. Presence
    is the question, because only the ABSENCE of the variable is the supported
    unbound path.
    """
    with patch.dict(os.environ, {"CAO_TERMINAL_ID": value}):
        assert caller_is_bound() is True
