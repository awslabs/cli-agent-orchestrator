"""Pre-task bound identity on the UNMANAGED
new-terminal path.

The ordinary ``create_terminal`` path consumes a pre-task bound identity
contract for the activated cells:

- Claude Code: caller-minted UUID, bound before provider start, consumed by
  ``--session-id <id>``.
- Codex: zero-turn app-server bootstrap with the SAME canonical cwd and the
  same composed profile/route material (profile model + effective
  ``model_reasoning_effort``) the resumed TUI consumes, then ``resume <id>``.

Repair invariants covered here:

- Identity resolution + roster binding are ONE cancellation-owned
  operation — cancellation cannot outlive teardown or lose a minted id; a
  cancelled launch whose bind committed retires the exact incarnation
  RETAINING the minted id, with zero task bytes.
- For an activated cell, executable/version/bootstrap/refusal failure
  FAILS the new launch (zero provider initialization, zero task bytes, no
  live roster incarnation) — never a silent identity_missing fallback.
- One canonical effective working directory (None / symlink alias /
  explicit canonical path) consumed by both pane launch and bootstrap;
  profile-derived model/effort with explicit expectations winning.
- Kimi: NOT activated (verified blocker) — the ordinary Kimi path is
  byte-for-byte unchanged and the pre-task capability does not advertise it.
- Integration-shaped event-order oracles for Claude and Codex:
  bootstrap complete -> roster ID durable -> provider initialize -> initial
  task, with the exact minted id in both the roster and the launch argv.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import threading
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.services import stable_agent_roster as roster
from cli_agent_orchestrator.services.terminal_service import (
    TerminalInputRefusedError,
    create_terminal,
)


@pytest.fixture(autouse=True)
def _patch_clear_session_env():
    """Stub the strict new-session pre-clear."""
    with patch("cli_agent_orchestrator.services.terminal_service.clear_session_env"):
        yield


@pytest.fixture
def launch_mocks(monkeypatch):
    """Full create_terminal mock stack with a durable-persisted row."""
    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.services import terminal_service as ts

    with (
        patch("cli_agent_orchestrator.services.terminal_service.status_monitor") as status_monitor,
        patch("cli_agent_orchestrator.services.terminal_service.fifo_manager") as fifo_manager,
        patch("cli_agent_orchestrator.services.terminal_service.FIFO_DIR") as fifo_dir,
        patch(
            "cli_agent_orchestrator.services.terminal_service.provider_manager"
        ) as provider_manager,
        patch(
            "cli_agent_orchestrator.services.terminal_service.db_create_terminal",
            side_effect=database.create_terminal,
        ) as db_create_terminal,
        patch("cli_agent_orchestrator.backends.registry._backend") as tmux,
        patch(
            "cli_agent_orchestrator.services.terminal_service.generate_window_name"
        ) as gen_window,
        patch(
            "cli_agent_orchestrator.services.terminal_service.generate_session_name"
        ) as gen_session,
        patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id") as gen_id,
        patch(
            "cli_agent_orchestrator.services.terminal_service.load_agent_profile"
        ) as load_profile,
    ):
        # Unique terminal ids per create_terminal call (the real DB row write
        # rejects a duplicate id); the first stays "test1234" for assertions.
        gen_id.side_effect = [f"test{i}234" for i in range(1, 9)]
        gen_session.return_value = "cao-session"
        gen_window.return_value = "developer-abcd"
        tmux.session_exists.return_value = False
        tmux.window_identity.return_value = {
            "pane_id": "%88",
            "window_id": "w88",
            "server_socket_path": "/tmp/cao-roster.sock",
            "session_id": "1",
            "pane_pid": 4321,
        }
        load_profile.return_value = AgentProfile(name="developer", description="Developer")
        fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")
        mock_provider = AsyncMock()
        mock_provider.initialize.return_value = True
        provider_manager.create_provider.return_value = mock_provider
        monkeypatch.setattr(
            ts,
            "get_terminal_metadata",
            lambda terminal_id, **_kwargs: {"provider": "claude_code"},
        )
        yield {
            "tmux": tmux,
            "provider_manager": provider_manager,
            "db_create_terminal": db_create_terminal,
        }


def _codex_seam_harness(
    monkeypatch, *, native_id, profile=None, model="gpt-5.6-sol", effort="xhigh"
):
    """Deterministic codex seam: resolve executable/digest/version, load the
    given profile (or a default), and stub the bootstrap mint."""
    import cli_agent_orchestrator.services.unmanaged_native_identity as seam

    if profile is None:
        profile = AgentProfile(
            name="developer",
            description="Developer",
            model=model,
            codexConfig={"model_reasoning_effort": effort},
        )
    monkeypatch.setattr(seam, "_resolve_executable", lambda p: "/fake/bin/codex")
    monkeypatch.setattr(seam, "_binary_sha256", lambda p: "a" * 64)
    monkeypatch.setattr(seam, "_version_output", lambda p, e, env: "0.146.0")
    # create_terminal builds the profile material from the profile IT loads
    # (the terminal_service import site), so the rich profile must reach it —
    # not the bare launch_mocks default.
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.load_agent_profile",
        lambda name: profile,
    )
    captured = {}
    # The first mint returns the fixed id tests assert against; subsequent
    # mints (e.g. a cwd test that launches twice) return fresh ids so two
    # launches never collide on the same native session in the roster.
    _id_iter = itertools.chain([native_id], (str(uuid.uuid4()) for _ in itertools.count()))

    def _mint(**kwargs):
        captured["kwargs"] = kwargs
        # Echo the effective route back as the observed actual route, the way
        # the real bootstrap receipt does (model always reported; effort only
        # when the route selected one).
        return {
            "native_session_id": next(_id_iter),
            "model": kwargs.get("model"),
            "effort": kwargs.get("effort"),
        }

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.codex_native_bootstrap.mint_session", _mint
    )
    return captured


# ---------------------------------------------------------------------------
# Activated cells: Claude golden path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unmanaged_claude_new_launch_binds_pre_task_identity(
    isolated_memory_db, launch_mocks, monkeypatch
):
    """Claude: the caller-minted id is durable in the roster (chosen, never
    identity_missing) before the terminal is returned."""
    result = await create_terminal("claude_code", "developer", new_session=True)
    assert result.id == "test1234"
    agent = roster.get_agent(roster.derive_initial_agent_id("test1234"))
    lineage = agent["current_lineage"]
    assert lineage["native_session_id"] is not None
    assert lineage["harness"] == "claude_code"
    assert lineage["acquisition_method"] == roster.ACQUISITION_CHOSEN_SESSION_ID
    assert str(uuid.UUID(lineage["native_session_id"])) == lineage["native_session_id"]
    from cli_agent_orchestrator.clients import database

    assert (
        database.get_terminal_metadata("test1234")["native_session_id"]
        == lineage["native_session_id"]
    )


# ---------------------------------------------------------------------------
# Activated cells: Codex golden path + profile-derived route + canonical cwd
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unmanaged_codex_new_launch_binds_profile_route(
    isolated_memory_db, launch_mocks, monkeypatch, tmp_path
):
    """Codex: the zero-turn bootstrap id is durable in the roster, minted in
    the SAME canonical working directory the pane uses, with the
    profile-derived route (profile.model + codexConfig.model_reasoning_effort)
    — no empty or provider-default route."""
    native_id = str(uuid.uuid4())
    captured = _codex_seam_harness(monkeypatch, native_id=native_id)
    result = await create_terminal(
        "codex", "developer", new_session=True, working_directory=str(tmp_path)
    )
    assert result.id == "test1234"
    agent = roster.get_agent(roster.derive_initial_agent_id("test1234"))
    lineage = agent["current_lineage"]
    assert lineage["native_session_id"] == native_id
    assert lineage["harness"] == "codex"
    assert lineage["acquisition_method"] == roster.ACQUISITION_ZERO_TURN_BOOTSTRAP
    from cli_agent_orchestrator.clients import database

    assert database.get_terminal_metadata("test1234")["native_session_id"] == native_id
    # The bootstrap consumed the canonical cwd and the profile route.
    assert captured["kwargs"]["working_directory"] == os.path.realpath(str(tmp_path))
    assert captured["kwargs"]["model"] == "gpt-5.6-sol"
    assert captured["kwargs"]["effort"] == "xhigh"
    assert captured["kwargs"]["profile_args"][0:2] == ["--yolo", "-c"]
    joined = " ".join(captured["kwargs"]["profile_args"])
    # The canonical trust override uses the projects={...trust_level="trusted"}
    # renderer (the SAME one the resumed TUI consumes), NOT trusted_project=.
    canonical = os.path.realpath(str(tmp_path))
    assert f"projects={{{canonical}" in joined.replace('"', "") or canonical in joined
    assert 'trust_level="trusted"' in joined
    assert "trusted_project=" not in joined
    assert "--model gpt-5.6-sol" in joined
    assert 'model_reasoning_effort="xhigh"' in joined
    # The sealed route is emitted LAST so it cannot be silently replaced.
    assert captured["kwargs"]["profile_args"][-4:] == [
        "--model",
        "gpt-5.6-sol",
        "-c",
        'model_reasoning_effort="xhigh"',
    ]
    # The pane window was created in the SAME canonical cwd.
    launch_mocks["tmux"].create_session.assert_called_once()
    assert launch_mocks["tmux"].create_session.call_args.args[3] == os.path.realpath(str(tmp_path))
    pane_env = launch_mocks["tmux"].create_session.call_args.kwargs["extra_env"]
    assert pane_env["CODEX_HOME"] == captured["kwargs"]["environment"]["CODEX_HOME"]
    from cli_agent_orchestrator.services.session_env import get_session_env

    assert get_session_env("cao-session")["CODEX_HOME"] == pane_env["CODEX_HOME"]


@pytest.mark.asyncio
async def test_unmanaged_codex_canonical_cwd_none_and_symlink(
    isolated_memory_db, launch_mocks, monkeypatch, tmp_path
):
    """None and a symlink alias both resolve to the same canonical
    cwd used by the pane and the bootstrap."""
    native_id = str(uuid.uuid4())
    captured = _codex_seam_harness(monkeypatch, native_id=native_id)

    # None resolves to the canonical current directory.
    await create_terminal("codex", "developer", new_session=True)
    none_cwd = captured["kwargs"]["working_directory"]
    assert none_cwd == os.path.realpath(os.getcwd())
    launch_mocks["tmux"].create_session.reset_mock()

    # A symlink alias resolves to its real path.
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real_dir)
    await create_terminal("codex", "developer", new_session=True, working_directory=str(link))
    assert captured["kwargs"]["working_directory"] == os.path.realpath(str(real_dir))
    assert launch_mocks["tmux"].create_session.call_args.args[3] == os.path.realpath(str(real_dir))


@pytest.mark.asyncio
async def test_unmanaged_codex_explicit_route_wins(
    isolated_memory_db, launch_mocks, monkeypatch, tmp_path
):
    """An explicit expected model/effort wins over the profile route."""
    native_id = str(uuid.uuid4())
    captured = _codex_seam_harness(
        monkeypatch, native_id=native_id, model="profile-model", effort="high"
    )
    await create_terminal(
        "codex",
        "developer",
        new_session=True,
        working_directory=str(tmp_path),
        expected_model="explicit-model",
        expected_effort="max",
    )
    assert captured["kwargs"]["model"] == "explicit-model"
    assert captured["kwargs"]["effort"] == "max"
    assert "--model" in captured["kwargs"]["profile_args"]
    assert (
        captured["kwargs"]["profile_args"][captured["kwargs"]["profile_args"].index("--model") + 1]
        == "explicit-model"
    )


@pytest.mark.asyncio
async def test_unmanaged_codex_codexconfig_model_beats_profile_model(
    isolated_memory_db, launch_mocks, monkeypatch, tmp_path
):
    """Conflicting values: for an ordinary profile route, the profile's
    ``codexConfig.model`` override wins over the bare ``profile.model``
    field, and a caller-sealed expected model still wins over both — the
    same precedence the resumed TUI consumes."""
    native_id = str(uuid.uuid4())
    profile = AgentProfile(
        name="developer",
        description="Developer",
        model="profile-model",
        codexConfig={"model": "config-model", "model_reasoning_effort": "high"},
    )
    captured = _codex_seam_harness(monkeypatch, native_id=native_id, profile=profile)

    # No sealed expectation: the codexConfig.model override wins.
    await create_terminal("codex", "developer", new_session=True, working_directory=str(tmp_path))
    route_args = captured["kwargs"]["profile_args"]
    assert route_args[route_args.index("--model") + 1] == "config-model"
    assert captured["kwargs"]["model"] == "config-model"


@pytest.mark.asyncio
async def test_unmanaged_codex_http_mcp_profile_launches(
    isolated_memory_db, launch_mocks, monkeypatch, tmp_path
):
    """A command-less URL/streamable-HTTP MCP server entry (type: http with
    url + bearer_token_env_var) resolves through the ordinary pre-task path
    without a raw KeyError: the bootstrap and the resumed TUI both consume
    the exact Codex url/bearer overrides and nothing else."""
    native_id = str(uuid.uuid4())
    profile = AgentProfile(
        name="developer",
        description="Developer",
        mcpServers={
            "web": {
                "type": "http",
                "url": "https://example.invalid/mcp",
                "bearer_token_env_var": "TEST_TOKEN",
            }
        },
    )
    captured = _codex_seam_harness(monkeypatch, native_id=native_id, profile=profile)
    result = await create_terminal(
        "codex", "developer", new_session=True, working_directory=str(tmp_path)
    )
    assert result.id == "test1234"
    # The bootstrap core args carry the URL transport and nothing else.
    profile_args = captured["kwargs"]["profile_args"]
    joined = " ".join(profile_args)
    assert 'mcp_servers.web.url="https://example.invalid/mcp"' in joined
    assert 'mcp_servers.web.bearer_token_env_var="TEST_TOKEN"' in joined
    # The URL transport emits no subprocess surface.
    assert "mcp_servers.web.command" not in joined
    assert "mcp_servers.web.env" not in joined
    assert "mcp_servers.web.tool_timeout_sec" not in joined
    # The roster bind still completed with the minted identity.
    agent = roster.get_agent(roster.derive_initial_agent_id("test1234"))
    assert agent["current_lineage"]["native_session_id"] == native_id


@pytest.mark.asyncio
async def test_unmanaged_codex_malformed_mcp_fails_typed(
    isolated_memory_db, launch_mocks, monkeypatch, tmp_path
):
    """An MCP server entry with no usable transport (or two transports) is
    a typed pre-task identity refusal — zero provider initialization, never
    a raw KeyError or serializer leak."""
    native_id = str(uuid.uuid4())
    profile = AgentProfile(
        name="developer",
        description="Developer",
        mcpServers={
            "broken": {
                "type": "http",
                "command": "/usr/bin/env",
                "url": "https://example.invalid/mcp",
            }
        },
    )
    _codex_seam_harness(monkeypatch, native_id=native_id, profile=profile)
    from cli_agent_orchestrator.services import unmanaged_native_identity

    with pytest.raises(
        unmanaged_native_identity.UnmanagedIdentityUnavailable,
        match="exactly one usable transport",
    ):
        await create_terminal(
            "codex", "developer", new_session=True, working_directory=str(tmp_path)
        )
    launch_mocks["provider_manager"].create_provider.assert_not_called()


@pytest.mark.asyncio
async def test_unmanaged_codex_sealed_model_beats_codexconfig_and_profile(
    isolated_memory_db, launch_mocks, monkeypatch, tmp_path
):
    """The caller-sealed expected model wins over both the codexConfig
    override and the profile model on the bootstrap side too."""
    native_id = str(uuid.uuid4())
    profile = AgentProfile(
        name="developer",
        description="Developer",
        model="profile-model",
        codexConfig={"model": "config-model", "model_reasoning_effort": "high"},
    )
    captured = _codex_seam_harness(monkeypatch, native_id=native_id, profile=profile)

    await create_terminal(
        "codex",
        "developer",
        new_session=True,
        working_directory=str(tmp_path),
        expected_model="sealed-model",
        expected_effort="max",
    )
    route_args = captured["kwargs"]["profile_args"]
    assert route_args[route_args.index("--model") + 1] == "sealed-model"
    assert captured["kwargs"]["model"] == "sealed-model"
    assert captured["kwargs"]["effort"] == "max"


def test_codex_bootstrap_environment_uses_the_pane_filter(monkeypatch):
    """The shared CODEX_HOME and allowed explicit values win identically;
    unrelated blocked/oversized values are absent."""
    from cli_agent_orchestrator.clients.tmux import TmuxClient
    from cli_agent_orchestrator.services import unmanaged_native_identity as seam

    monkeypatch.setenv("CODEX_HOME", "/ambient/codex")
    monkeypatch.setenv("AWS_REGION", "ambient-region")
    overlay = {
        "CODEX_HOME": "/forwarded/codex",
        "AWS_REGION": "explicit-region",
        "SMALL": "kept",
        "BIG": "x" * 2048,
    }
    pane = TmuxClient._filtered_child_environment(overlay, terminal_id="test1234")
    bootstrap = seam._bootstrap_environment(
        terminal_id="test1234",
        session_name="cao-session",
        forwarded_environment=overlay,
    )

    assert pane["CODEX_HOME"] == bootstrap["CODEX_HOME"] == "/forwarded/codex"
    assert pane["AWS_REGION"] == bootstrap["AWS_REGION"] == "explicit-region"
    assert pane["SMALL"] == bootstrap["SMALL"] == "kept"
    assert "BIG" not in pane and "BIG" not in bootstrap
    assert bootstrap == {**pane, "CAO_SESSION_NAME": "cao-session"}


def test_captured_marker_without_native_id_refuses_input(isolated_memory_db):
    """A corrupt/partial captured marker is never mistaken for readiness."""
    from cli_agent_orchestrator.services import unmanaged_native_identity as seam

    terminal_id = "partial1"
    generation = "gen-1"
    roster.bind_generation(
        roster.BindingContract(
            agent_id=roster.derive_initial_agent_id(terminal_id, generation),
            session_name="cao-session",
            role=roster.ROLE_WORKER,
            profile_family="developer",
            harness="codex",
            terminal_id=terminal_id,
            generation=generation,
            pane_id="%71",
            pane_pid=7171,
            execution_mode="native_tui",
            continuity_note=seam.PRE_TASK_IDENTITY_CAPTURED,
        )
    )

    with pytest.raises(roster.StableAgentAdmissionRefused, match="reaches its ready state"):
        seam.assert_unmanaged_admission_ready(
            terminal_id,
            {"provider": "codex", "generation": generation},
        )


def test_legacy_identity_missing_row_remains_compatible(isolated_memory_db):
    """An unmarked pre-existing row is not retroactively bricked."""
    from cli_agent_orchestrator.services import unmanaged_native_identity as seam

    terminal_id = "legacy01"
    roster.bind_generation(
        roster.BindingContract(
            agent_id=roster.derive_initial_agent_id(terminal_id),
            session_name="cao-session",
            role=roster.ROLE_WORKER,
            profile_family="developer",
            harness="codex",
            terminal_id=terminal_id,
            pane_id="%72",
            pane_pid=7272,
            execution_mode="native_tui",
        )
    )

    seam.assert_unmanaged_admission_ready(terminal_id, {"provider": "codex"})


@pytest.mark.asyncio
async def test_unmanaged_codex_rejects_divergent_trust_root_before_pane(
    isolated_memory_db, launch_mocks, monkeypatch, tmp_path
):
    native_id = str(uuid.uuid4())
    captured = _codex_seam_harness(monkeypatch, native_id=native_id)
    workdir = tmp_path / "work"
    trustdir = tmp_path / "trust"
    workdir.mkdir()
    trustdir.mkdir()

    with pytest.raises(ValueError, match="one contract"):
        await create_terminal(
            "codex",
            "developer",
            new_session=True,
            working_directory=str(workdir),
            trusted_project_root=str(trustdir),
        )

    launch_mocks["tmux"].create_session.assert_not_called()
    assert "kwargs" not in captured


@pytest.mark.asyncio
async def test_unmanaged_codex_rejects_relative_store_before_pane(
    isolated_memory_db, launch_mocks, monkeypatch, tmp_path
):
    native_id = str(uuid.uuid4())
    captured = _codex_seam_harness(monkeypatch, native_id=native_id)

    with pytest.raises(ValueError, match="CODEX_HOME must be an absolute path"):
        await create_terminal(
            "codex",
            "developer",
            new_session=True,
            working_directory=str(tmp_path),
            env_vars={"CODEX_HOME": "relative-store"},
        )

    launch_mocks["tmux"].create_session.assert_not_called()
    assert "kwargs" not in captured


@pytest.mark.asyncio
async def test_terminal_row_native_id_refusal_retires_pending_roster_before_provider(
    isolated_memory_db, launch_mocks, monkeypatch
):
    """The terminal row and roster cannot publish different native IDs."""
    from cli_agent_orchestrator.services import terminal_service as ts
    from cli_agent_orchestrator.services import unmanaged_native_identity as seam

    monkeypatch.setattr(ts, "set_terminal_native_session_id", lambda *_args: False)
    with pytest.raises(
        seam.UnmanagedIdentityUnavailable,
        match="refused its pre-task native-session bind",
    ):
        await create_terminal("claude_code", "developer", new_session=True)

    incarnations = roster.list_incarnations()
    assert len(incarnations) == 1
    assert incarnations[0]["disposition"] == roster.INCARNATION_RETIRED
    agent = roster.get_agent(roster.derive_initial_agent_id("test1234"))
    assert agent["disposition"] == roster.DISPOSITION_DORMANT
    assert agent["current_lineage"]["native_session_id"] is None
    launch_mocks["provider_manager"].create_provider.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_direct_input_is_refused_until_native_bind(
    isolated_memory_db, launch_mocks, monkeypatch
):
    """The visible terminal row is not an admission token: while pre-task
    identity is blocked, the real send_input sink proves zero task bytes."""
    from cli_agent_orchestrator.services import terminal_service as ts
    from cli_agent_orchestrator.services import unmanaged_native_identity as seam

    entered = threading.Event()
    release = threading.Event()
    original = seam.resolve_pre_task_identity

    def _blocked_resolve(**kwargs):
        entered.set()
        assert release.wait(timeout=10)
        return original(**kwargs)

    monkeypatch.setattr(seam, "resolve_pre_task_identity", _blocked_resolve)
    task = asyncio.create_task(create_terminal("claude_code", "developer", new_session=True))
    for _ in range(400):
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert entered.is_set()

    with pytest.raises(TerminalInputRefusedError) as excinfo:
        await asyncio.to_thread(ts.send_input, "test1234", "echo should-not-run")
    assert excinfo.value.reason_code == "lineage-unproven"
    launch_mocks["tmux"].send_keys.assert_not_called()

    release.set()
    result = await task
    assert result.id == "test1234"


def test_row_visible_before_roster_bind_refuses_input(isolated_memory_db):
    """The row-visible/pre-marker window: a newly created activated
    terminal row is addressable before the roster marker commits.  The row
    itself must visibly carry the pending state in its dedicated
    ``pre_task_identity_state`` column at first durable visibility — with
    ``native_session_id`` still NULL — so a direct-input or control-input
    call in that window returns the typed lineage refusal instead of being
    treated as a legacy row."""
    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.services import unmanaged_native_identity as seam

    terminal_id = "pending01"
    database.create_terminal(
        terminal_id,
        "cao-session",
        "developer-abcd",
        "claude_code",
        generation="gen-1",
        pane_id="%61",
        pane_pid=6161,
        pre_task_identity_state=seam.PRE_TASK_IDENTITY_PENDING,
    )
    # The state marker never impersonates a session: the real provider id
    # is still NULL during the pre-marker barrier.
    row = database.get_terminal_metadata(terminal_id)
    assert row["native_session_id"] is None
    assert row["pre_task_identity_state"] == seam.PRE_TASK_IDENTITY_PENDING
    # No roster row exists yet — the pre-task bind thread has not committed.
    # The admission gate reads the state from the carried metadata (the
    # direct lane passes the full row it read); it never re-queries.
    with pytest.raises(roster.StableAgentAdmissionRefused, match="pending"):
        seam.assert_unmanaged_admission_ready(
            terminal_id,
            {
                "provider": "claude_code",
                "generation": "gen-1",
                "pre_task_identity_state": seam.PRE_TASK_IDENTITY_PENDING,
            },
        )


def test_row_without_pending_marker_keeps_legacy_exemption(isolated_memory_db):
    """A row born without the new-launch marker stays legacy-compatible even
    when it names no roster row (pre-deploy rows never saw the marker)."""
    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.services import unmanaged_native_identity as seam

    terminal_id = "legacy02"
    database.create_terminal(
        terminal_id,
        "cao-session",
        "developer-abcd",
        "claude_code",
        pane_id="%62",
        pane_pid=6262,
    )
    seam.assert_unmanaged_admission_ready(terminal_id, {"provider": "claude_code"})


def test_legacy_repaired_row_with_real_id_keeps_exemption(isolated_memory_db):
    """A legacy row whose repair seam later wrote a real native id but whose
    dedicated state column stayed NULL keeps the compatibility exemption —
    the state column, not the id column, decides legacy status."""
    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.services import unmanaged_native_identity as seam

    terminal_id = "legacy03"
    database.create_terminal(
        terminal_id,
        "cao-session",
        "developer-abcd",
        "claude_code",
        pane_id="%63",
        pane_pid=6363,
        native_session_id="019fb17d-0c6d-7161-a408-6b1fa61c8f2d",
    )
    seam.assert_unmanaged_admission_ready(terminal_id, {"provider": "claude_code"})


def test_row_state_ready_without_roster_ready_fails_closed(isolated_memory_db):
    """Conflicting state fails closed: a row that reached ready while its
    roster lineage is still in-flight must not be admitted."""
    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.services import unmanaged_native_identity as seam

    terminal_id = "conflict1"
    generation = "gen-1"
    native_id = "019fb17d-0c6d-7161-a408-6b1fa61c8f2d"
    database.create_terminal(
        terminal_id,
        "cao-session",
        "developer-abcd",
        "claude_code",
        generation=generation,
        pane_id="%64",
        pane_pid=6464,
        native_session_id=native_id,
        pre_task_identity_state=seam.PRE_TASK_IDENTITY_READY,
    )
    roster.bind_generation(
        roster.BindingContract(
            agent_id=roster.derive_initial_agent_id(terminal_id, generation),
            session_name="cao-session",
            role=roster.ROLE_WORKER,
            profile_family="developer",
            harness="claude_code",
            native_session_id=native_id,
            acquisition_method=roster.ACQUISITION_CHOSEN_SESSION_ID,
            terminal_id=terminal_id,
            generation=generation,
            pane_id="%64",
            pane_pid=6464,
            execution_mode="native_tui",
            continuity_note=seam.PRE_TASK_IDENTITY_CAPTURED,
        )
    )
    with pytest.raises(roster.StableAgentAdmissionRefused, match="reaches its ready state"):
        seam.assert_unmanaged_admission_ready(
            terminal_id,
            {
                "provider": "claude_code",
                "generation": generation,
                "pre_task_identity_state": seam.PRE_TASK_IDENTITY_READY,
            },
        )


def test_roster_pending_blocks_ready_transition(isolated_memory_db):
    """The captured identity boundary cannot be skipped on the roster
    surface: a lineage still marked pending refuses the ready transition,
    and the row state is left untouched (still pending)."""
    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.services import unmanaged_native_identity as seam

    terminal_id = "pendingroster"
    generation = "gen-1"
    database.create_terminal(
        terminal_id,
        "cao-session",
        "developer-abcd",
        "claude_code",
        generation=generation,
        pane_id="%65",
        pane_pid=6565,
        pre_task_identity_state=seam.PRE_TASK_IDENTITY_PENDING,
    )
    roster.bind_generation(
        roster.BindingContract(
            agent_id=roster.derive_initial_agent_id(terminal_id, generation),
            session_name="cao-session",
            role=roster.ROLE_WORKER,
            profile_family="developer",
            harness="claude_code",
            terminal_id=terminal_id,
            generation=generation,
            pane_id="%65",
            pane_pid=6565,
            execution_mode="native_tui",
            continuity_note=seam.PRE_TASK_IDENTITY_PENDING,
        )
    )
    with pytest.raises(roster.StableAgentConflict, match="only a lineage marked"):
        seam.mark_pre_task_identity_ready(terminal_id=terminal_id, generation=generation)
    # The refused transition changed nothing on either surface.
    assert database.get_terminal_metadata(terminal_id)["pre_task_identity_state"] == (
        seam.PRE_TASK_IDENTITY_PENDING
    )
    agent = roster.get_agent(roster.derive_initial_agent_id(terminal_id, generation))
    assert agent["current_lineage"]["continuity_note"] == seam.PRE_TASK_IDENTITY_PENDING


@pytest.mark.asyncio
async def test_concurrent_direct_input_refused_before_roster_marker_commits(
    isolated_memory_db, launch_mocks, monkeypatch
):
    """End-to-end reproduction: block the very first roster write (the
    bind that publishes the pending marker) and prove the visible row is
    fail-closed — the concurrent direct-input call is refused with the typed
    lineage refusal and zero pane writes."""
    from cli_agent_orchestrator.services import stable_agent_roster as real_roster
    from cli_agent_orchestrator.services import terminal_service as ts
    from cli_agent_orchestrator.services import unmanaged_native_identity as seam

    entered = threading.Event()
    release = threading.Event()
    original_bind = real_roster.bind_generation

    def _blocked_bind(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=10)
        return original_bind(*args, **kwargs)

    monkeypatch.setattr(real_roster, "bind_generation", _blocked_bind)
    task = asyncio.create_task(create_terminal("claude_code", "developer", new_session=True))
    for _ in range(400):
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert entered.is_set(), "the pre-task roster bind never started"

    # The pre-marker barrier: the row's dedicated state is pending and the
    # real native session id is still NULL — a state string never occupies
    # the session-id column.  The direct lane's metadata carries the state
    # the way the real metadata read would.
    from cli_agent_orchestrator.clients import database

    row = database.get_terminal_metadata("test1234")
    assert row["pre_task_identity_state"] == seam.PRE_TASK_IDENTITY_PENDING
    assert row["native_session_id"] is None
    monkeypatch.setattr(
        ts,
        "get_terminal_metadata",
        lambda terminal_id, **_kwargs: {
            "provider": "claude_code",
            "pre_task_identity_state": seam.PRE_TASK_IDENTITY_PENDING,
        },
    )

    with pytest.raises(TerminalInputRefusedError) as excinfo:
        await asyncio.to_thread(ts.send_input, "test1234", "echo should-not-run")
    assert excinfo.value.reason_code == "lineage-unproven"
    launch_mocks["tmux"].send_keys.assert_not_called()

    release.set()
    result = await task
    assert result.id == "test1234"


@pytest.mark.asyncio
async def test_captured_identity_stays_gated_until_provider_ready(
    isolated_memory_db, launch_mocks, monkeypatch, tmp_path
):
    """After native-ID capture the roster must NOT be
    admissible while the provider is still warming up and has not launched
    the resumed TUI.  Direct input in the post-capture/pre-init window is
    refused with zero pane writes; once provider initialization succeeds and
    the readiness marker transitions, the same input lane is admitted."""
    from cli_agent_orchestrator.services import terminal_service as ts
    from cli_agent_orchestrator.services import unmanaged_native_identity as seam

    native_id = str(uuid.uuid4())
    _codex_seam_harness(monkeypatch, native_id=native_id)

    entered = threading.Event()
    release = threading.Event()

    def _blocked_provider(*args, **kwargs):
        instance = MagicMock()
        instance.shell_baseline = None

        async def _blocked_initialize():
            entered.set()
            # Yield to the loop: initialize() is awaited on the event loop,
            # so a blocking wait here would freeze the test body too.
            while not release.is_set():
                await asyncio.sleep(0.01)
            return True

        instance.initialize = _blocked_initialize
        return instance

    launch_mocks["provider_manager"].create_provider.side_effect = _blocked_provider
    task = asyncio.create_task(
        create_terminal("codex", "developer", new_session=True, working_directory=str(tmp_path))
    )
    for _ in range(400):
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert entered.is_set(), "provider initialization never started"

    # The identity is captured (real id on the row, captured row state and
    # roster note) but the provider/TUI is not ready: input must be refused
    # with zero bytes.
    from cli_agent_orchestrator.clients import database

    row = database.get_terminal_metadata("test1234")
    assert row["native_session_id"] == native_id
    assert row["pre_task_identity_state"] == seam.PRE_TASK_IDENTITY_CAPTURED
    agent = roster.get_agent(roster.derive_initial_agent_id("test1234"))
    assert agent["current_lineage"]["continuity_note"] == seam.PRE_TASK_IDENTITY_CAPTURED

    with pytest.raises(TerminalInputRefusedError) as excinfo:
        await asyncio.to_thread(ts.send_input, "test1234", "echo should-not-run")
    assert excinfo.value.reason_code == "lineage-unproven"
    launch_mocks["tmux"].send_keys.assert_not_called()

    release.set()
    result = await task
    assert result.id == "test1234"

    # Provider/TUI initialization succeeded: both the roster lineage and the
    # row state transition to ready and the same input lane is admitted
    # (task bytes flow).
    agent = roster.get_agent(roster.derive_initial_agent_id("test1234"))
    assert agent["current_lineage"]["continuity_note"] == seam.PRE_TASK_IDENTITY_READY
    row = database.get_terminal_metadata("test1234")
    assert row["pre_task_identity_state"] == seam.PRE_TASK_IDENTITY_READY
    assert row["native_session_id"] == native_id
    # The direct lane reads the real row: provider, session names, and the
    # captured native id all on the durable row.
    monkeypatch.setattr(
        ts,
        "get_terminal_metadata",
        lambda terminal_id, **_kwargs: {
            "provider": "codex",
            "tmux_session": "cao-session",
            "tmux_window": "developer-abcd",
            "generation": None,
            "native_session_id": native_id,
            "pre_task_identity_state": seam.PRE_TASK_IDENTITY_READY,
        },
    )
    await asyncio.to_thread(ts.send_input, "test1234", "echo now-ok")
    assert launch_mocks["tmux"].send_keys.called


# ---------------------------------------------------------------------------
# Activated cells fail closed — no silent identity_missing fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unmanaged_codex_bootstrap_refusal_fails_launch_closed(
    isolated_memory_db, launch_mocks, monkeypatch, tmp_path
):
    """A refused bootstrap (e.g. unproven build) FAILS the new launch:
    zero provider initialization, zero task bytes, and no live roster
    incarnation."""
    import cli_agent_orchestrator.services.unmanaged_native_identity as seam

    monkeypatch.setattr(seam, "_resolve_executable", lambda p: "/fake/bin/codex")
    monkeypatch.setattr(seam, "_binary_sha256", lambda p: "a" * 64)
    monkeypatch.setattr(seam, "_version_output", lambda p, e, env: "0.999.0")

    def _refuse(**kwargs):
        raise RuntimeError("codex 0.999.0 is not a proven build for native identity")

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.codex_native_bootstrap.mint_session", _refuse
    )
    with pytest.raises(seam.UnmanagedIdentityUnavailable):
        await create_terminal(
            "codex", "developer", new_session=True, working_directory=str(tmp_path)
        )
    # Zero provider initialization: the provider manager was never asked.
    launch_mocks["provider_manager"].create_provider.assert_not_called()
    # The pending roster history is retained truthfully but cannot be live.
    incarnations = roster.list_incarnations()
    assert len(incarnations) == 1
    assert incarnations[0]["disposition"] == roster.INCARNATION_RETIRED
    # The terminal row was cleaned up (delete path invoked).
    launch_mocks["db_create_terminal"].assert_called()


@pytest.mark.asyncio
async def test_unmanaged_claude_mint_failure_emits_zero_input(
    isolated_memory_db, launch_mocks, monkeypatch
):
    """If the pre-task identity mint fails, the launch fails closed with
    zero input and no live roster incarnation."""
    from cli_agent_orchestrator.services import claude_native_launch

    def _boom():
        raise claude_native_launch.ClaudeNativeLaunchError("cannot mint")

    monkeypatch.setattr(claude_native_launch, "mint_session_id", _boom)
    with pytest.raises(claude_native_launch.ClaudeNativeLaunchError):
        await create_terminal("claude_code", "developer", new_session=True)
    incarnations = roster.list_incarnations()
    assert len(incarnations) == 1
    assert incarnations[0]["disposition"] == roster.INCARNATION_RETIRED
    launch_mocks["provider_manager"].create_provider.assert_not_called()


# ---------------------------------------------------------------------------
# Identity resolution + roster binding are one cancellation-owned operation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelled_codex_bind_retains_minted_id_retired(
    isolated_memory_db, launch_mocks, monkeypatch, tmp_path
):
    """Cancelling the launch while the off-thread identity+bind operation is
    in flight: teardown/session-lock release cannot pass the worker; once it
    commits, the teardown retires the exact incarnation RETAINING the minted
    id in a truthful retired lineage — zero task bytes, no late write."""
    import cli_agent_orchestrator.services.unmanaged_native_identity as seam

    native_id = str(uuid.uuid4())
    _codex_seam_harness(monkeypatch, native_id=native_id)
    entered = threading.Event()
    release = threading.Event()

    def _blocking_resolve(**kwargs):
        entered.set()
        release.wait(timeout=10)
        return {
            "native_session_id": native_id,
            "acquisition_method": roster.ACQUISITION_ZERO_TURN_BOOTSTRAP,
            "working_directory": os.path.realpath(str(tmp_path)),
            "model": "gpt-5.6-sol",
            "effort": "xhigh",
            "bootstrap": {},
        }

    monkeypatch.setattr(seam, "resolve_pre_task_identity", _blocking_resolve)

    task = asyncio.create_task(
        create_terminal("codex", "developer", new_session=True, working_directory=str(tmp_path))
    )
    for _ in range(400):
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert entered.is_set(), "the off-thread identity+bind never started"

    task.cancel()
    await asyncio.sleep(0.05)
    task.cancel()  # repeated cancellation while the worker settles
    await asyncio.sleep(0.05)
    assert not task.done(), "teardown/session-lock release must not pass the worker"

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()

    # The minted id is durably retained in a truthful RETIRED lineage; no
    # live incarnation, no late write.
    agent = roster.get_agent(roster.derive_initial_agent_id("test1234"))
    lineage = agent["current_lineage"]
    assert lineage["native_session_id"] == native_id
    assert agent["disposition"] == roster.DISPOSITION_DORMANT
    assert agent["current_incarnation"]["disposition"] == roster.INCARNATION_RETIRED
    await asyncio.sleep(0.1)
    assert roster.get_agent(roster.derive_initial_agent_id("test1234"))["disposition"] == (
        roster.DISPOSITION_DORMANT
    )
    launch_mocks["provider_manager"].create_provider.assert_not_called()


# ---------------------------------------------------------------------------
# Scope: Kimi is NOT activated — ordinary path unchanged, not advertised
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kimi_ordinary_path_unchanged_and_not_advertised(
    isolated_memory_db, launch_mocks, monkeypatch
):
    """Kimi is deliberately outside the activated set (verified blocker): the
    ordinary launch keeps the truthful identity_missing lineage, the legacy
    command carries NO ``--session`` flag, and the pre-task capability does
    not advertise Kimi."""
    import cli_agent_orchestrator.services.unmanaged_native_identity as seam

    assert "kimi_cli" not in seam.UNMANAGED_PRE_TASK_PROVIDERS
    result = await create_terminal("kimi_cli", "developer", new_session=True)
    assert result.id == "test1234"
    agent = roster.get_agent(roster.derive_initial_agent_id("test1234"))
    assert agent["current_lineage"]["native_session_id"] is None
    assert agent["current_lineage"]["harness"] == "kimi_cli"


def test_kimi_provider_launch_argv_unchanged():
    """The Kimi provider command is byte-for-byte unchanged from base (no
    --session injection)."""
    from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider

    provider = KimiCliProvider("t1", "cao-s", "w1")
    command = provider._build_kimi_command()
    assert "--session" not in command
    assert "kimi --yolo" in command


# ---------------------------------------------------------------------------
# P2: production-boundary event-order oracles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude_code", "codex"])
async def test_native_id_is_durable_before_provider_init_and_initial_task(
    isolated_memory_db, launch_mocks, monkeypatch, tmp_path, provider
):
    """Instrument the real bind, provider, and deferred-input boundaries.

    Unlike the former oracle, this does not insert a synthetic roster event
    after creation or rebuild a second provider from host-local profile state.
    """
    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.services import codex_native_bootstrap
    from cli_agent_orchestrator.services import terminal_service as ts
    from cli_agent_orchestrator.services import unmanaged_native_identity as seam

    native_id = str(uuid.uuid4())
    events: list[str] = []
    profile = AgentProfile(
        name="developer",
        description="Developer",
        model="gpt-5.6-sol" if provider == "codex" else None,
        codexConfig={"model_reasoning_effort": "xhigh"} if provider == "codex" else None,
    )
    monkeypatch.setattr(ts, "load_agent_profile", lambda _name: profile)
    if provider == "codex":
        _codex_seam_harness(monkeypatch, native_id=native_id, profile=profile)
        minted = codex_native_bootstrap.mint_session

        def _record_mint(**kwargs):
            result = minted(**kwargs)
            events.append(f"bootstrap:{result['native_session_id']}")
            return result

        monkeypatch.setattr(codex_native_bootstrap, "mint_session", _record_mint)
    else:
        monkeypatch.setattr(
            seam.claude_native_launch,
            "mint_session_id",
            lambda: (events.append(f"bootstrap:{native_id}") or native_id),
        )

    original_bind = ts._pre_task_bind_and_resolve

    def _record_bind(**kwargs):
        result = original_bind(**kwargs)
        row_id = database.get_terminal_metadata(kwargs["terminal_id"])["native_session_id"]
        lineage_id = roster.get_agent(roster.derive_initial_agent_id(kwargs["terminal_id"]))[
            "current_lineage"
        ]["native_session_id"]
        assert row_id == lineage_id == native_id
        events.append(f"durable:{native_id}")
        return result

    monkeypatch.setattr(ts, "_pre_task_bind_and_resolve", _record_bind)
    captured_provider: dict = {}

    def _provider(*args, **kwargs):
        captured_provider.update(kwargs)
        events.append(f"provider-constructed:{kwargs['native_session_id']}")
        instance = MagicMock()
        instance.shell_baseline = None

        async def _initialize():
            events.append("provider-initialize")
            return True

        instance.initialize = _initialize
        return instance

    launch_mocks["provider_manager"].create_provider.side_effect = _provider

    def _send(_terminal_id, message, **_kwargs):
        events.append(f"task:{message}")
        return True

    monkeypatch.setattr(ts, "send_input", _send)
    monkeypatch.setattr(
        ts,
        "_confirm_worker_started_or_resubmit",
        AsyncMock(return_value=True),
    )

    result = await create_terminal(
        provider,
        "developer",
        new_session=True,
        working_directory=str(tmp_path),
        defer_init=True,
        initial_message="do-the-task",
    )
    assert result.id == "test1234"
    for _ in range(400):
        if "task:do-the-task" in events:
            break
        await asyncio.sleep(0.01)
    assert "task:do-the-task" in events
    assert captured_provider["native_session_id"] == native_id
    assert events.index(f"bootstrap:{native_id}") < events.index(f"durable:{native_id}")
    assert events.index(f"durable:{native_id}") < events.index(f"provider-constructed:{native_id}")
    assert events.index(f"provider-constructed:{native_id}") < events.index("provider-initialize")
    assert events.index("provider-initialize") < events.index("task:do-the-task")


@pytest.mark.asyncio
async def test_supervisor_and_worker_share_the_gate(
    isolated_memory_db, launch_mocks, monkeypatch, tmp_path
):
    """Supervisor and worker both bind through the same pre-task contract;
    role comes from the launch owner, never profile-name inference."""
    from cli_agent_orchestrator.services import terminal_service as ts
    from cli_agent_orchestrator.services.terminal_service import _roster_bind_unmanaged

    native_id = str(uuid.uuid4())
    _codex_seam_harness(monkeypatch, native_id=native_id)
    await create_terminal("codex", "developer", new_session=True, working_directory=str(tmp_path))
    worker = roster.get_agent(roster.derive_initial_agent_id("test1234"))
    assert worker["role"] == roster.ROLE_WORKER

    monkeypatch.setattr(
        ts, "get_terminal_metadata", lambda terminal_id: {"provider": "claude_code"}
    )
    _roster_bind_unmanaged(
        terminal_id="b2c3d4e5",
        session_name=worker["session_name"],
        stable_agent_role=roster.ROLE_SUPERVISOR,
        agent_profile="code_supervisor",
        provider="claude_code",
        terminal_generation=None,
        pane_id="%77",
        pane_pid=1234,
        native_status_source=True,
        native_session_id=str(uuid.uuid4()),
        acquisition_method=roster.ACQUISITION_CHOSEN_SESSION_ID,
    )
    supervisor = roster.get_agent(roster.derive_initial_agent_id("b2c3d4e5"))
    assert supervisor["role"] == roster.ROLE_SUPERVISOR
    assert supervisor["current_lineage"]["native_session_id"] is not None


# ---------------------------------------------------------------------------
# The launch argv consumes the exact pre-task id (provider-unit level)
# ---------------------------------------------------------------------------


def test_claude_launch_argv_consumes_minted_id():
    from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

    sid = str(uuid.uuid4())
    provider = ClaudeCodeProvider("t1", "cao-s", "w1", native_session_id=sid)
    command = provider._build_claude_command()
    assert f"--session-id {sid}" in command


def test_codex_launch_argv_resumes_minted_id():
    from cli_agent_orchestrator.providers.codex import CodexProvider

    sid = str(uuid.uuid4())
    provider = CodexProvider("t1", "cao-s", "w1", native_session_id=sid)
    command = provider._build_codex_command()
    assert command.strip().endswith(f"resume {sid}")


@pytest.mark.asyncio
async def test_unmanaged_antigravity_new_launch_binds_pre_task_identity(
    isolated_memory_db, launch_mocks, monkeypatch
):
    """Antigravity: pre-task 1-turn bootstrap mints a conversation id, durable in roster and metadata."""
    import json
    import subprocess

    native_id = str(uuid.uuid4())

    def _mock_subprocess_run(cmd, *args, **kwargs):
        if "--version" in cmd or "version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="Antigravity CLI 1.1.11\n", stderr="")
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"conversation_id": native_id}), stderr=""
        )

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.unmanaged_native_identity._resolve_executable",
        lambda provider: "/usr/local/bin/agy",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.unmanaged_native_identity._binary_sha256",
        lambda exe: "a" * 64,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.unmanaged_native_identity._version_output",
        lambda provider, exe, env: "Antigravity CLI 1.1.11",
    )
    monkeypatch.setattr(subprocess, "run", _mock_subprocess_run)

    result = await create_terminal("antigravity_cli", "implementer-gemini", new_session=True)
    assert result.id == "test1234"
    agent = roster.get_agent(roster.derive_initial_agent_id("test1234"))
    lineage = agent["current_lineage"]
    assert lineage["native_session_id"] == native_id
    assert lineage["harness"] == "antigravity_cli"
    assert lineage["acquisition_method"] == roster.ACQUISITION_CONTROLLED_BOOTSTRAP_TURN
    from cli_agent_orchestrator.clients import database

    assert database.get_terminal_metadata("test1234")["native_session_id"] == native_id


def test_antigravity_launch_argv_resumes_minted_id():
    from cli_agent_orchestrator.providers.antigravity_cli import AntigravityCliProvider

    sid = str(uuid.uuid4())
    provider = AntigravityCliProvider(
        "t1", "cao-s", "w1", native_session_id=sid, model="gemini-3.7-flash", effort="high"
    )
    command = provider._build_agy_command()
    assert f"--conversation {sid}" in command
    assert "--model gemini-3.7-flash" in command
    assert "--effort high" in command
    assert "-i" not in command


def test_provider_manager_creates_antigravity_provider_with_native_id_and_effort():
    from cli_agent_orchestrator.providers.manager import ProviderManager

    manager = ProviderManager()
    sid = str(uuid.uuid4())
    provider = manager.create_provider(
        "antigravity_cli",
        "t1",
        "cao-s",
        "w1",
        agent_profile="implementer-gemini",
        native_session_id=sid,
        expected_model="gemini-3.7-flash",
        expected_effort="high",
    )
    command = provider._build_agy_command()
    assert f"--conversation {sid}" in command
    assert "--model gemini-3.7-flash" in command
    assert "--effort high" in command
    assert "-i" not in command


def test_validate_resume_argv_accepts_antigravity_wire_names():
    from cli_agent_orchestrator.services import provider_contracts

    sid = str(uuid.uuid4())
    argv = ["/usr/local/bin/agy", "--dangerously-skip-permissions", "--conversation", sid]

    parsed = provider_contracts.validate_resume_argv("antigravity_cli", argv)
    assert parsed.native_id == sid

    parsed_alias = provider_contracts.validate_resume_argv("antigravity", argv)
    assert parsed_alias.native_id == sid


def test_provider_manager_get_provider_reconstructs_antigravity_metadata(monkeypatch):
    from cli_agent_orchestrator.providers.manager import ProviderManager

    manager = ProviderManager()
    sid = str(uuid.uuid4())

    fake_metadata = {
        "provider": "antigravity_cli",
        "tmux_session": "cao-s",
        "tmux_window": "w1",
        "agent_profile": "implementer-gemini",
        "native_session_id": sid,
        "model": "gemini-3.7-flash",
        "effort": "high",
    }

    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.manager.get_terminal_metadata",
        lambda tid: fake_metadata,
    )

    provider = manager.get_provider("t1")
    assert provider._native_session_id == sid
    assert provider._model == "gemini-3.7-flash"
    assert provider._effort == "high"


def test_resolve_executable_is_entered_unpatched_for_every_activated_provider():
    """``_resolve_executable`` must actually EXECUTE for every activated cell.

    Every other test in this file monkeypatches this seam, so the region was
    never entered and a stale symbol reference survived: the table was renamed
    while its only call site still named the old one, making every activated
    unmanaged launch raise ``NameError`` -- which is not
    ``UnmanagedIdentityUnavailable``, so the launch path's typed handler did not
    catch it either.  This test enters the region for real.

    It deliberately does NOT require the binaries to be installed: the contract
    is that the call either resolves a path or refuses with the module's own
    typed error.  ``NameError``/``KeyError`` are neither, and are the failure
    this pins.
    """
    from cli_agent_orchestrator.services import unmanaged_native_identity as u

    assert u.UNMANAGED_PRE_TASK_PROVIDERS, "no activated providers to check"
    for provider in sorted(u.UNMANAGED_PRE_TASK_PROVIDERS):
        # The table must cover every activated provider...
        assert (
            provider in u._PROVIDER_EXECUTABLE
        ), f"{provider!r} is activated but has no executable-name entry"
        # ...and calling through must not blow up on a symbol/lookup error.
        try:
            resolved = u._resolve_executable(provider)
        except u.UnmanagedIdentityUnavailable:
            continue  # binary absent on this host: the typed, legal refusal
        assert isinstance(resolved, str) and resolved


def test_antigravity_resolves_the_agy_binary_not_its_policy_key():
    """Antigravity is the one provider whose executable differs from its
    provider vocabulary: the policy keys are ``antigravity`` /
    ``antigravity_cli``, the binary on PATH is ``agy``.  Substituting a
    ``PROVIDER_*`` constant here would make every agy launch fail to resolve,
    so the mapping is pinned explicitly.
    """
    from cli_agent_orchestrator.services import unmanaged_native_identity as u

    assert u._PROVIDER_EXECUTABLE["antigravity_cli"] == "agy"
