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
