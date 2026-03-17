"""Security tests for DNS rebinding protection and host validation."""

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app

client = TestClient(app)


class TestDNSRebindingProtection:
    """Test suite for DNS rebinding attack prevention via TrustedHostMiddleware."""

    def test_localhost_hostname_allowed(self):
        """Legitimate requests with 'localhost' Host header should be accepted."""
        response = client.get("/health", headers={"Host": "localhost"})
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_localhost_hostname_with_port_allowed(self):
        """Requests with 'localhost:9889' Host header should be accepted."""
        response = client.get("/health", headers={"Host": "localhost:9889"})
        assert response.status_code == 200

    def test_ipv4_loopback_allowed(self):
        """IPv4 loopback address '127.0.0.1' should be allowed."""
        response = client.get("/health", headers={"Host": "127.0.0.1"})
        assert response.status_code == 200

    def test_ipv4_loopback_with_port_allowed(self):
        """IPv4 loopback with port '127.0.0.1:9889' should be allowed."""
        response = client.get("/health", headers={"Host": "127.0.0.1:9889"})
        assert response.status_code == 200

    @pytest.mark.skip(reason="IPv6 support is optional - CAO primarily uses IPv4 localhost")
    def test_ipv6_loopback_with_brackets_allowed(self):
        """IPv6 loopback '[::1]' with brackets should be allowed."""
        response = client.get("/health", headers={"Host": "[::1]"})
        assert response.status_code == 200

    @pytest.mark.skip(reason="IPv6 support is optional - CAO primarily uses IPv4 localhost")
    def test_ipv6_loopback_without_brackets_allowed(self):
        """IPv6 loopback '::1' without brackets should be allowed."""
        response = client.get("/health", headers={"Host": "::1"})
        assert response.status_code == 200

    def test_arbitrary_domain_rejected(self):
        """Requests with arbitrary domain Host header should be blocked."""
        response = client.get("/health", headers={"Host": "attack.poc"})
        assert response.status_code == 400

    def test_external_domain_rejected(self):
        """External domains like 'example.com' should be rejected."""
        response = client.get("/health", headers={"Host": "example.com"})
        assert response.status_code == 400

    def test_malicious_domain_rejected(self):
        """Malicious domains should be rejected."""
        response = client.get("/health", headers={"Host": "malicious-site.com"})
        assert response.status_code == 400

    def test_dns_rebinding_attack_simulation(self):
        """Simulate DNS rebinding attack - attacker's domain after rebind should be blocked."""
        # After DNS rebinding, attacker's domain points to 127.0.0.1
        # But Host header still says "attack.poc"
        response = client.post(
            "/sessions",
            headers={"Host": "attack.poc"},
            params={"provider": "kiro_cli", "agent_profile": "developer"},
        )
        # Should be blocked before reaching the endpoint
        assert response.status_code == 400

    def test_subdomain_of_localhost_rejected(self):
        """Subdomains of localhost should be rejected (e.g., 'evil.localhost')."""
        response = client.get("/health", headers={"Host": "evil.localhost"})
        assert response.status_code == 400

    def test_localhost_lookalike_rejected(self):
        """Domains that look like localhost should be rejected."""
        response = client.get("/health", headers={"Host": "localhost.attacker.com"})
        assert response.status_code == 400

    def test_ip_lookalike_rejected(self):
        """Domains that look like IP addresses should be rejected."""
        response = client.get("/health", headers={"Host": "127.0.0.2"})
        assert response.status_code == 400

    def test_missing_host_header_rejected(self):
        """Requests without Host header should be rejected."""
        # Note: TestClient automatically adds Host header, so we test with empty string
        response = client.get("/health", headers={"Host": ""})
        assert response.status_code == 400


