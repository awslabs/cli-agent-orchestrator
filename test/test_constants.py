"""Tests for CLI Agent Orchestrator constants."""

from pathlib import Path
from unittest.mock import patch

import pytest


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
            "http://localhost:5174",
            "http://127.0.0.1:5174",
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
        host, port = "cao.internal", 9889
        mod.add_local_cors_origins(host, port)
        origins = list(mod.CORS_ORIGINS)
        assert origins.count(f"http://{host}:{port}") == 1
        assert origins.count(f"http://localhost:{port}") == 0
        assert origins.count(f"http://127.0.0.1:{port}") == 0

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


class TestPipeLivenessCheckIntervalClamp:
    """Regression for the round-3 Copilot review on #397: PIPE_LIVENESS_CHECK_INTERVAL_S
    feeds ``threading.Event.wait(timeout)`` in the watchdog's poll loop
    (services/fifo_reader.py's ``_watchdog_loop``) — a non-positive value makes
    ``wait()`` return immediately every iteration, turning the periodic poll
    into a hot spin (high CPU + a tmux ``capture-pane`` call as fast as the
    loop can run). A non-positive value here is invalid for the parameter's
    meaning, not just atypical, so it's treated the same way an unparseable
    string already is: fall back to the default.
    """

    def _reload_constants(self, env_overrides):
        import importlib
        import os

        env_copy = os.environ.copy()
        env_copy.pop("CAO_PIPE_LIVENESS_CHECK_INTERVAL_S", None)
        env_copy.update(env_overrides)
        with patch.dict("os.environ", env_copy, clear=True):
            import cli_agent_orchestrator.constants as constants_module

            importlib.reload(constants_module)
            return constants_module

    def test_zero_falls_back_to_default(self):
        mod = self._reload_constants({"CAO_PIPE_LIVENESS_CHECK_INTERVAL_S": "0"})
        assert mod.PIPE_LIVENESS_CHECK_INTERVAL_S == 4.0

    def test_negative_falls_back_to_default(self):
        mod = self._reload_constants({"CAO_PIPE_LIVENESS_CHECK_INTERVAL_S": "-1.5"})
        assert mod.PIPE_LIVENESS_CHECK_INTERVAL_S == 4.0

    def test_non_numeric_falls_back_to_default(self):
        mod = self._reload_constants({"CAO_PIPE_LIVENESS_CHECK_INTERVAL_S": "not-a-number"})
        assert mod.PIPE_LIVENESS_CHECK_INTERVAL_S == 4.0

    def test_positive_value_is_honored(self):
        mod = self._reload_constants({"CAO_PIPE_LIVENESS_CHECK_INTERVAL_S": "0.5"})
        assert mod.PIPE_LIVENESS_CHECK_INTERVAL_S == 0.5

    def test_default_when_env_not_set(self):
        mod = self._reload_constants({})
        assert mod.PIPE_LIVENESS_CHECK_INTERVAL_S == 4.0


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


