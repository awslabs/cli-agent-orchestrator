"""Tests for CLI Agent Orchestrator constants."""

from pathlib import Path
from unittest.mock import patch


class TestServerConstants:
    """Tests for server configuration constants."""

    def test_server_host_defaults_to_127_0_0_1(self):
        """Test that SERVER_HOST defaults to '127.0.0.1' (not 'localhost')."""
        # Re-import with clean environment to test default
        with patch.dict("os.environ", {}, clear=False):
            # Remove CAO_API_HOST if present so the default is used
            import os

            env_copy = os.environ.copy()
            env_copy.pop("CAO_API_HOST", None)
            with patch.dict("os.environ", env_copy, clear=True):
                import importlib

                import cli_agent_orchestrator.constants as constants_module

                importlib.reload(constants_module)
                assert constants_module.SERVER_HOST == "127.0.0.1"

    def test_server_port_defaults_to_9889(self):
        """Test that SERVER_PORT defaults to 9889."""
        import os

        env_copy = os.environ.copy()
        env_copy.pop("CAO_API_PORT", None)
        with patch.dict("os.environ", env_copy, clear=True):
            import importlib

            import cli_agent_orchestrator.constants as constants_module

            importlib.reload(constants_module)
            assert constants_module.SERVER_PORT == 9889

    def test_server_host_is_not_localhost(self):
        """Test that the default SERVER_HOST is an IP, not 'localhost'."""
        import os

        env_copy = os.environ.copy()
        env_copy.pop("CAO_API_HOST", None)
        with patch.dict("os.environ", env_copy, clear=True):
            import importlib

            import cli_agent_orchestrator.constants as constants_module

            importlib.reload(constants_module)
            assert constants_module.SERVER_HOST != "localhost"


class TestCorsOrigins:
    """Tests for CORS configuration constants."""

    def test_cors_origins_includes_localhost_5173(self):
        """Test that CORS_ORIGINS includes localhost:5173 for the web UI."""
        from cli_agent_orchestrator.constants import CORS_ORIGINS

        assert "http://localhost:5173" in CORS_ORIGINS

    def test_cors_origins_includes_127_0_0_1_5173(self):
        """Test that CORS_ORIGINS includes 127.0.0.1:5173 for the web UI."""
        from cli_agent_orchestrator.constants import CORS_ORIGINS

        assert "http://127.0.0.1:5173" in CORS_ORIGINS

    def test_cors_origins_includes_localhost_3000(self):
        """Test that CORS_ORIGINS includes localhost:3000."""
        from cli_agent_orchestrator.constants import CORS_ORIGINS

        assert "http://localhost:3000" in CORS_ORIGINS

    def test_cors_origins_includes_127_0_0_1_3000(self):
        """Test that CORS_ORIGINS includes 127.0.0.1:3000."""
        from cli_agent_orchestrator.constants import CORS_ORIGINS

        assert "http://127.0.0.1:3000" in CORS_ORIGINS


class TestNetworkAllowlistEnvOverrides:
    """Tests for env-var-driven extensions of the network allowlists (issues #149, #151).

    Each override extends the built-in defaults rather than replacing them, so an
    operator can add a Docker bridge IP or a custom origin without locking
    themselves out of loopback access.
    """

    def _reload_constants(self, env_overrides):
        import importlib
        import os

        env_copy = os.environ.copy()
        # Strip any pre-set network override vars so the test starts from the
        # documented defaults, then layer the overrides under test on top.
        for key in ("CAO_CORS_ORIGINS", "CAO_ALLOWED_HOSTS", "CAO_WS_ALLOWED_CLIENTS"):
            env_copy.pop(key, None)
        env_copy.update(env_overrides)
        with patch.dict("os.environ", env_copy, clear=True):
            import cli_agent_orchestrator.constants as constants_module

            importlib.reload(constants_module)
            return constants_module

    def test_cao_cors_origins_extends_defaults(self):
        mod = self._reload_constants(
            {"CAO_CORS_ORIGINS": "http://app.local,http://example.test:9000"}
        )
        # Use .count() == 1 instead of `in` so CodeQL's
        # py/incomplete-url-substring-sanitization query does not
        # pattern-match (the operand is a list, so `in` is exact-match,
        # but the AST shape triggers a false positive).
        origins = list(mod.CORS_ORIGINS)
        assert origins.count("http://localhost:5173") == 1
        assert origins.count("http://app.local") == 1
        assert origins.count("http://example.test:9000") == 1

    def test_cao_allowed_hosts_extends_defaults(self):
        mod = self._reload_constants({"CAO_ALLOWED_HOSTS": "cao.internal,proxy.example.com"})
        hosts = list(mod.ALLOWED_HOSTS)
        assert hosts.count("localhost") == 1
        assert hosts.count("127.0.0.1") == 1
        assert hosts.count("cao.internal") == 1
        assert hosts.count("proxy.example.com") == 1

    def test_cao_ws_allowed_clients_extends_defaults(self):
        mod = self._reload_constants({"CAO_WS_ALLOWED_CLIENTS": "172.17.0.1, 192.168.1.5"})
        assert "127.0.0.1" in mod.WS_ALLOWED_CLIENTS
        assert "::1" in mod.WS_ALLOWED_CLIENTS
        assert "172.17.0.1" in mod.WS_ALLOWED_CLIENTS
        # Leading whitespace stripped
        assert "192.168.1.5" in mod.WS_ALLOWED_CLIENTS

    def test_overrides_skip_empty_segments(self):
        mod = self._reload_constants({"CAO_WS_ALLOWED_CLIENTS": ",,172.17.0.1,, ,"})
        assert "" not in mod.WS_ALLOWED_CLIENTS
        assert "172.17.0.1" in mod.WS_ALLOWED_CLIENTS

    def test_defaults_when_env_not_set(self):
        mod = self._reload_constants({})
        # Defaults intact, no extras.
        assert mod.WS_ALLOWED_CLIENTS == ["127.0.0.1", "::1", "localhost"]
        assert mod.ALLOWED_HOSTS == ["localhost", "127.0.0.1"]
        assert mod.CORS_ORIGINS == [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]