class TestCriticalEndpointProtection:
    """Test that critical endpoints are protected from DNS rebinding."""

    def test_create_session_protected(self):
        """POST /sessions endpoint should reject malicious Host headers."""
        response = client.post(
            "/sessions",
            headers={"Host": "malicious.com"},
            params={"provider": "kiro_cli", "agent_profile": "developer"},
        )
        assert response.status_code == 400

    def test_send_terminal_input_protected(self):
        """POST /terminals/{id}/input should reject malicious Host headers."""
        response = client.post(
            "/terminals/fake-id/input",
            headers={"Host": "attacker.poc"},
            params={"message": "malicious command"},
        )
        assert response.status_code == 400

    def test_get_terminal_output_protected(self):
        """GET /terminals/{id}/output should reject malicious Host headers."""
        response = client.get(
            "/terminals/fake-id/output",
            headers={"Host": "evil.example.com"},
            params={"mode": "full"},
        )
        assert response.status_code == 400

    def test_delete_session_protected(self):
        """DELETE /sessions/{name} should reject malicious Host headers."""
        response = client.delete("/sessions/fake-session", headers={"Host": "attacker.com"})
        assert response.status_code == 400


class TestRealWorldAttackScenarios:
    """Test scenarios from the actual CVE report."""

    def test_cao_terminal_injection_poc_blocked(self):
        """
        Simulate the exact attack from the security report PoC.

        The attacker's JavaScript tries to:
        1. Enumerate sessions: GET /sessions with Host: attack.poc
        2. List terminals: GET /sessions/{name}/terminals with Host: attack.poc
        3. Inject prompt: POST /terminals/{id}/input with Host: attack.poc
        4. Read output: GET /terminals/{id}/output with Host: attack.poc

        All should be blocked by TrustedHostMiddleware.
        """
        # Step 1: Enumerate sessions (should be blocked)
        response = client.get("/sessions", headers={"Host": "attack.poc"})
        assert response.status_code == 400

        # Step 2: List terminals (should be blocked)
        response = client.get(
            "/sessions/cao-fake-session/terminals", headers={"Host": "attack.poc"}
        )
        assert response.status_code == 400

        # Step 3: Inject malicious prompt (should be blocked)
        response = client.post(
            "/terminals/fake-terminal-id/input",
            headers={"Host": "attack.poc"},
            params={"message": "launch the calculator"},  # From actual PoC
        )
        assert response.status_code == 400

        # Step 4: Read terminal output (should be blocked)
        response = client.get(
            "/terminals/fake-terminal-id/output",
            headers={"Host": "attack.poc"},
            params={"mode": "full"},
        )
        assert response.status_code == 400

    def test_singularity_dns_rebinding_blocked(self):
        """
        Test against Singularity DNS rebinding tool configuration.

        From the PoC, attacker uses:
        - attackHostDomain: "attack.poc"
        - targetHostIPAddress: "127.0.0.1"

        After rebinding, attack.poc points to 127.0.0.1, but Host header
        still says "attack.poc" - this should be blocked.
        """
        response = client.get("/health", headers={"Host": "attack.poc"})
        assert response.status_code == 400

        # Even with port
        response = client.get("/health", headers={"Host": "attack.poc:9889"})
        assert response.status_code == 400


class TestLegitimateUseCases:
    """Ensure legitimate CAO usage patterns still work."""

    def test_cao_cli_can_connect(self):
        """CAO CLI connecting to localhost should work."""
        response = client.get("/health", headers={"Host": "localhost:9889"})
        assert response.status_code == 200

    def test_mcp_server_can_connect(self):
        """MCP server connecting to localhost should work."""
        response = client.get("/health", headers={"Host": "127.0.0.1:9889"})
        assert response.status_code == 200

    def test_browser_localhost_access(self):
        """Browser accessing http://localhost:9889 should work."""
        response = client.get("/health", headers={"Host": "localhost"})
        assert response.status_code == 200

    def test_curl_localhost_access(self):
        """curl http://127.0.0.1:9889/health should work."""
        response = client.get("/health", headers={"Host": "127.0.0.1"})
        assert response.status_code == 200
