"""create_terminal hands the backend cao.profile and, with a caller, cao.caller.

Spec ``herdr-app-role-labels``, "Terminals carry their profile and caller as
labels": the root, worker and added-window scenarios. The tests observe the
backend call that ``create_terminal`` makes; the last class also runs it on a
real ``HerdrAppBackend`` over the fake core and reads what ``terminal.run``
received.
"""

from contextlib import ExitStack
from test.backends.fake_hc import FakeHc
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.backends import registry
from cli_agent_orchestrator.backends.herdr_app_backend import HerdrAppBackend
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.services.terminal_service import create_terminal

pytestmark = pytest.mark.usefixtures("isolated_memory_db", "kiro_agent_store_not_under_test")

_TS = "cli_agent_orchestrator.services.terminal_service"

# A caller row whose delegation ceiling is unrestricted keeps these tests about
# labels; test_delegation_ceiling.py covers the ceiling rule itself.
_CALLER_ROW = {"agent_profile": "supervisor", "delegate_tools": ["*"]}


@pytest.fixture
def launch(monkeypatch):
    """Every collaborator of create_terminal replaced except the backend.

    ``launch.backend`` is a MagicMock bound as the registry backend; a test may
    rebind the registry to another backend with ``monkeypatch``.
    """
    backend = MagicMock()
    backend.session_exists.return_value = False
    backend.create_window.return_value = "developer-abcd"
    monkeypatch.setattr(registry, "_backend", backend)
    provider = AsyncMock()
    provider.identity_verified = True
    provider.initialize.return_value = True
    fifo_dir = MagicMock()
    fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")
    with ExitStack() as stack:

        def enter(target, **kwargs):
            return stack.enter_context(patch(f"{_TS}.{target}", **kwargs))

        enter("generate_terminal_id", return_value="test1234")
        enter("generate_session_name", return_value="cao-session")
        enter("generate_window_name", return_value="developer-abcd")
        enter(
            "agent_profiles.load_agent_profile",
            return_value=AgentProfile(name="developer", description="Developer"),
        )
        enter("get_terminal_metadata", return_value=_CALLER_ROW)
        enter("db_create_terminal")
        enter("delete_terminals_by_session")
        enter("status_monitor")
        enter("fifo_manager")
        enter("FIFO_DIR", new=fifo_dir)
        manager = enter("provider_manager")
        manager.create_provider.return_value = provider
        yield SimpleNamespace(backend=backend)


def _new_session_labels(launch):
    return launch.backend.create_session.call_args.kwargs["extra_labels"]


def _add_window_labels(launch):
    return launch.backend.create_window.call_args.kwargs["extra_labels"]


class TestNewSession:
    @pytest.mark.asyncio
    async def test_root_terminal_gets_its_profile_and_no_caller(self, launch):
        await create_terminal("kiro_cli", "developer", new_session=True)
        assert _new_session_labels(launch) == {"cao.profile": "developer"}

    @pytest.mark.asyncio
    async def test_worker_gets_profile_and_caller(self, launch):
        await create_terminal("kiro_cli", "reviewer", new_session=True, caller_id="abcd1234")
        assert _new_session_labels(launch) == {
            "cao.profile": "reviewer",
            "cao.caller": "abcd1234",
        }

    @pytest.mark.asyncio
    async def test_empty_caller_is_a_root(self, launch):
        await create_terminal("kiro_cli", "developer", new_session=True, caller_id="")
        assert _new_session_labels(launch) == {"cao.profile": "developer"}

    @pytest.mark.asyncio
    async def test_empty_profile_writes_no_profile_label(self, launch):
        """Assumed: no profile name means no cao.profile key, and no empty value either.

        Passes before the change (no labels at all); it guards the helper against
        writing ``{"cao.profile": ""}``. A ``None`` profile never gets this far:
        create_terminal rejects it first.
        """
        await create_terminal("kiro_cli", "", new_session=True)
        sent = launch.backend.create_session.call_args.kwargs.get("extra_labels")
        assert not sent

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "profile",
        [
            "code supervisor",
            "\u00fcn\u00ef-\u00e7\u00f8d\u00e9",
            "a" * 200,
            "x=y,z",
            "dev\u202eeloper",
        ],
        ids=["spaces", "unicode", "very-long", "separators", "bidi-control"],
    )
    async def test_profile_name_is_passed_verbatim(self, launch, profile):
        await create_terminal("kiro_cli", profile, new_session=True)
        assert _new_session_labels(launch) == {"cao.profile": profile}

    @pytest.mark.asyncio
    async def test_the_other_arguments_of_the_backend_call_are_unchanged(self, launch):
        await create_terminal("kiro_cli", "developer", new_session=True)
        args = launch.backend.create_session.call_args.args
        assert args[:3] == ("cao-session", "developer-abcd", "test1234")
        launch.backend.create_window.assert_not_called()


