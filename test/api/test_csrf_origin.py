"""Tests for the CSRF / cross-origin request guard on the HTTP API (CWE-352).

The API is default-unauthenticated on the loopback interface, so state-changing
HTTP methods must reject any browser-supplied ``Origin`` that is not the same
origin or an explicitly trusted one — otherwise any web page the operator
visits can forge POSTs to ``127.0.0.1`` (session launch, terminal keystroke
injection, profile install). Read methods stay open: they never mutate state.
"""

from unittest.mock import patch

CROSS_SITE_ORIGIN = "http://evil.example"
SAME_ORIGIN = "http://127.0.0.1:9889"
DEV_SERVER_ORIGIN = "http://localhost:5173"


class TestCrossSiteStateChangesBlocked:
    """State-changing methods carrying a disallowed Origin return 403."""

    def test_post_flow_blocked(self, client):
        """A cross-site POST is rejected before it reaches the route."""
        response = client.post("/flows", headers={"Origin": CROSS_SITE_ORIGIN})

        assert response.status_code == 403
        assert response.json() == {"detail": "Cross-origin request blocked"}

    def test_delete_flow_blocked(self, client):
        """A cross-site DELETE is rejected too."""
        response = client.delete("/flows/test-flow", headers={"Origin": CROSS_SITE_ORIGIN})

        assert response.status_code == 403

    def test_cross_site_blocked_even_with_same_host(self, client):
        """DNS rebinding can forge the Host but never the Origin.

        An attacker page that has rebound ``evil.example`` to 127.0.0.1 still
        can't set the ``Origin`` the browser sends, so the POST is rejected.
        """
        response = client.post(
            "/flows",
            headers={"Host": "127.0.0.1:9889", "Origin": CROSS_SITE_ORIGIN},
        )

        assert response.status_code == 403


class TestAllowedStateChanges:
    """Legitimate callers keep full write access."""

    def test_same_origin_allowed(self, client):
        """A same-origin browser POST passes the guard (422 is body validation)."""
        response = client.post(
            "/flows",
            headers={"Host": "127.0.0.1:9889", "Origin": SAME_ORIGIN},
        )

        assert response.status_code != 403

    def test_cors_listed_origin_allowed(self, client):
        """A page served from a configured dev-server origin is trusted."""
        response = client.post(
            "/flows",
            headers={"Host": "localhost", "Origin": DEV_SERVER_ORIGIN},
        )

        assert response.status_code != 403

    def test_missing_origin_allowed(self, client):
        """Non-browser clients (CLI, curl, MCP) send no Origin and pass."""
        response = client.post("/flows")

        assert response.status_code != 403


class TestReadsStayOpen:
    """GET requests are never blocked, whatever Origin they carry."""

    def test_get_with_cross_site_origin(self, client):
        """A cross-site read is still served."""
        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.list_flows.return_value = []
            response = client.get("/flows", headers={"Origin": CROSS_SITE_ORIGIN})

        assert response.status_code != 403
        assert response.status_code == 200