class TestAddLocalCorsOrigins:
    """Tests for runtime CORS extension from the cao-server listen address (issue #151)."""

    def _reload_constants(self):
        """Reload constants with the network override env vars stripped so the
        list under test always starts from the documented defaults."""
        import importlib
        import os

        env_copy = os.environ.copy()
        for key in ("CAO_CORS_ORIGINS", "CAO_ALLOWED_HOSTS", "CAO_WS_ALLOWED_CLIENTS"):
            env_copy.pop(key, None)
        with patch.dict("os.environ", env_copy, clear=True):
            import cli_agent_orchestrator.constants as constants_module

            importlib.reload(constants_module)
            return constants_module

    def test_custom_port_on_loopback_host_adds_localhost_and_ip_origins(self):
        mod = self._reload_constants()
        mod.add_local_cors_origins("127.0.0.1", 9999)
        assert "http://localhost:9999" in mod.CORS_ORIGINS
        assert "http://127.0.0.1:9999" in mod.CORS_ORIGINS

    def test_wildcard_bind_derives_loopback_origins(self):
        mod = self._reload_constants()
        mod.add_local_cors_origins("0.0.0.0", 8080)
        assert "http://localhost:8080" in mod.CORS_ORIGINS
        assert "http://127.0.0.1:8080" in mod.CORS_ORIGINS

    def test_custom_host_adds_that_host_only(self):
        mod = self._reload_constants()
        mod.add_local_cors_origins("cao.internal", 9889)
        assert "http://cao.internal:9889" in mod.CORS_ORIGINS
        assert "http://localhost:9889" not in mod.CORS_ORIGINS
        assert "http://127.0.0.1:9889" not in mod.CORS_ORIGINS

    def test_idempotent_when_called_twice(self):
        mod = self._reload_constants()
        mod.add_local_cors_origins("127.0.0.1", 9999)
        mod.add_local_cors_origins("127.0.0.1", 9999)
        assert mod.CORS_ORIGINS.count("http://localhost:9999") == 1
        assert mod.CORS_ORIGINS.count("http://127.0.0.1:9999") == 1

    def test_default_port_does_not_duplicate_existing_origins(self):
        mod = self._reload_constants()
        # 5173 is already in the built-in defaults; calling with that port
        # must not add a duplicate entry.
        mod.add_local_cors_origins("127.0.0.1", 5173)
        assert mod.CORS_ORIGINS.count("http://localhost:5173") == 1
        assert mod.CORS_ORIGINS.count("http://127.0.0.1:5173") == 1

    def test_mutation_is_observable_through_existing_reference(self):
        """CORSMiddleware stores the list by reference. Mutating the module
        attribute after middleware install must be visible to anyone holding
        a prior reference, otherwise the runtime extension does nothing."""
        mod = self._reload_constants()
        captured = mod.CORS_ORIGINS  # the reference the middleware would hold
        mod.add_local_cors_origins("127.0.0.1", 7777)
        assert "http://localhost:7777" in captured
        assert "http://127.0.0.1:7777" in captured

    def test_ipv6_loopback_adds_all_loopback_aliases(self):
        """``::1`` is loopback like ``127.0.0.1`` / ``localhost``: any of the
        three should grant same-host access from a browser that picks any of
        the others, so all three origins are added."""
        mod = self._reload_constants()
        mod.add_local_cors_origins("::1", 9999)
        assert "http://localhost:9999" in mod.CORS_ORIGINS
        assert "http://127.0.0.1:9999" in mod.CORS_ORIGINS
        assert "http://[::1]:9999" in mod.CORS_ORIGINS

    def test_ipv6_wildcard_bind_includes_bracketed_loopback(self):
        """Binding on ``::`` must also allow IPv6 loopback in brackets — that
        is the form a browser actually emits in ``Origin``."""
        mod = self._reload_constants()
        mod.add_local_cors_origins("::", 8080)
        assert "http://[::1]:8080" in mod.CORS_ORIGINS

    def test_ipv6_literal_host_is_bracketed(self):
        """A non-loopback IPv6 literal must be formatted with brackets so the
        derived origin matches the ``Origin`` header the browser sends."""
        mod = self._reload_constants()
        mod.add_local_cors_origins("2001:db8::1", 9889)
        assert "http://[2001:db8::1]:9889" in mod.CORS_ORIGINS
        # The unbracketed form would never match a real Origin header and so
        # would only bloat the allowlist — guard against accidental reintro.
        assert "http://2001:db8::1:9889" not in mod.CORS_ORIGINS


