"""Fast regression tests for the opt-in Pi E2E configuration gate."""

from test.e2e import conftest as e2e_conftest
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("config_kind", ["missing", "file"])
def test_require_pi_skips_without_an_existing_config_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path, config_kind: str
) -> None:
    """The live Pi gate must reject unset and non-directory configuration paths."""
    monkeypatch.setattr(e2e_conftest.shutil, "which", lambda command: "/test/pi")
    if config_kind == "missing":
        monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    else:
        invalid_path = tmp_path / "not-a-directory"
        invalid_path.write_text("not Pi configuration", encoding="utf-8")
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(invalid_path))

    with pytest.raises(pytest.skip.Exception, match="PI_CODING_AGENT_DIR"):
        e2e_conftest.require_pi.__wrapped__(SimpleNamespace(home_dir=tmp_path))


def test_require_pi_accepts_existing_explicit_config_and_seeds_profiles(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """An explicit existing Pi config permits the existing isolated profile setup."""
    monkeypatch.setattr(e2e_conftest.shutil, "which", lambda command: "/test/pi")
    config_dir = tmp_path / "private-pi-config"
    config_dir.mkdir(mode=0o700)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(config_dir))

    server_home = tmp_path / "server-home"
    e2e_conftest.require_pi.__wrapped__(SimpleNamespace(home_dir=server_home))

    profile_store = server_home / ".aws" / "cli-agent-orchestrator" / "agent-store"
    assert [path.name for path in sorted(profile_store.glob("*.md"))] == [
        "analysis_supervisor.md",
        "data_analyst.md",
        "report_generator.md",
    ]