class TestCaoHomeDirEnvOverride:
    """The ``CAO_HOME_DIR`` env var relocates CAO's entire data tree.

    Some environments restrict access to ``~/.aws`` (where CAO stores its data
    by default) to protect AWS credentials, which can leave CAO unable to read
    its own agent profiles. Setting ``CAO_HOME_DIR`` moves the whole tree
    elsewhere. The override is read at import, so a single reload must
    propagate to every derived path.
    """

    def _reload_constants(self, env_overrides):
        import importlib
        import os

        env_copy = os.environ.copy()
        env_copy.pop("CAO_HOME_DIR", None)
        env_copy.update(env_overrides)
        with patch.dict("os.environ", env_copy, clear=True):
            import cli_agent_orchestrator.constants as constants_module

            importlib.reload(constants_module)
            return constants_module

    def _reload_constants_and_settings(self, override):
        """Reload constants, then settings_service, under a CAO_HOME_DIR override.

        ``settings_service`` binds ``CAO_HOME_DIR`` at its own import time, so it
        must be reloaded *after* constants for the override to reach its
        ``_DEFAULTS`` agent-dir map. Passing ``None`` restores the defaults.
        """
        import importlib
        import os

        env_copy = os.environ.copy()
        env_copy.pop("CAO_HOME_DIR", None)
        if override is not None:
            env_copy["CAO_HOME_DIR"] = str(override)
        with patch.dict("os.environ", env_copy, clear=True):
            import cli_agent_orchestrator.constants as constants_module
            import cli_agent_orchestrator.services.settings_service as settings_module

            importlib.reload(constants_module)
            importlib.reload(settings_module)
            return constants_module, settings_module

    @pytest.fixture(autouse=True)
    def _restore_default_constants(self):
        # Reloading under an override mutates the shared modules in place; reload
        # them back to their original-env state after each test so the override
        # cannot leak into tests (here or in other files) that import directly.
        import os

        original_value = os.environ.get("CAO_HOME_DIR")
        yield
        self._reload_constants_and_settings(original_value)

    def test_override_relocates_home_dir(self, tmp_path):
        override = tmp_path / "cao-home"
        mod = self._reload_constants({"CAO_HOME_DIR": str(override)})
        assert mod.CAO_HOME_DIR == override.resolve()

    def test_derived_paths_follow_override(self, tmp_path):
        override = tmp_path / "cao-home"
        mod = self._reload_constants({"CAO_HOME_DIR": str(override)})
        resolved = override.resolve()
        assert mod.DB_DIR == resolved / "db"
        assert mod.LOG_DIR == resolved / "logs"
        assert mod.FIFO_DIR == resolved / "fifos"
        assert mod.AGENT_CONTEXT_DIR == resolved / "agent-context"
        assert mod.LOCAL_AGENT_STORE_DIR == resolved / "agent-store"
        assert mod.SKILLS_DIR == resolved / "skills"
        assert mod.MEMORY_BASE_DIR == resolved / "memory"
        assert mod.DATABASE_FILE == resolved / "db" / "cli-agent-orchestrator.db"

    def test_import_time_dirs_created_under_override(self, tmp_path):
        # constants.py mkdirs TERMINAL_LOG_DIR and FIFO_DIR at import; under the
        # override they must be created below the new root, never under ~/.aws.
        override = tmp_path / "cao-home"
        self._reload_constants({"CAO_HOME_DIR": str(override)})
        resolved = override.resolve()
        assert (resolved / "logs" / "terminal").is_dir()
        assert (resolved / "fifos").is_dir()

    def test_agent_dir_defaults_follow_override(self, tmp_path):
        # Load-bearing for the restricted-~/.aws case: the agent-store and
        # agent-context defaults used for the handoff profile read must relocate.
        override = tmp_path / "cao-home"
        _, settings_module = self._reload_constants_and_settings(override)
        resolved = override.resolve()
        assert settings_module._DEFAULTS["claude_code"] == str(resolved / "agent-store")
        assert settings_module._DEFAULTS["codex"] == str(resolved / "agent-store")
        assert settings_module._DEFAULTS["cao_installed"] == str(resolved / "agent-context")
        # kiro_cli tracks ~/.kiro, not CAO_HOME_DIR, so it is intentionally unchanged.
        assert settings_module._DEFAULTS["kiro_cli"] == str(Path.home() / ".kiro" / "agents")

    def test_default_when_env_not_set(self):
        mod = self._reload_constants({})
        assert mod.CAO_HOME_DIR == Path.home() / ".aws" / "cli-agent-orchestrator"

    def test_empty_string_treated_as_unset(self):
        # An empty CAO_HOME_DIR (e.g. `export CAO_HOME_DIR=`) must not resolve
        # to CWD; treat it as unset and fall back to the default.
        mod = self._reload_constants({"CAO_HOME_DIR": ""})
        assert mod.CAO_HOME_DIR == Path.home() / ".aws" / "cli-agent-orchestrator"

    def test_whitespace_only_treated_as_unset(self):
        mod = self._reload_constants({"CAO_HOME_DIR": "   "})
        assert mod.CAO_HOME_DIR == Path.home() / ".aws" / "cli-agent-orchestrator"

    def test_tilde_expanded(self, tmp_path):
        # A literal ~/some-path must be expanded, not create a dir named "~".
        mod = self._reload_constants({"CAO_HOME_DIR": "~/cao-test-data"})
        expected = Path("~/cao-test-data").expanduser().resolve()
        assert mod.CAO_HOME_DIR == expected
        assert "~" not in str(mod.CAO_HOME_DIR)

    def test_import_time_dirs_have_restricted_permissions(self, tmp_path):
        # Directories created at import time must be owner-only (0o700) to
        # protect secret-bearing terminal logs when relocated outside ~/.aws.
        override = tmp_path / "cao-home"
        self._reload_constants({"CAO_HOME_DIR": str(override)})
        terminal_log_dir = override / "logs" / "terminal"
        fifo_dir = override / "fifos"
        assert terminal_log_dir.stat().st_mode & 0o777 == 0o700
        assert fifo_dir.stat().st_mode & 0o777 == 0o700


class TestSessionConstants:
    """Tests for session configuration constants."""

    def test_session_prefix(self):
        """Test that SESSION_PREFIX is 'cao-'."""
        from cli_agent_orchestrator.constants import SESSION_PREFIX

        assert SESSION_PREFIX == "cao-"


class TestEventBusConstants:
    """Tests for event bus configuration constants."""

    def _reload_constants(self, env_overrides):
        import importlib
        import os

        env_copy = os.environ.copy()
        env_copy.pop("CAO_EVENT_BUS_MAX_QUEUE_SIZE", None)
        env_copy.update(env_overrides)
        with patch.dict("os.environ", env_copy, clear=True):
            import cli_agent_orchestrator.constants as constants_module

            importlib.reload(constants_module)
            return constants_module

    def test_event_bus_queue_size_falls_back_when_env_is_non_numeric(self):
        mod = self._reload_constants({"CAO_EVENT_BUS_MAX_QUEUE_SIZE": "not-a-number"})

        assert mod.EVENT_BUS_MAX_QUEUE_SIZE == 16384


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