class TestCaoHomeDir:
    """Tests for CAO home directory constants."""

    def test_cao_home_dir_is_under_aws_cli_agent_orchestrator(self):
        """Test that CAO_HOME_DIR is under ~/.aws/cli-agent-orchestrator."""
        from cli_agent_orchestrator.constants import CAO_HOME_DIR

        expected = Path.home() / ".aws" / "cli-agent-orchestrator"
        assert CAO_HOME_DIR == expected

    def test_cao_home_dir_is_pathlib_path(self):
        """Test that CAO_HOME_DIR is a Path object."""
        from cli_agent_orchestrator.constants import CAO_HOME_DIR

        assert isinstance(CAO_HOME_DIR, Path)

    def test_db_dir_is_under_cao_home(self):
        """Test that DB_DIR is under CAO_HOME_DIR."""
        from cli_agent_orchestrator.constants import CAO_HOME_DIR, DB_DIR

        assert DB_DIR == CAO_HOME_DIR / "db"

    def test_local_agent_store_dir_is_under_cao_home(self):
        """Test that LOCAL_AGENT_STORE_DIR is under CAO_HOME_DIR."""
        from cli_agent_orchestrator.constants import CAO_HOME_DIR, LOCAL_AGENT_STORE_DIR

        assert LOCAL_AGENT_STORE_DIR == CAO_HOME_DIR / "agent-store"

    def test_skills_dir_is_under_cao_home(self):
        """Test that SKILLS_DIR is under CAO_HOME_DIR."""
        from cli_agent_orchestrator.constants import CAO_HOME_DIR, SKILLS_DIR

        assert SKILLS_DIR == CAO_HOME_DIR / "skills"


class TestSessionConstants:
    """Tests for session configuration constants."""

    def test_session_prefix(self):
        """Test that SESSION_PREFIX is 'cao-'."""
        from cli_agent_orchestrator.constants import SESSION_PREFIX

        assert SESSION_PREFIX == "cao-"


class TestOpenCodeConstants:
    """Tests for OpenCode provider path constants."""

    def test_opencode_config_dir_resolves_correctly(self):
        from pathlib import Path

        from cli_agent_orchestrator.constants import OPENCODE_CONFIG_DIR

        assert OPENCODE_CONFIG_DIR == Path.home() / ".aws" / "opencode"

    def test_opencode_agents_dir_is_under_config_dir(self):
        from cli_agent_orchestrator.constants import OPENCODE_AGENTS_DIR, OPENCODE_CONFIG_DIR

        assert OPENCODE_AGENTS_DIR == OPENCODE_CONFIG_DIR / "agents"

    def test_opencode_config_file_is_json(self):
        from cli_agent_orchestrator.constants import OPENCODE_CONFIG_DIR, OPENCODE_CONFIG_FILE

        assert OPENCODE_CONFIG_FILE == OPENCODE_CONFIG_DIR / "opencode.json"
        assert OPENCODE_CONFIG_FILE.suffix == ".json"

    def test_opencode_config_dir_is_pathlib_path(self):
        from pathlib import Path

        from cli_agent_orchestrator.constants import OPENCODE_CONFIG_DIR

        assert isinstance(OPENCODE_CONFIG_DIR, Path)

    def test_opencode_cli_in_providers_list(self):
        from cli_agent_orchestrator.constants import PROVIDERS

        assert "opencode_cli" in PROVIDERS


class TestOpenCodeProviderType:
    """Tests for OPENCODE_CLI entry in ProviderType enum."""

    def test_opencode_cli_enum_value(self):
        from cli_agent_orchestrator.models.provider import ProviderType

        assert ProviderType.OPENCODE_CLI.value == "opencode_cli"

    def test_opencode_cli_importable(self):
        from cli_agent_orchestrator.models.provider import ProviderType

        assert hasattr(ProviderType, "OPENCODE_CLI")
