"""A non-ASCII runtime token is rejected cleanly, not with a TypeError (#802).

``hmac.compare_digest`` on ``str`` raises ``TypeError`` for a non-ASCII operand.
The shared-HTTP token gate must encode both operands so a hostile or malformed
header is refused as unauthorized rather than crashing the request (Augusto).
"""

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.mcp_server.http_hosting import (
    RUNTIME_TOKEN_HEADER,
    CallerIdentityMiddleware,
)


def test_non_ascii_presented_token_is_unauthorized_not_typeerror():
    middleware = CallerIdentityMiddleware("expected-token")
    with patch(
        "cli_agent_orchestrator.mcp_server.http_hosting.get_http_headers",
        return_value={RUNTIME_TOKEN_HEADER: "café-non-ascii-tökén"},
    ):
        with pytest.raises(ValueError, match="unauthorized"):
            middleware._require_token()


def test_a_matching_token_still_passes():
    middleware = CallerIdentityMiddleware("expected-token")
    with patch(
        "cli_agent_orchestrator.mcp_server.http_hosting.get_http_headers",
        return_value={RUNTIME_TOKEN_HEADER: "expected-token"},
    ):
        # No exception: the correct token is accepted.
        middleware._require_token()