class TestAddWindow:
    @pytest.mark.asyncio
    async def test_root_window_gets_its_profile_and_no_caller(self, launch):
        launch.backend.session_exists.return_value = True
        await create_terminal("kiro_cli", "developer", session_name="cao-existing")
        assert _add_window_labels(launch) == {"cao.profile": "developer"}

    @pytest.mark.asyncio
    async def test_worker_window_gets_profile_and_caller(self, launch):
        launch.backend.session_exists.return_value = True
        await create_terminal(
            "kiro_cli", "reviewer", session_name="cao-existing", caller_id="abcd1234"
        )
        assert _add_window_labels(launch) == {
            "cao.profile": "reviewer",
            "cao.caller": "abcd1234",
        }

    @pytest.mark.asyncio
    async def test_empty_caller_is_a_root(self, launch):
        launch.backend.session_exists.return_value = True
        await create_terminal("kiro_cli", "developer", session_name="cao-existing", caller_id="")
        assert _add_window_labels(launch) == {"cao.profile": "developer"}

    @pytest.mark.asyncio
    async def test_the_other_arguments_of_the_backend_call_are_unchanged(self, launch):
        launch.backend.session_exists.return_value = True
        await create_terminal("kiro_cli", "developer", session_name="cao-existing")
        args = launch.backend.create_window.call_args.args
        assert args[:3] == ("cao-existing", "developer-abcd", "test1234")
        launch.backend.create_session.assert_not_called()


class TestLabelsReachTerminalRun:
    """The same call chain on a real HerdrAppBackend: what the core is asked to run."""

    @pytest.fixture
    def core(self, launch, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.mcp_resolution.cao_server_env",
            lambda: {"CAO_API": "http://x"},
        )
        hc = FakeHc()
        monkeypatch.setattr(
            registry, "_backend", HerdrAppBackend(herdr_app_session="cao", client=hc)
        )
        return SimpleNamespace(hc=hc, cwd=str(tmp_path))

    @staticmethod
    def _labels(core):
        return core.hc.calls_to("terminal.run")[-1]["labels"]

    @pytest.mark.asyncio
    async def test_session_launch_of_a_root_terminal(self, core):
        """Spec: POST /sessions with agent_profile=conductor and no caller."""
        await create_terminal("kiro_cli", "conductor", new_session=True, working_directory=core.cwd)
        labels = self._labels(core)
        assert labels["cao.profile"] == "conductor"
        assert "cao.caller" not in labels
        assert {k: v for k, v in labels.items() if k.startswith("cao.")} == {
            "cao.managed": "1",
            "cao.home": labels["cao.home"],
            "cao.terminal_id": "test1234",
            "cao.session": "cao-session",
            "cao.window": "developer-abcd",
            "cao.profile": "conductor",
        }
        assert set(labels) >= {"herdr.space", "herdr.tab"}

    @pytest.mark.asyncio
    async def test_added_window_of_a_worker(self, core):
        """Spec: caller_id=abcd1234 and agent_profile=reviewer."""
        registry._backend.create_session("cao-existing", "root", "root0001", core.cwd)
        await create_terminal(
            "kiro_cli",
            "reviewer",
            session_name="cao-existing",
            caller_id="abcd1234",
            working_directory=core.cwd,
        )
        labels = self._labels(core)
        assert labels["cao.profile"] == "reviewer"
        assert labels["cao.caller"] == "abcd1234"
        assert labels["cao.session"] == "cao-existing"
        assert labels["cao.terminal_id"] == "test1234"
        root = core.hc.calls_to("terminal.run")[0]["labels"]
        assert labels["herdr.space"] == root["herdr.space"]
