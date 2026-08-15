"""The dark exact physical executor (cond-0378 B3).

B3 consumes the exact B1 restore contract and the B2 winning
operation/effect/barrier seam and performs the one physical
reincarnation: reserve exactly one successor terminal id/generation
before physical I/O, fence/reap only the exact prior generation,
release its native attachment only after authoritative no-survivor
proof, acquire the same harness-scoped native session id, create the
successor pane through ``native_tui_launch.start(...,
launch_kind="resume")`` under CAS-authorized effect intents, verify the
provider-reported identity, and append/bind the successor incarnation
``bound`` — never admitted, never sending a single original task byte.

These tests were written before the production changes that satisfy
them.  Every physical boundary is faked deterministically: the pane
transport is an in-memory ``NativePaneTransport``, the exact-generation
terminal teardown is either recorded or driven through the real
``delete_terminal`` with the same patched-internals pattern the teardown
suite uses, and the prior attachment is a real attachment-store row over
a genuinely dead pid.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.providers.codex import render_trusted_project_override
from cli_agent_orchestrator.services import exact_executor as xe
from cli_agent_orchestrator.services import (
    managed_launch_v2,
    muse_native_launch,
)
from cli_agent_orchestrator.services import native_attachment as na
from cli_agent_orchestrator.services import native_attachment_recovery as recovery
from cli_agent_orchestrator.services import (
    native_tui_launch,
)
from cli_agent_orchestrator.services import operation_journal as oj
from cli_agent_orchestrator.services import restore_contract as rc
from cli_agent_orchestrator.services import session_lifecycle as sl
from cli_agent_orchestrator.services import stable_agent_roster as roster
from cli_agent_orchestrator.services import (
    terminal_service,
)
from cli_agent_orchestrator.utils.terminal import managed_window_name

_NATIVE_ID = "11111111-2222-4333-8444-555555555555"
_OTHER_NATIVE_ID = "99999999-8888-4777-8666-555555555555"
_CELL_REF = "claude_code:anthropic:native_tui"
_CELL_DIGEST = "c" * 64


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _db(isolated_memory_db):
    return isolated_memory_db


@pytest.fixture(autouse=True)
def _no_teardown_grace(monkeypatch):
    """The release step's wait for a just-killed provider adds only latency
    against pids these tests already know the answer for."""
    monkeypatch.setattr(recovery, "TEARDOWN_GRACE_SECONDS", 0.0)


@pytest.fixture(scope="module")
def _launch_template(tmp_path_factory):
    """A real canonical working directory and a real digest-pinned binary.

    The executor requires the stored contract's executable and working
    directory to be present and digest/canonically matched at launch
    time, so the source facts have to be real filesystem facts.
    """
    root = tmp_path_factory.mktemp("b3-launch")
    workdir = os.path.realpath(str(root / "work"))
    os.makedirs(workdir, exist_ok=True)
    bindir = os.path.realpath(str(root / "bin"))
    os.makedirs(bindir, exist_ok=True)
    binary = os.path.join(bindir, "claude")
    with open(binary, "wb") as handle:
        handle.write(b"#!/bin/sh\nsleep 60\n")
    os.chmod(binary, os.stat(binary).st_mode | stat.S_IXUSR | stat.S_IXGRP)
    digest = hashlib.sha256(open(binary, "rb").read()).hexdigest()
    return {"workdir": workdir, "binary": binary, "binary_sha256": digest}


@pytest.fixture
def launch_root(tmp_path, _launch_template):
    """Per-test launch facts so parallel state never collides.

    Includes a REAL profile file with its REAL digest: the executor
    re-hashes the contract's paired ``_path``/``_sha256`` profile and
    provider-home references at launch time, so the stored facts must be
    genuine filesystem facts, exactly like the executable.
    """
    import shutil

    workdir = os.path.realpath(str(tmp_path / "work"))
    os.makedirs(workdir, exist_ok=True)
    binary = os.path.realpath(str(tmp_path / "bin" / "claude"))
    os.makedirs(os.path.dirname(binary), exist_ok=True)
    shutil.copyfile(_launch_template["binary"], binary)
    os.chmod(binary, os.stat(binary).st_mode | stat.S_IXUSR)
    digest = hashlib.sha256(open(binary, "rb").read()).hexdigest()
    profile = os.path.realpath(str(tmp_path / "profile" / "settings.json"))
    os.makedirs(os.path.dirname(profile), exist_ok=True)
    with open(profile, "wb") as handle:
        handle.write(b'{"permissions": {"allow": ["*"]}}\n')
    alt_profile = os.path.realpath(str(tmp_path / "profile" / "settings-alt.json"))
    with open(alt_profile, "wb") as handle:
        handle.write(b'{"permissions": {"allow": ["Read"]}}\n')
    home_file = os.path.realpath(str(tmp_path / "home" / "credentials.json"))
    os.makedirs(os.path.dirname(home_file), exist_ok=True)
    with open(home_file, "wb") as handle:
        handle.write(b'{"token_ref": "keychain"}\n')
    return {
        "workdir": workdir,
        "binary": binary,
        "binary_sha256": digest,
        "profile_path": profile,
        "profile_sha256": hashlib.sha256(open(profile, "rb").read()).hexdigest(),
        "alt_profile_path": alt_profile,
        "alt_profile_sha256": hashlib.sha256(open(alt_profile, "rb").read()).hexdigest(),
        "home_dir": os.path.dirname(home_file),
        "home_path": home_file,
        "home_sha256": hashlib.sha256(open(home_file, "rb").read()).hexdigest(),
    }


def _fact(value):
    return rc.ContractFact.present(value)


def _bind_worker(agent_id=None, terminal_id="a1b2c3d4", **bind_changes):
    payload = {
        "agent_id": agent_id or str(uuid.uuid4()),
        "session_name": "cao-campaign-a",
        "role": roster.ROLE_WORKER,
        "profile_family": "developer",
        "harness": "claude_code",
        "native_session_id": _NATIVE_ID,
        "acquisition_method": "chosen_session_id",
        "route_provenance": {"provider_route": "anthropic"},
        "terminal_id": terminal_id,
        "generation": "00000000-0000-4000-8000-000000000001",
        "pane_id": "%101",
        "pane_pid": 4242,
        "process_identity": {"pid": 4242, "start_marker": "2026-08-09T00:00:00Z"},
        "execution_mode": "native_tui",
    }
    payload.update(bind_changes)
    return roster.bind_generation(roster.BindingContract(**payload))


def _contract_for(bind, launch_root, **changes):
    payload = {
        "agent_id": bind["agent"]["agent_id"],
        "lineage_id": bind["lineage"]["lineage_id"],
        "terminal_id": bind["incarnation"]["terminal_id"],
        "generation": bind["incarnation"]["generation"],
        "native_session_id": bind["lineage"]["native_session_id"],
        "harness": bind["lineage"]["harness"],
        "provider": "claude_code",
        "route_provenance": bind["lineage"]["route_provenance"],
        "execution_mode": bind["incarnation"]["execution_mode"],
        "model": _fact("claude-sonnet-4-5"),
        "effort": _fact("high"),
        "working_directory": launch_root["workdir"],
        "trusted_project_root": None,
        "executable": _fact(
            {"path": launch_root["binary"], "sha256": launch_root["binary_sha256"]}
        ),
        "profile_material": _fact(
            {
                "profile_config_path": launch_root["profile_path"],
                "profile_config_sha256": launch_root["profile_sha256"],
            }
        ),
        "provider_home_facts": rc.ContractFact.unavailable(
            "no provider-home carrier facts at this source seam"
        ),
    }
    payload.update(changes)
    return rc.RestoreContract(**payload)


def _dormant_worker(
    launch_root, agent_id=None, terminal_id="a1b2c3d4", _contract_changes=None, **bind_changes
):
    """A dormant source: bound, contracted, and atomically retired."""
    bind = _bind_worker(agent_id=agent_id, terminal_id=terminal_id, **bind_changes)
    contract = _contract_for(bind, launch_root, **(_contract_changes or {}))
    rc.publish_contract(contract)
    roster.transition_dormant(
        terminal_id=contract.terminal_id,
        generation=contract.generation,
        agent_id=contract.agent_id,
        lineage_id=contract.lineage_id,
        contract_digest=contract.digest(),
        reason="pane lost",
    )
    return bind, contract


def _operation_request(bind, contract, operation_id=None, **changes):
    agent = roster.get_agent(bind["agent"]["agent_id"])
    payload = {
        "operation_id": operation_id or str(uuid.uuid4()),
        "session_name": bind["agent"]["session_name"],
        "agent_id": bind["agent"]["agent_id"],
        "roster_revision": agent["revision"],
        "role": bind["agent"]["role"],
        "profile_family": bind["agent"]["profile_family"],
        "lineage_id": bind["lineage"]["lineage_id"],
        "harness": bind["lineage"]["harness"],
        "native_session_id": bind["lineage"]["native_session_id"],
        "prior_terminal_id": bind["incarnation"]["terminal_id"],
        "prior_generation": bind["incarnation"]["generation"],
        "prior_incarnation_id": bind["incarnation"]["incarnation_id"],
        "lifecycle_epoch": 0,
        "lifecycle_observation": sl.WORKING,
        "restore_contract_digest": contract.digest(),
        "restore_contract_schema": rc.SCHEMA_VERSION,
        "route_provider": "claude_code",
        "model_requested": "claude-sonnet-4-5",
        "effort_requested": "high",
        "execution_mode_requested": "native_tui",
        "compatibility_cell_ref": _CELL_REF,
        "compatibility_cell_digest": _CELL_DIGEST,
    }
    if "restore_contract_id" not in changes:
        payload["restore_contract_id"] = rc.get_contract_by_incarnation(
            terminal_id=bind["incarnation"]["terminal_id"],
            generation=bind["incarnation"]["generation"],
        )["contract_id"]
    payload.update(changes)
    return oj.OperationRequest(**payload)


def _codex_dormant_worker(launch_root, trusted_root, _contract_changes=None):
    """A dormant Codex source whose contract records a trusted project root.

    Codex has no generic settings-file argv lane, so its contract records
    profile material as truthfully unavailable and the provider home as a
    reference (applied through the ``CODEX_HOME`` carrier lane)."""
    bind = _bind_worker(harness="codex", route_provenance={"provider_route": "openai"})
    changes = {
        "harness": "codex",
        "provider": "codex",
        "model": _fact("gpt-5.3-codex"),
        "trusted_project_root": trusted_root,
        "profile_material": rc.ContractFact.unavailable(
            "codex profile material is composed from structured profile facts, "
            "not a single settings file"
        ),
        "provider_home_facts": _fact({"provider_home_path": launch_root["home_dir"]}),
    }
    changes.update(_contract_changes or {})
    contract = _contract_for(bind, launch_root, **changes)
    rc.publish_contract(contract)
    roster.transition_dormant(
        terminal_id=contract.terminal_id,
        generation=contract.generation,
        agent_id=contract.agent_id,
        lineage_id=contract.lineage_id,
        contract_digest=contract.digest(),
        reason="pane lost",
    )
    return bind, contract


def _codex_request(bind, contract, **changes):
    payload = {
        "harness": "codex",
        "route_provider": "codex",
        "model_requested": "gpt-5.3-codex",
        "compatibility_cell_ref": "codex:openai:native_tui",
    }
    payload.update(changes)
    return _operation_request(bind, contract, **payload)


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


def _prior_attachment(
    *,
    pid: Optional[int] = None,
    terminal_id: str = "a1b2c3d4",
    generation: str = "00000000-0000-4000-8000-000000000001",
    native_session_id: str = _NATIVE_ID,
    provider: str = "claude_code",
) -> dict:
    """A real attachment row owned by the exact prior incarnation."""
    owner = {
        "terminal_id": terminal_id,
        "generation": generation,
        "execution_mode": "native_tui",
    }
    na.declare(
        provider=provider,
        native_session_id=native_session_id,
        intent=na.acquire_intent(
            acquisition_method=na.ACQUISITION_CHOSEN_SESSION_ID,
            acquisition_receipt={"kind": "chosen", "session_id": native_session_id},
            admits_only_new_instructions=True,
            replays_task_bytes=False,
        ),
        **owner,
    )
    na.mark_starting(provider=provider, native_session_id=native_session_id, **owner)
    return na.mark_attached(
        provider=provider,
        native_session_id=native_session_id,
        process_identity=na.process_identity(
            pid=pid if pid is not None else _dead_pid(),
            start_marker="2026-08-09T00:00:00Z",
        ),
        **owner,
    )


@dataclass
class FakeTransport:
    """A deterministic ``NativePaneTransport`` over the real launch path.

    ``create_pane`` records the exact argv it was asked to start and
    returns a fresh pane handle; ``observe`` reports that pane running
    whichever session the test wants it to run (default: the exact bound
    one) in the canonical working directory.
    """

    workdir: str
    native_session_id: str = _NATIVE_ID
    argv: Optional[list] = None
    created: int = 0
    observed_argv: Optional[list] = None
    pane_pid: int = field(default_factory=os.getpid)
    create_error: Optional[Exception] = None
    observe_error: Optional[Exception] = None

    def create_pane(self, *, argv) -> str:
        if self.create_error is not None:
            raise self.create_error
        self.created += 1
        self.argv = list(argv)
        return f"%{self.created + 41}"

    def observe(self):
        if self.observe_error is not None:
            raise self.observe_error
        if self.created == 0:
            return None
        argv = self.observed_argv if self.observed_argv is not None else list(self.argv or [])
        return {
            "pane_id": "%42",
            "pid": self.pane_pid,
            "start_marker": "2026-08-14T00:00:00Z",
            "argv": argv,
            "cwd": self.workdir,
        }

    def capture_render(self, pane_id: str) -> list[str]:
        return []


@dataclass
class ReapRecorder:
    """Records the exact-generation teardown the executor performs."""

    calls: list = field(default_factory=list)
    result: bool = True
    error: Optional[Exception] = None

    def __call__(self, terminal_id, registry=None, **kwargs):
        self.calls.append(
            {"terminal_id": terminal_id, "thread_id": threading.get_ident(), **kwargs}
        )
        if self.error is not None:
            raise self.error
        return self.result


def _reap_noop(monkeypatch) -> ReapRecorder:
    recorder = ReapRecorder()
    monkeypatch.setattr(terminal_service, "delete_terminal", recorder)
    return recorder


class _InputSpy:
    """Records (and forbids) any task/input/conductor surface touch."""

    def __init__(self) -> None:
        self.touched: list = []

    def __call__(self, *args, **kwargs):
        self.touched.append((args, kwargs))
        raise AssertionError("B3 must not send any task/input bytes")


@pytest.fixture
def input_spies(monkeypatch):
    """Every lane that could carry task bytes or supervisor effects is
    booby-trapped; the executor must complete without touching one."""
    spy = _InputSpy()
    from cli_agent_orchestrator.services import (
        agent_step,
        control_input_service,
        flow_service,
        inbox_service,
    )

    monkeypatch.setattr(terminal_service, "send_message", spy, raising=False)
    monkeypatch.setattr(terminal_service, "send_special_key", spy, raising=False)
    monkeypatch.setattr(terminal_service, "send_keys", spy, raising=False)
    monkeypatch.setattr(control_input_service, "submit_control_input", spy, raising=False)
    monkeypatch.setattr(flow_service, "FlowService", spy, raising=False)
    monkeypatch.setattr(inbox_service, "InboxService", spy, raising=False)
    monkeypatch.setattr(agent_step, "run_agent_step", spy, raising=False)
    monkeypatch.setattr(managed_launch_v2, "attempt_resume", spy)
    monkeypatch.setattr(native_tui_launch, "start_discovered", spy)
    monkeypatch.setattr(roster, "mark_admitted", spy)
    return spy


def _execute(request, transport, material=None):
    return xe.execute(
        request, material=material or xe.LaunchMaterial(), transport_factory=lambda: transport
    )


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def file_db(tmp_path, monkeypatch):
    """A real SQLite-file store for concurrency tests (two sessions, one file)."""
    from cli_agent_orchestrator.clients import database

    engine = create_engine(f"sqlite:///{tmp_path / 'conc.db'}")
    database.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(
        database,
        "SessionLocal",
        sessionmaker(bind=engine),
    )
    yield engine
    engine.dispose()


def _retamper_contract(bind, mutate) -> None:
    """Rewrite the stored contract payload through ``mutate`` canonically."""
    from cli_agent_orchestrator.clients import database

    stored = rc.get_contract_by_incarnation(
        terminal_id=bind["incarnation"]["terminal_id"],
        generation=bind["incarnation"]["generation"],
    )
    payload = stored["contract"]
    mutate(payload)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    with database.SessionLocal() as session:
        session.execute(
            text(
                "UPDATE restore_contracts SET contract_json = :json, "
                "contract_digest = :digest WHERE terminal_id = :tid AND generation = :gen"
            ),
            {
                "json": canonical,
                "digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                "tid": bind["incarnation"]["terminal_id"],
                "gen": bind["incarnation"]["generation"],
            },
        )
        session.commit()


# ---------------------------------------------------------------------------
# 1. one fresh successor generation on the same stable agent/native lineage
# ---------------------------------------------------------------------------


def test_lost_pane_source_converges_to_one_fresh_successor(launch_root, monkeypatch):
    """A source whose pane/row are already gone still restores exactly: one
    fresh successor generation on the same stable agent and native lineage."""
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    result = _run(_execute(request, transport))

    assert result["outcome"] == xe.OUTCOME_ACCEPTED
    successor_generation = result["successor_generation"]
    assert successor_generation != contract.generation
    assert result["successor_terminal_id"] != contract.terminal_id
    agent = roster.get_agent(bind["agent"]["agent_id"])
    incarnation = agent["current_incarnation"]
    assert incarnation["generation"] == successor_generation
    assert incarnation["terminal_id"] == result["successor_terminal_id"]
    assert incarnation["disposition"] == roster.INCARNATION_BOUND
    assert incarnation["lineage_id"] == bind["lineage"]["lineage_id"]
    assert agent["current_lineage"]["native_session_id"] == _NATIVE_ID
    # Exactly one successor incarnation was appended, ever.
    assert len(roster.list_incarnations(agent_id=agent["agent_id"])) == 2
    assert transport.created == 1


def test_deliberately_retired_worker_restores_through_the_real_teardown(launch_root, monkeypatch):
    """A deliberately retired worker (its terminal still present) is reaped
    through the real exact-generation teardown seam — with the attachment
    release and roster retirement held for B3's own authorized steps."""
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)

    from unittest.mock import patch

    ts = "cli_agent_orchestrator.services.terminal_service"
    window = managed_window_name(contract.terminal_id, contract.generation)
    release_calls: list = []
    roster_retire_calls: list = []
    _original_release = recovery.release_owned_by_terminal

    def _recording_release(terminal_id, *, generation=None, grace_seconds=None):
        release_calls.append(terminal_id)
        return _original_release(terminal_id, generation=generation, grace_seconds=grace_seconds)

    def _recording_roster_retire(terminal_id, generation):
        roster_retire_calls.append(terminal_id)

    with (
        patch(f"{ts}.status_monitor"),
        patch(f"{ts}.fifo_manager"),
        patch(f"{ts}.provider_manager"),
        patch("cli_agent_orchestrator.backends.registry._backend") as mock_backend,
        patch(
            f"{ts}.get_terminal_metadata",
            return_value={
                "tmux_session": "cao-campaign-a",
                "tmux_window": window,
                "generation": contract.generation,
                "terminal_id": contract.terminal_id,
            },
        ),
        patch(f"{ts}.get_terminal_metadata_v2", return_value=None),
        patch(f"{ts}.db_delete_terminal_if_generation", return_value=True),
        patch.object(recovery, "release_owned_by_terminal", _recording_release),
        patch(f"{ts}._roster_retire_incarnation_best_effort", _recording_roster_retire),
    ):
        mock_backend.window_exists.return_value = False
        transport = FakeTransport(workdir=launch_root["workdir"])
        result = _run(_execute(request, transport))

    assert result["outcome"] == xe.OUTCOME_ACCEPTED
    # The teardown itself must NOT have released the attachment or bumped the
    # roster: the only release call is B3's own authorized step (after the
    # teardown returned), and the roster retirement is held entirely.
    assert release_calls.count("a1b2c3d4") == 1
    assert roster_retire_calls == []
    # The attachment was released by the executor's own authorized step and
    # re-acquired by the successor.
    attachment = na.get("claude_code", _NATIVE_ID)
    assert attachment["state"] == na.ATTACHED
    assert attachment["owner"]["terminal_id"] == result["successor_terminal_id"]
    assert attachment["owner"]["generation"] == result["successor_generation"]
    assert attachment["release_proof"] is not None  # prior release preserved


# ---------------------------------------------------------------------------
# 2. exact retry / response loss / restart adopt — never a second effect
# ---------------------------------------------------------------------------


def test_exact_retry_never_recreates_or_rebinds(launch_root, monkeypatch):
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract, operation_id=str(uuid.uuid4()))
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    first = _run(_execute(request, transport))
    revision_after_first = roster.get_agent(bind["agent"]["agent_id"])["revision"]

    second = _run(_execute(request, transport))
    assert second["outcome"] == xe.OUTCOME_ACCEPTED
    assert second["successor_terminal_id"] == first["successor_terminal_id"]
    assert second["successor_generation"] == first["successor_generation"]
    assert second["successor_incarnation_id"] == first["successor_incarnation_id"]
    # No second pane, no second bind, no roster churn.
    assert transport.created == 1
    assert roster.get_agent(bind["agent"]["agent_id"])["revision"] == revision_after_first
    assert len(roster.list_incarnations(agent_id=bind["agent"]["agent_id"])) == 2


def test_response_loss_mid_launch_adopts_the_inflight_pane(launch_root, monkeypatch):
    """A run that response-lost after the pane existed re-enters through the
    launch seam's own re-entry contract: observe-and-publish, never a second
    pane."""
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])

    # Crash the first run exactly after the pane was published but before the
    # final roster bind.
    original_bind = roster.bind_generation
    state = {"crashed": False}

    def _crash_bind(*args, **kwargs):
        if not state["crashed"]:
            state["crashed"] = True
            raise RuntimeError("response lost before the final bind")
        return original_bind(*args, **kwargs)

    monkeypatch.setattr(roster, "bind_generation", _crash_bind)
    with pytest.raises(Exception):
        _run(_execute(request, transport))
    monkeypatch.setattr(roster, "bind_generation", original_bind)

    result = _run(_execute(request, transport))
    assert result["outcome"] == xe.OUTCOME_ACCEPTED
    assert transport.created == 1
    assert len(roster.list_incarnations(agent_id=bind["agent"]["agent_id"])) == 2


def test_crash_between_launch_intent_and_pane_creation_adopts_on_retry(launch_root, monkeypatch):
    """The launch_resume intent committed, then the process died before the
    atomic transport call: the retry adopts the durable intents, creates
    exactly one pane, and binds — never a second pane or effect."""
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    original = oj.authorize_effect_intent
    state = {"crashed": False}

    def _crash_after_launch_intent(operation_id, **kwargs):
        result = original(operation_id, **kwargs)
        if not state["crashed"] and kwargs.get("effect_step") == oj.EFFECT_STEP_LAUNCH_RESUME:
            state["crashed"] = True
            raise RuntimeError("process died after the launch intent committed")
        return result

    monkeypatch.setattr(oj, "authorize_effect_intent", _crash_after_launch_intent)
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(RuntimeError):
        _run(_execute(request, transport))
    # The intent is durable; the pane never existed.
    assert transport.created == 0
    assert oj.get_operation(request.operation_id)["phase"] == oj.EFFECT_STEP_LAUNCH_RESUME
    monkeypatch.setattr(oj, "authorize_effect_intent", original)

    result = _run(_execute(request, transport))
    assert result["outcome"] == xe.OUTCOME_ACCEPTED
    assert transport.created == 1
    assert len(roster.list_incarnations(agent_id=bind["agent"]["agent_id"])) == 2


def test_restart_adopts_the_durable_successor(launch_root, monkeypatch, tmp_path):
    """A process restart (fresh engine over the same file) adopts the same
    operation, reservation, intents, attachment, and roster incarnation."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from cli_agent_orchestrator.clients import database

    db_path = tmp_path / "restart.db"
    engine = create_engine(f"sqlite:///{db_path}")
    database.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))

    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    first = _run(_execute(request, transport))
    engine.dispose()

    engine2 = create_engine(f"sqlite:///{db_path}")
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine2))
    try:
        transport2 = FakeTransport(workdir=launch_root["workdir"])
        second = _run(_execute(request, transport2))
    finally:
        engine2.dispose()
    assert second["successor_terminal_id"] == first["successor_terminal_id"]
    assert second["successor_generation"] == first["successor_generation"]
    assert transport2.created == 0
    stored = oj.get_operation(request.operation_id)
    assert stored["result_state"] == xe.OUTCOME_ACCEPTED
    assert stored["successor_terminal_id"] == first["successor_terminal_id"]


def test_local_refusal_surfaces_concurrent_durable_reconciliation(launch_root, monkeypatch):
    """A cross-process final result that wins after the executor's initial
    read is the response, even when this process subsequently reaches a local
    pre-effect refusal.  The retryable local observation must not contradict
    the journal's write-once reconciliation outcome."""
    bind, contract = _dormant_worker(launch_root)
    request = _operation_request(bind, contract)
    durable_detail = "another executor recorded an ambiguous physical result"
    real_record = oj.record_result
    injected = {"done": False}

    def _race_final_result(operation_id, state, **kwargs):
        if state == oj.RESULT_REFUSED and not injected["done"]:
            injected["done"] = True
            real_record(
                operation_id,
                oj.RESULT_RECONCILIATION_REQUIRED,
                detail=durable_detail,
            )
        return real_record(operation_id, state, **kwargs)

    monkeypatch.setattr(oj, "record_result", _race_final_result)
    monkeypatch.setattr(xe, "_fact_refusal", lambda _contract: "local retryable refusal")

    with pytest.raises(xe.ExactExecutorReconciliation, match=durable_detail):
        _run(_execute(request, FakeTransport(workdir=launch_root["workdir"])))
    assert oj.get_result(request.operation_id)["result_state"] == (
        xe.OUTCOME_RECONCILIATION_REQUIRED
    )


def test_local_reconciliation_returns_concurrent_durable_acceptance(launch_root, monkeypatch):
    """If another executor has already finalized acceptance, a later local
    ambiguity adopts that accepted result instead of reporting a contradictory
    reconciliation-required response."""
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    successor_incarnation_id = str(uuid.uuid4())
    real_record = oj.record_result
    injected = {"done": False}

    def _race_final_result(operation_id, state, **kwargs):
        if state == oj.RESULT_RECONCILIATION_REQUIRED and not injected["done"]:
            injected["done"] = True
            real_record(
                operation_id,
                oj.RESULT_ACCEPTED,
                detail="another executor completed the exact bind",
                successor_incarnation_id=successor_incarnation_id,
            )
        return real_record(operation_id, state, **kwargs)

    def _local_ambiguity(**_kwargs):
        raise native_tui_launch.NativeLaunchAmbiguous(
            native_tui_launch.AMBIGUOUS_PANE_CREATE,
            "this process lost the pane-create response",
        )

    monkeypatch.setattr(oj, "record_result", _race_final_result)
    monkeypatch.setattr(native_tui_launch, "start", _local_ambiguity)

    result = _run(_execute(request, FakeTransport(workdir=launch_root["workdir"])))
    assert result["outcome"] == xe.OUTCOME_ACCEPTED
    assert result["successor_incarnation_id"] == successor_incarnation_id
    assert oj.get_result(request.operation_id)["result_state"] == xe.OUTCOME_ACCEPTED


def test_local_launch_unavailable_returns_concurrent_durable_acceptance(launch_root, monkeypatch):
    """A transient launch dependency error must not hide another process's
    write-once acceptance that committed while this launch was in flight."""
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    successor_incarnation_id = str(uuid.uuid4())

    def _accepted_then_unavailable(**_kwargs):
        oj.record_result(
            request.operation_id,
            oj.RESULT_ACCEPTED,
            detail="another executor completed the exact bind",
            successor_incarnation_id=successor_incarnation_id,
        )
        raise native_tui_launch.NativeLaunchUnavailable("attachment store read timed out")

    monkeypatch.setattr(native_tui_launch, "start", _accepted_then_unavailable)

    result = _run(_execute(request, FakeTransport(workdir=launch_root["workdir"])))
    assert result["outcome"] == xe.OUTCOME_ACCEPTED
    assert result["successor_incarnation_id"] == successor_incarnation_id
    assert oj.get_result(request.operation_id)["result_state"] == xe.OUTCOME_ACCEPTED


# ---------------------------------------------------------------------------
# 3. wrong material refuses before physical effects
# ---------------------------------------------------------------------------


def test_unavailable_executable_fact_refuses_before_effects(launch_root, monkeypatch):
    """A stored contract whose executable fact is not present is a typed
    disabled outcome: the claim adopts, but no successor is reserved and no
    effect intent is recorded."""
    bind, contract = _dormant_worker(launch_root)
    request = _operation_request(bind, contract)
    assert oj.claim_operation(request)["adopted"] is False

    def _mutate(payload):
        payload["executable"] = {
            "state": "unavailable",
            "value": None,
            "reason": "the launch path could not resolve the binary",
        }

    _retamper_contract(bind, _mutate)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(xe.ExactExecutorRefused) as excinfo:
        _run(_execute(request, transport))
    assert "executable" in str(excinfo.value).lower()
    # Zero physical effects and zero successor reservation.
    assert transport.created == 0
    stored_op = oj.get_operation(request.operation_id)
    assert stored_op["successor_terminal_id"] is None
    assert oj.list_effect_intents(request.operation_id) == []


def test_route_variation_without_a_compatibility_cell_is_typed_disabled(launch_root, monkeypatch):
    bind, contract = _dormant_worker(launch_root)
    request = _operation_request(
        bind,
        contract,
        model_requested="claude-opus-5",
        compatibility_cell_ref=None,
        compatibility_cell_digest=None,
    )
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(xe.ExactExecutorRefused) as excinfo:
        _run(_execute(request, transport))
    assert "compatibility" in str(excinfo.value).lower()
    assert transport.created == 0
    assert oj.list_effect_intents(request.operation_id) == []


def test_cell_not_naming_the_harness_refuses(launch_root, monkeypatch):
    bind, contract = _dormant_worker(launch_root)
    request = _operation_request(
        bind,
        contract,
        model_requested="claude-opus-5",
        compatibility_cell_ref="muse_cli:contributor:native_tui",
        compatibility_cell_digest="d" * 64,
    )
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(xe.ExactExecutorRefused):
        _run(_execute(request, transport))
    assert transport.created == 0
    assert oj.list_effect_intents(request.operation_id) == []


def test_wrong_restore_contract_digest_conflicts_at_claim(launch_root, monkeypatch):
    bind, contract = _dormant_worker(launch_root)
    request = _operation_request(bind, contract, restore_contract_digest="e" * 64)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(oj.OperationJournalError):
        _run(_execute(request, transport))
    assert oj.list_operations() == []
    assert transport.created == 0


def test_executable_digest_drift_refuses_before_effects(launch_root, monkeypatch):
    """The digest-pinned executable is re-hashed at launch time: bytes that
    changed after the contract pinned the digest refuse before any physical
    effect, rather than launching an image that was never admitted."""
    bind, contract = _dormant_worker(launch_root)
    request = _operation_request(bind, contract)
    assert oj.claim_operation(request)["adopted"] is False
    with open(launch_root["binary"], "wb") as handle:
        handle.write(b"#!/bin/sh\nsleep 61\n")
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(xe.ExactExecutorRefused) as excinfo:
        _run(_execute(request, transport))
    assert "digest" in str(excinfo.value).lower()
    assert transport.created == 0
    assert oj.list_effect_intents(request.operation_id) == []
    assert oj.get_operation(request.operation_id)["successor_terminal_id"] is None


def test_profile_digest_drift_refuses_before_effects(launch_root, monkeypatch):
    """A paired profile ``_path``/``_sha256`` fact is re-hashed at launch
    time: edited profile bytes are changed launch material, and B3 exact
    restore refuses before any physical effect instead of launching them."""
    bind, contract = _dormant_worker(launch_root)
    request = _operation_request(bind, contract)
    assert oj.claim_operation(request)["adopted"] is False
    with open(launch_root["profile_path"], "wb") as handle:
        handle.write(b'{"permissions": {"allow": []}}\n')
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(xe.ExactExecutorRefused) as excinfo:
        _run(_execute(request, transport))
    assert "profile" in str(excinfo.value).lower()
    assert transport.created == 0
    assert oj.list_effect_intents(request.operation_id) == []
    assert oj.get_operation(request.operation_id)["successor_terminal_id"] is None


def test_provider_home_digest_drift_refuses_before_effects(launch_root, monkeypatch):
    """The same live digest check binds present provider-home facts: a
    changed home file the harness will read is a pre-effect refusal."""
    bind, contract = _dormant_worker(
        launch_root,
        _contract_changes={
            "provider_home_facts": _fact(
                {
                    "credentials_path": launch_root["home_path"],
                    "credentials_sha256": launch_root["home_sha256"],
                }
            )
        },
    )
    request = _operation_request(bind, contract)
    assert oj.claim_operation(request)["adopted"] is False
    with open(launch_root["home_path"], "wb") as handle:
        handle.write(b'{"token_ref": "rotated"}\n')
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(xe.ExactExecutorRefused) as excinfo:
        _run(_execute(request, transport))
    assert "provider_home_facts" in str(excinfo.value)
    assert transport.created == 0
    assert oj.list_effect_intents(request.operation_id) == []


def _alt_profile_material(launch_root) -> dict:
    """A supplied profile mapping that differs from the contract's."""
    return {
        "profile_config_path": launch_root["alt_profile_path"],
        "profile_config_sha256": launch_root["alt_profile_sha256"],
    }


def test_declared_profile_variation_requires_the_exact_cell(launch_root, monkeypatch):
    """Launch material supplying profile material other than the contract's
    is a variation: without the operation naming the exact compatibility
    cell it is typed-disabled before any physical effect."""
    bind, contract = _dormant_worker(launch_root)
    request = _operation_request(
        bind, contract, compatibility_cell_ref=None, compatibility_cell_digest=None
    )
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    material = xe.LaunchMaterial(profile_material=_alt_profile_material(launch_root))
    with pytest.raises(xe.ExactExecutorRefused) as excinfo:
        _run(_execute(request, transport, material=material))
    assert "compatibility" in str(excinfo.value).lower()
    assert "profile" in str(excinfo.value).lower()
    assert transport.created == 0
    assert oj.list_effect_intents(request.operation_id) == []


def test_declared_profile_variation_with_a_wrong_harness_cell_refuses(launch_root, monkeypatch):
    """A variation covered by a cell that does not name the launch harness
    is not covered at all: the launch material does not agree with the
    recorded cell."""
    bind, contract = _dormant_worker(launch_root)
    request = _operation_request(
        bind,
        contract,
        compatibility_cell_ref="muse_cli:contributor:native_tui",
        compatibility_cell_digest="d" * 64,
    )
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    material = xe.LaunchMaterial(profile_material=_alt_profile_material(launch_root))
    with pytest.raises(xe.ExactExecutorRefused) as excinfo:
        _run(_execute(request, transport, material=material))
    assert "does not name" in str(excinfo.value)
    assert transport.created == 0
    assert oj.list_effect_intents(request.operation_id) == []


def test_declared_profile_variation_covered_by_the_exact_cell_restores(launch_root, monkeypatch):
    """A cell-covered profile variation supplies the new reference mapping,
    its paired file is verified live, and the new path is what actually
    reaches the resume argv through the explicit lane."""
    bind, contract = _dormant_worker(launch_root)
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    material = xe.LaunchMaterial(profile_material=_alt_profile_material(launch_root))
    result = _run(_execute(request, transport, material=material))
    assert result["outcome"] == xe.OUTCOME_ACCEPTED
    # The ALTERNATE profile config is on the resume argv, not the contract's.
    settings_at = transport.argv.index("--settings")
    assert transport.argv[settings_at + 1] == launch_root["alt_profile_path"]
    evidence = oj.get_operation(request.operation_id)["result_evidence"]
    assert evidence["compatibility_cell_ref"] == _CELL_REF
    assert evidence["declared_profile_material_digest"] == xe._reference_dict_digest(
        _alt_profile_material(launch_root)
    )


def test_variation_mapping_with_unverifiable_files_refuses_before_reservation(
    launch_root, monkeypatch
):
    """A supplied mapping whose paired file no longer exists cannot be
    verified; even with the exact cell the variation refuses before the
    successor is reserved or any effect is authorized."""
    bind, contract = _dormant_worker(launch_root)
    request = _operation_request(bind, contract)
    gone = os.path.realpath(os.path.join(launch_root["workdir"], "gone-settings.json"))
    material = xe.LaunchMaterial(
        profile_material={"profile_config_path": gone, "profile_config_sha256": "a" * 64}
    )
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(xe.ExactExecutorRefused) as excinfo:
        _run(_execute(request, transport, material=material))
    assert "profile_material" in str(excinfo.value)
    assert transport.created == 0
    assert oj.list_effect_intents(request.operation_id) == []
    assert oj.get_operation(request.operation_id)["successor_terminal_id"] is None


def test_unpinnable_effective_model_refuses_before_effects(launch_root, monkeypatch):
    """Claude's route must be pinnable: an effective model the pin contract
    cannot attest refuses before any physical effect rather than launching
    whatever the provider would default to."""
    bind, contract = _dormant_worker(launch_root, _contract_changes={"model": _fact("llama-9")})
    request = _operation_request(bind, contract, model_requested="llama-9")
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(xe.ExactExecutorRefused) as excinfo:
        _run(_execute(request, transport))
    assert "model" in str(excinfo.value).lower()
    assert transport.created == 0
    assert oj.list_effect_intents(request.operation_id) == []


def test_missing_effective_model_refuses_for_a_pin_requiring_harness(launch_root, monkeypatch):
    """No stored model fact and no requested model: a harness whose route
    must be argv-pinned disables exact restore rather than falling back to
    the provider's ambient default."""
    bind, contract = _dormant_worker(
        launch_root,
        _contract_changes={"model": rc.ContractFact.unavailable("no model at the source seam")},
    )
    request = _operation_request(bind, contract, model_requested=None)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(xe.ExactExecutorRefused) as excinfo:
        _run(_execute(request, transport))
    assert "model" in str(excinfo.value).lower()
    assert transport.created == 0
    assert oj.list_effect_intents(request.operation_id) == []


def test_missing_profile_and_home_material_refuses_before_effects(launch_root, monkeypatch):
    """A native session does not carry process-start profile bytes by itself.

    If B1 recorded neither profile nor provider-home material, B3 must not
    silently read the ambient HOME/default profile when it recreates the CLI.
    """
    bind, contract = _dormant_worker(
        launch_root,
        _contract_changes={
            "profile_material": rc.ContractFact.unavailable("profile was not captured"),
            "provider_home_facts": rc.ContractFact.unavailable("home was not captured"),
        },
    )
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])

    with pytest.raises(xe.ExactExecutorRefused) as excinfo:
        _run(_execute(request, transport))

    assert "profile" in str(excinfo.value).lower()
    assert "ambient" in str(excinfo.value).lower()
    assert transport.created == 0
    assert oj.list_effect_intents(request.operation_id) == []
    assert oj.get_operation(request.operation_id)["successor_terminal_id"] is None


def test_extra_args_restating_the_route_refuse(launch_root, monkeypatch):
    """The route flags are owned by the executor's derivation from the bound
    facts; launch material restating them — even with the same value — can
    never be shown to agree and refuses before any physical effect."""
    bind, contract = _dormant_worker(launch_root)
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    for restated in (
        ["--model", "claude-opus-5"],
        ["--model", "claude-sonnet-4-5"],
        ["--effort", "low"],
    ):
        with pytest.raises(xe.ExactExecutorRefused) as excinfo:
            _run(_execute(request, transport, material=xe.LaunchMaterial(extra_args=restated)))
        assert "route" in str(excinfo.value).lower()
    # The sealed profile args may not shadow the owned pins either.
    with pytest.raises(xe.ExactExecutorRefused):
        _run(
            _execute(
                request,
                transport,
                material=xe.LaunchMaterial(profile_args=["--effort", "low"]),
            )
        )
    assert transport.created == 0
    assert oj.list_effect_intents(request.operation_id) == []


def test_trusted_project_root_drift_refuses_before_effects(launch_root, monkeypatch, tmp_path):
    """The stored trusted project root is validated before effects: a root
    that no longer exists as a canonical real directory refuses the launch."""
    trusted_root = os.path.realpath(str(tmp_path / "trusted"))
    os.makedirs(trusted_root, exist_ok=True)
    bind, contract = _codex_dormant_worker(launch_root, trusted_root)
    request = _codex_request(bind, contract)
    assert oj.claim_operation(request)["adopted"] is False
    import shutil

    shutil.rmtree(trusted_root)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(xe.ExactExecutorRefused) as excinfo:
        _run(_execute(request, transport))
    assert "trusted project root" in str(excinfo.value)
    assert transport.created == 0
    assert oj.list_effect_intents(request.operation_id) == []


@pytest.mark.parametrize("material_field", ["extra_args", "profile_args"])
def test_codex_material_cannot_shadow_the_contract_trusted_root(
    launch_root, monkeypatch, material_field
):
    """A later Codex projects override cannot win over contract-owned trust.

    Exact resume adds the canonical trusted root before sealed material.  A
    second projects override in either later material lane would make option
    precedence, rather than the restore contract, decide which root is trusted.
    """
    bind, contract = _codex_dormant_worker(launch_root, launch_root["workdir"])
    request = _codex_request(bind, contract)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    material = xe.LaunchMaterial(
        **{
            material_field: [
                "-c",
                'projects={"/tmp/other"={trust_level="trusted"}}',
            ]
        }
    )

    with pytest.raises(xe.ExactExecutorRefused) as excinfo:
        _run(_execute(request, transport, material=material))

    assert "trusted project root" in str(excinfo.value)
    assert transport.created == 0
    assert oj.list_effect_intents(request.operation_id) == []


def test_trusted_project_root_drift_at_launch_is_a_retryable_typed_refusal(
    launch_root, monkeypatch
):
    """A root can drift after the pre-effect check but before argv rendering.

    At that point the prior incarnation is already reaped and detached, but
    no successor effect may be authorized.  Preserve that truthful phase as
    a durable retryable refusal instead of leaking a bare ``ValueError``.
    """
    bind, contract = _codex_dormant_worker(launch_root, launch_root["workdir"])
    request = _codex_request(bind, contract)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])

    def _drifted_root(_root):
        raise ValueError("trusted project root is no longer canonical")

    monkeypatch.setattr(xe, "render_trusted_project_override", _drifted_root)

    with pytest.raises(xe.ExactExecutorRefused) as excinfo:
        _run(_execute(request, transport))

    assert "drifted before successor launch" in str(excinfo.value)
    assert transport.created == 0
    effects = oj.list_effect_intents(request.operation_id)
    assert [effect["effect_step"] for effect in effects] == [
        oj.EFFECT_STEP_FENCE_PRIOR,
        oj.EFFECT_STEP_REAP_PRIOR,
        oj.EFFECT_STEP_RELEASE_ATTACHMENT,
    ]
    result = oj.get_result(request.operation_id)
    assert result["result_state"] == oj.RESULT_REFUSED
    assert "no successor effect was authorized" in result["result_detail"]


def test_trusted_project_root_on_a_non_codex_harness_refuses(launch_root, monkeypatch):
    """A trusted project root applies only to the Codex harness: a contract
    carrying one for any other harness cannot authorize an exact launch."""
    bind, contract = _dormant_worker(
        launch_root, _contract_changes={"trusted_project_root": launch_root["workdir"]}
    )
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(xe.ExactExecutorRefused) as excinfo:
        _run(_execute(request, transport))
    assert "codex" in str(excinfo.value).lower()
    assert transport.created == 0
    assert oj.list_effect_intents(request.operation_id) == []


def test_working_directory_drift_refuses_before_effects(launch_root, monkeypatch):
    """The stored cwd is a required launch fact: a canonical path that no
    longer exists is a typed refusal before any physical effect."""
    bind, contract = _dormant_worker(launch_root)
    request = _operation_request(bind, contract)
    assert oj.claim_operation(request)["adopted"] is False
    gone = os.path.realpath("/definitely/not/a/real/dir/b3")

    def _mutate(payload):
        payload["working_directory"] = gone

    _retamper_contract(bind, _mutate)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(xe.ExactExecutorRefused) as excinfo:
        _run(_execute(request, transport))
    assert "working" in str(excinfo.value).lower() or "directory" in str(excinfo.value).lower()
    assert transport.created == 0
    assert oj.list_effect_intents(request.operation_id) == []


# ---------------------------------------------------------------------------
# 4. exact old generation fenced and reaped; replacements untouched
# ---------------------------------------------------------------------------


def test_reused_terminal_id_preserves_the_replacement_and_refuses(launch_root, monkeypatch):
    """A prior terminal id that now names a replacement generation receives
    zero destructive action: the exact-generation teardown raises its typed
    mismatch and the executor surfaces it without any successor pane."""
    bind, contract = _dormant_worker(launch_root)
    request = _operation_request(bind, contract)
    reap = ReapRecorder(
        error=terminal_service.TerminalGenerationMismatchError(
            f"terminal a1b2c3d4 generation mismatch; expected {contract.generation!r}"
        )
    )
    monkeypatch.setattr(terminal_service, "delete_terminal", reap)
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(xe.ExactExecutorConflict):
        _run(_execute(request, transport))
    assert len(reap.calls) == 1
    assert reap.calls[0]["release_native_attachments"] is False
    assert reap.calls[0]["retire_roster"] is False
    assert transport.created == 0
    stored = oj.get_operation(request.operation_id)
    assert stored["successor_terminal_id"] is not None  # reservation is durable
    assert [i["effect_step"] for i in oj.list_effect_intents(request.operation_id)] == [
        oj.EFFECT_STEP_FENCE_PRIOR,
        oj.EFFECT_STEP_REAP_PRIOR,
    ]
    assert xe.get_result(request.operation_id)["result_state"] == xe.OUTCOME_REFUSED


def test_fence_step_refuses_a_re_livened_source(launch_root, monkeypatch):
    """The fenced-source boundary refuses when a concurrent successor has
    already re-livened the agent: zero bytes, zero panes."""
    bind, contract = _dormant_worker(launch_root)
    request = _operation_request(bind, contract)
    assert oj.claim_operation(request)["adopted"] is False
    successor = roster.bind_generation(
        roster.BindingContract(
            agent_id=bind["agent"]["agent_id"],
            session_name="cao-campaign-a",
            role=roster.ROLE_WORKER,
            profile_family="developer",
            harness="claude_code",
            native_session_id=_NATIVE_ID,
            acquisition_method="pinned_resume",
            terminal_id="b2c3d4e5",
            generation=str(uuid.uuid4()),
            execution_mode="native_tui",
        )
    )
    assert successor["agent"]["disposition"] == roster.DISPOSITION_LIVE
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(xe.ExactExecutorError):
        _run(_execute(request, transport))
    assert transport.created == 0
    assert oj.list_effect_intents(request.operation_id) == []


# ---------------------------------------------------------------------------
# 5. no-survivor release / competing ownership
# ---------------------------------------------------------------------------


def test_live_competing_owner_refuses_with_no_successor_pane(launch_root, monkeypatch):
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment(pid=os.getpid())  # the old process is still running
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(xe.ExactExecutorRefused) as excinfo:
        _run(_execute(request, transport))
    assert "alive" in str(excinfo.value).lower()
    assert transport.created == 0
    assert na.get("claude_code", _NATIVE_ID)["owner"]["terminal_id"] == "a1b2c3d4"


def test_unobservable_owner_refuses_with_no_successor_pane(launch_root, monkeypatch):
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)

    def _unobservable(record, **kwargs):
        return {
            "disposition": recovery.OWNER_UNOBSERVABLE,
            "survivors": [{"pid": 1, "start_marker": None}],
            "observed_at": "2026-08-14T00:00:00Z",
            "observer": recovery.OBSERVER,
            "detail": "pid could not be checked; an unchecked owner is treated as alive",
        }

    monkeypatch.setattr(recovery, "observe_owner", _unobservable)
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(xe.ExactExecutorRefused):
        _run(_execute(request, transport))
    assert transport.created == 0


def test_frozen_prior_attachment_refuses_at_acquire(launch_root, monkeypatch):
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    na.mark_ambiguous(
        provider="claude_code",
        native_session_id=_NATIVE_ID,
        reason="pane_argv_does_not_resume_bound_session",
    )
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(xe.ExactExecutorRefused):
        _run(_execute(request, transport))
    assert transport.created == 0


def test_absent_prior_attachment_still_restores(launch_root, monkeypatch):
    """No prior attachment row at all (legacy/ACP source): nothing to
    release, the successor acquires the session cleanly."""
    bind, contract = _dormant_worker(launch_root)
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    result = _run(_execute(request, transport))
    assert result["outcome"] == xe.OUTCOME_ACCEPTED


# ---------------------------------------------------------------------------
# 6. the exact launch form
# ---------------------------------------------------------------------------


def test_launch_goes_through_resume_with_pinned_facts(launch_root, monkeypatch, input_spies):
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)

    calls: list = []
    original_start = native_tui_launch.start

    def _spy_start(**kwargs):
        calls.append(kwargs)
        return original_start(**kwargs)

    monkeypatch.setattr(native_tui_launch, "start", _spy_start)
    transport = FakeTransport(workdir=launch_root["workdir"])
    result = _run(_execute(request, transport))

    assert len(calls) == 1
    call = calls[0]
    assert call["launch_kind"] == native_tui_launch.LAUNCH_KIND_RESUME
    assert call["native_session_id"] == _NATIVE_ID
    assert call["provider"] == "claude_code"
    assert call["binary"] == launch_root["binary"]
    assert call["binary_sha256"] == launch_root["binary_sha256"]
    assert call["working_directory"] == launch_root["workdir"]
    assert call["terminal_id"] == result["successor_terminal_id"]
    assert call["generation"] == result["successor_generation"]
    assert call["execution_mode"] == "native_tui"
    assert call["intent"]["acquisition_method"] == na.ACQUISITION_RESUME
    assert call["intent"]["replays_task_bytes"] is False
    # The resumed pane's argv carries no task bytes: it is exactly the
    # provider's resume form plus the executor-pinned route.
    assert transport.argv[0] == launch_root["binary"]
    assert "--resume" in transport.argv
    assert _NATIVE_ID in transport.argv
    # The effective route reaches the exact resume launch as argv pins, and
    # the contract's exact profile config reaches it through the explicit
    # ``--settings`` lane — the resumed harness never reads ambient settings.
    assert call["extra_args"] == [
        "--model",
        "claude-sonnet-4-5",
        "--effort",
        "high",
        "--settings",
        launch_root["profile_path"],
    ]
    # No fresh/managed-resume helper is ever invoked.
    assert input_spies.touched == []


def test_default_transport_creates_the_managed_successor_terminal(launch_root, monkeypatch):
    """The default pane transport creates the successor through the managed
    native terminal seam: reserved successor id/generation, the resume argv
    as the pane's own process, no shell, no input."""
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)

    created: list = []

    class _Terminal:
        id = None

    async def _fake_create_terminal(**kwargs):
        created.append(kwargs)
        terminal = _Terminal()
        terminal.id = kwargs["reserved_terminal_id"]
        return terminal

    monkeypatch.setattr(terminal_service, "create_terminal", _fake_create_terminal)

    class _FakeTmuxPane:
        def __init__(self, *args, **kwargs):
            pass

        def observe(self):
            return {
                "pane_id": "%77",
                "pid": os.getpid(),
                "start_marker": "2026-08-14T00:00:00Z",
                "argv": created[-1]["managed_native_command"],
                "cwd": launch_root["workdir"],
            }

        def capture_render(self, pane_id):
            return []

    monkeypatch.setattr(native_tui_launch, "TmuxNativePane", _FakeTmuxPane)
    result = _run(xe.execute(request))
    assert result["outcome"] == xe.OUTCOME_ACCEPTED
    assert len(created) == 1
    kwargs = created[0]
    assert kwargs["reserved_terminal_id"] == result["successor_terminal_id"]
    assert kwargs["terminal_generation"] == result["successor_generation"]
    # The successor is a reserved managed generation: it persists ONLY on the
    # isolated managed-v2 surface, never as a legacy terminal row.
    assert kwargs["protocol_vintage"] == "v2"
    argv = kwargs["managed_native_command"]
    assert argv[0] == launch_root["binary"]
    # The effective requested-or-stored route is pinned on the resume argv
    # itself, not only in terminal metadata — and the contract's exact
    # profile config is applied through the explicit ``--settings`` lane.
    assert argv[1:3] == ["--resume", _NATIVE_ID]
    assert argv[3:5] == ["--model", "claude-sonnet-4-5"]
    assert argv[5:7] == ["--effort", "high"]
    assert argv[7:9] == ["--settings", launch_root["profile_path"]]
    assert kwargs["new_session"] is False
    assert kwargs["session_name"] == "cao-campaign-a"
    assert kwargs["working_directory"] == launch_root["workdir"]
    assert kwargs["native_status_source"] is True
    assert kwargs["preserve_on_init_failure"] is True
    assert kwargs.get("initial_message") is None


def test_codex_pins_and_trusted_root_reach_the_managed_creation(launch_root, monkeypatch):
    """The Codex exact resume: the validated trusted project root is passed
    exactly to managed terminal creation, the route is pinned in the codex
    form (``--model`` + the ``-c model_reasoning_effort`` override), and the
    successor persists on the managed-v2 surface."""
    bind, contract = _codex_dormant_worker(launch_root, launch_root["workdir"])
    request = _codex_request(bind, contract)
    _reap_noop(monkeypatch)

    created: list = []

    class _Terminal:
        id = None

    async def _fake_create_terminal(**kwargs):
        created.append(kwargs)
        terminal = _Terminal()
        terminal.id = kwargs["reserved_terminal_id"]
        return terminal

    monkeypatch.setattr(terminal_service, "create_terminal", _fake_create_terminal)

    class _FakeTmuxPane:
        def __init__(self, *args, **kwargs):
            pass

        def observe(self):
            return {
                "pane_id": "%78",
                "pid": os.getpid(),
                "start_marker": "2026-08-14T00:00:00Z",
                "argv": created[-1]["managed_native_command"],
                "cwd": launch_root["workdir"],
            }

        def capture_render(self, pane_id):
            return []

    monkeypatch.setattr(native_tui_launch, "TmuxNativePane", _FakeTmuxPane)
    result = _run(xe.execute(request))
    assert result["outcome"] == xe.OUTCOME_ACCEPTED
    assert len(created) == 1
    kwargs = created[0]
    assert kwargs["protocol_vintage"] == "v2"
    assert kwargs["trusted_project_root"] == launch_root["workdir"]
    # The contract's exact provider home reaches the launch through the
    # explicit CODEX_HOME carrier lane — never an ambient HOME default.
    assert kwargs["env_vars"]["CODEX_HOME"] == launch_root["home_dir"]
    argv = kwargs["managed_native_command"]
    # Codex route and invocation-only trust options precede the resume
    # subcommand; the identity pair stays final. Passing the trusted root as
    # terminal metadata alone is insufficient because managed_native_command
    # bypasses the provider command builder.
    assert argv[-2:] == ["resume", _NATIVE_ID]
    assert argv[:7] == [
        launch_root["binary"],
        "--model",
        "gpt-5.3-codex",
        "-c",
        'model_reasoning_effort="high"',
        "-c",
        render_trusted_project_override(launch_root["workdir"]),
    ]


def test_muse_resume_revalidates_the_profile_carrier_and_proves_its_inner_image(
    launch_root, monkeypatch
):
    """The canaried wrapper may exec only its exact proven Muse inner image."""
    bind = _bind_worker(
        harness="muse_cli",
        route_provenance={"provider_route": "meta"},
    )
    full_banner = "Muse Code 0.1.0 (0.1.0-R708.1)"
    inner = os.path.realpath(os.path.join(launch_root["workdir"], "muse-bin-0.1.0-R708.1"))
    with open(inner, "wb") as handle:
        handle.write(b"#!/bin/sh\nsleep 60\n")
    os.chmod(inner, os.stat(inner).st_mode | stat.S_IXUSR)
    inner_digest = hashlib.sha256(open(inner, "rb").read()).hexdigest()
    contract = _contract_for(
        bind,
        launch_root,
        harness="muse_cli",
        provider="muse_cli",
        model=_fact("muse-spark-1.2"),
        effort=_fact("high"),
        executable=_fact(
            {
                "path": launch_root["binary"],
                "sha256": launch_root["binary_sha256"],
                "version": full_banner,
            }
        ),
        profile_material=_fact(
            {
                "profile_system_prompt_path": launch_root["profile_path"],
                "profile_system_prompt_sha256": launch_root["profile_sha256"],
            }
        ),
    )
    rc.publish_contract(contract)
    roster.transition_dormant(
        terminal_id=contract.terminal_id,
        generation=contract.generation,
        agent_id=contract.agent_id,
        lineage_id=contract.lineage_id,
        contract_digest=contract.digest(),
        reason="pane lost",
    )
    request = _operation_request(
        bind,
        contract,
        harness="muse_cli",
        route_provider="muse_cli",
        model_requested="muse-spark-1.2",
        effort_requested="high",
        compatibility_cell_ref="muse_cli:meta:native_tui:r708.1",
    )
    capability = muse_native_launch.MuseProfileCarrierCapability(
        True,
        "",
        cell=muse_native_launch.PROFILE_CARRIER_CAPABILITY_CELL,
        full_banner=full_banner,
        inner_executable=inner,
        inner_executable_sha256=inner_digest,
    )
    monkeypatch.setattr(
        muse_native_launch,
        "profile_carrier_capability",
        lambda **_kwargs: capability,
    )
    _prior_attachment(provider="muse_cli")
    _reap_noop(monkeypatch)
    transport = FakeTransport(
        workdir=launch_root["workdir"],
        observed_argv=[
            inner,
            "resume",
            _NATIVE_ID,
            "--model",
            "muse-spark-1.2",
            "--reasoning-effort",
            "high",
            "--trust-workspace",
            "--yolo",
        ],
    )
    material = xe.LaunchMaterial(
        profile_args=["--trust-workspace", "--yolo"],
        profile_environment={
            muse_native_launch.PROFILE_SYSTEM_PROMPT_ENV: launch_root["profile_path"],
            "MUSE_NO_AUTO_UPDATE": "1",
        },
    )
    launch_call: dict[str, Any] = {}
    original_start = native_tui_launch.start

    def _spy_start(**kwargs):
        launch_call.update(kwargs)
        return original_start(**kwargs)

    monkeypatch.setattr(native_tui_launch, "start", _spy_start)

    result = _run(_execute(request, transport, material=material))

    assert result["outcome"] == xe.OUTCOME_ACCEPTED
    assert transport.argv == [
        launch_root["binary"],
        "resume",
        _NATIVE_ID,
        "--model",
        "muse-spark-1.2",
        "--reasoning-effort",
        "high",
        "--trust-workspace",
        "--yolo",
    ]
    assert launch_call["expected_inner_executable"] == inner
    assert launch_call["expected_inner_executable_sha256"] == inner_digest
    evidence = oj.get_operation(request.operation_id)["result_evidence"]
    assert evidence["profile_carrier_inner_sha256"] == inner_digest


def test_muse_profile_carrier_drift_refuses_before_effects(launch_root, monkeypatch):
    """A changed wrapper/inner pair cannot consume the exact Muse cell."""
    bind = _bind_worker(
        harness="muse_cli",
        route_provenance={"provider_route": "meta"},
    )
    contract = _contract_for(
        bind,
        launch_root,
        harness="muse_cli",
        provider="muse_cli",
        model=_fact("muse-spark-1.2"),
        effort=_fact("high"),
        executable=_fact(
            {
                "path": launch_root["binary"],
                "sha256": launch_root["binary_sha256"],
                "version": "Muse Code 0.1.0 (0.1.0-R708.1)",
            }
        ),
        profile_material=_fact(
            {
                "profile_system_prompt_path": launch_root["profile_path"],
                "profile_system_prompt_sha256": launch_root["profile_sha256"],
            }
        ),
    )
    rc.publish_contract(contract)
    roster.transition_dormant(
        terminal_id=contract.terminal_id,
        generation=contract.generation,
        agent_id=contract.agent_id,
        lineage_id=contract.lineage_id,
        contract_digest=contract.digest(),
        reason="pane lost",
    )
    request = _operation_request(
        bind,
        contract,
        harness="muse_cli",
        route_provider="muse_cli",
        model_requested="muse-spark-1.2",
        effort_requested="high",
        compatibility_cell_ref="muse_cli:meta:native_tui:r708.1",
    )
    monkeypatch.setattr(
        muse_native_launch,
        "profile_carrier_capability",
        lambda **_kwargs: muse_native_launch.MuseProfileCarrierCapability(
            False,
            "the installed wrapper digest no longer selects the proven inner image",
        ),
    )
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])

    with pytest.raises(xe.ExactExecutorRefused, match="profile carrier"):
        _run(_execute(request, transport))

    assert transport.created == 0
    assert oj.list_effect_intents(request.operation_id) == []
    assert oj.get_operation(request.operation_id)["successor_terminal_id"] is None


def test_route_pin_maps_each_supported_harness():
    """The provider-aware route pinning: model/effort land on the argv or
    the environment in the harness's own native pattern, and only Codex may
    omit an unknown model (its bootstrap records the provider's actual)."""
    args, env = xe._route_pin("claude_code", "claude-sonnet-4-5", "high")
    assert args == ["--model", "claude-sonnet-4-5", "--effort", "high"]
    assert env == {}

    args, env = xe._route_pin("kimi_cli", "k2.7", None)
    assert args == ["--model", "k2.7"]
    assert env == {}
    args, env = xe._route_pin("kimi_cli", "k2.7", "max")
    assert env == {"KIMI_MODEL_THINKING_EFFORT": "max"}

    args, env = xe._route_pin("muse_cli", "muse-spark-1.2", "low")
    assert args == ["--model", "muse-spark-1.2", "--reasoning-effort", "low"]
    assert env == {}

    args, env = xe._route_pin("codex", "gpt-5.3-codex", None)
    assert args == ["--model", "gpt-5.3-codex"]
    assert env == {}
    # Provider-default effort is omitted, never emitted as a literal.
    args, env = xe._route_pin("codex", None, "provider-default")
    assert args == []

    for harness in ("claude_code", "kimi_cli", "muse_cli"):
        with pytest.raises(xe.ExactExecutorRefused):
            xe._route_pin(harness, None, None)
    with pytest.raises(xe.ExactExecutorRefused):
        xe._route_pin("opencode", "model", None)


def test_route_pin_accepts_cell_bound_route_material_for_an_unattestable_route():
    """A cell-covered claude_code route the Anthropic validator cannot
    attest (a DeepSeek/GLM-style route) uses the material's explicit,
    bounded route args instead — validated for exactly one model pin."""
    args, env = xe._route_pin(
        "claude_code",
        "deepseek-v4",
        "high",
        route_args=("--model", "deepseek-v4", "--base-url", "https://deepseek.example/anthropic"),
        route_environment={"ANTHROPIC_AUTH_TOKEN": "carrier"},
        cell_covered=True,
    )
    assert args == [
        "--model",
        "deepseek-v4",
        "--base-url",
        "https://deepseek.example/anthropic",
        "--effort",
        "high",
    ]
    assert env == {"ANTHROPIC_AUTH_TOKEN": "carrier"}

    # The material is only lawful when the normal validator cannot attest
    # the route AND a cell covers the variation.
    with pytest.raises(xe.ExactExecutorRefused):
        xe._route_pin("claude_code", "deepseek-v4", None, route_args=("--model", "deepseek-v4"))
    with pytest.raises(xe.ExactExecutorRefused):
        xe._route_pin(
            "claude_code",
            "claude-sonnet-4-5",
            None,
            route_args=("--model", "claude-sonnet-4-5"),
            cell_covered=True,
        )


def test_exact_unattestable_route_uses_its_named_cell_material(launch_root, monkeypatch):
    """Restoring an already-recorded DeepSeek/GLM-style Claude-harness route
    still needs its explicit cell-bound carrier material.  It is not a
    semantic route change, but the normal Anthropic validator cannot build it.
    """
    bind, contract = _dormant_worker(
        launch_root,
        route_provenance={"provider_route": "deepseek"},
        _contract_changes={"provider": "deepseek", "model": _fact("deepseek-v4")},
    )
    request = _operation_request(
        bind,
        contract,
        route_provider="deepseek",
        model_requested="deepseek-v4",
        compatibility_cell_ref="claude_code:deepseek:native_tui",
        compatibility_cell_digest="e" * 64,
    )
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    material = xe.LaunchMaterial(
        route_args=("--model", "deepseek-v4", "--base-url", "https://deepseek.example/anthropic"),
        route_environment={"ANTHROPIC_AUTH_TOKEN": "carrier"},
    )

    result = _run(_execute(request, transport, material=material))

    assert result["outcome"] == xe.OUTCOME_ACCEPTED
    assert transport.argv.count("--model") == 1
    assert transport.argv[transport.argv.index("--model") + 1] == "deepseek-v4"


def test_route_material_cannot_be_shadowed_by_later_extra_args(launch_root, monkeypatch):
    """A stale general-extra layer must not override a canaried route flag.

    The same operation would otherwise launch a different endpoint depending
    on provider option precedence even though its route cell was unchanged.
    """
    bind, contract = _dormant_worker(launch_root)
    request = _operation_request(bind, contract, model_requested="deepseek-v4")
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    material = xe.LaunchMaterial(
        route_args=("--model", "deepseek-v4", "--base-url", "https://route.example"),
        extra_args=("--base-url", "https://stale.example"),
    )

    with pytest.raises(xe.ExactExecutorRefused) as excinfo:
        _run(_execute(request, transport, material=material))

    assert "precedence" in str(excinfo.value).lower() or "restate" in str(excinfo.value).lower()
    assert transport.created == 0
    assert oj.list_effect_intents(request.operation_id) == []


def test_route_environment_cannot_overlap_selected_home_lane(launch_root, monkeypatch):
    """A route carrier must not provide a second CLAUDE_CONFIG_DIR beside
    the selected provider-home lane; silently overwriting either value would
    make accepted material differ from the actual process environment.
    """
    bind, contract = _dormant_worker(
        launch_root,
        _contract_changes={
            "provider_home_facts": _fact({"provider_home_path": launch_root["home_dir"]})
        },
    )
    request = _operation_request(bind, contract, model_requested="deepseek-v4")
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    material = xe.LaunchMaterial(
        route_args=("--model", "deepseek-v4"),
        route_environment={"CLAUDE_CONFIG_DIR": launch_root["workdir"]},
    )

    with pytest.raises(xe.ExactExecutorRefused) as excinfo:
        _run(_execute(request, transport, material=material))

    assert "CLAUDE_CONFIG_DIR" in str(excinfo.value)
    assert transport.created == 0
    assert oj.list_effect_intents(request.operation_id) == []
    # Precedence protection: a duplicate or wrong model pin is refused.
    with pytest.raises(xe.ExactExecutorRefused):
        xe._route_pin(
            "claude_code",
            "deepseek-v4",
            None,
            route_args=("--model", "deepseek-v4", "--model", "deepseek-v4"),
            cell_covered=True,
        )
    with pytest.raises(xe.ExactExecutorRefused):
        xe._route_pin(
            "claude_code",
            "deepseek-v4",
            None,
            route_args=("--model", "glm-5.3"),
            cell_covered=True,
        )


def test_profile_material_values_stay_out_of_durable_evidence(launch_root, monkeypatch):
    """Durable request/result evidence carries digests and references only:
    the profile path and home path are contract facts, never executor
    evidence — even though both reach the launch itself."""
    bind, contract = _dormant_worker(
        launch_root,
        _contract_changes={
            "provider_home_facts": _fact({"provider_home_path": launch_root["home_dir"]})
        },
    )
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    result = _run(_execute(request, transport))
    assert result["outcome"] == xe.OUTCOME_ACCEPTED
    stored = oj.get_operation(request.operation_id)
    blob = stored["request_json"] + (stored.get("result_evidence_json") or "")
    assert launch_root["profile_path"] not in blob
    assert launch_root["home_dir"] not in blob
    assert "keychain" not in blob
    evidence = stored["result_evidence"]
    assert evidence["profile_material_digest"] == xe._reference_dict_digest(
        dict(contract.profile_material.value)
    )
    assert evidence["provider_home_digest"] == xe._reference_dict_digest(
        dict(contract.provider_home_facts.value)
    )


def _capture_default_transport(monkeypatch, workdir):
    """Fake ``create_terminal``/``TmuxNativePane`` pair for the real default
    transport: records the create kwargs and observes the pane running
    exactly the argv it was created with."""
    created: list = []

    class _Terminal:
        id = None

    async def _fake_create_terminal(**kwargs):
        created.append(kwargs)
        terminal = _Terminal()
        terminal.id = kwargs["reserved_terminal_id"]
        return terminal

    monkeypatch.setattr(terminal_service, "create_terminal", _fake_create_terminal)

    class _FakeTmuxPane:
        def __init__(self, *args, **kwargs):
            pass

        def observe(self):
            return {
                "pane_id": "%79",
                "pid": os.getpid(),
                "start_marker": "2026-08-14T00:00:00Z",
                "argv": created[-1]["managed_native_command"],
                "cwd": workdir,
            }

        def capture_render(self, pane_id):
            return []

    monkeypatch.setattr(native_tui_launch, "TmuxNativePane", _FakeTmuxPane)
    return created


def test_exact_provider_home_reaches_the_env_lane(launch_root, monkeypatch):
    """The contract's exact provider home reaches the resumed harness
    through the explicit carrier env lane — the ambient HOME default can
    never satisfy the recorded contract."""
    bind, contract = _dormant_worker(
        launch_root,
        _contract_changes={
            "provider_home_facts": _fact({"provider_home_path": launch_root["home_dir"]})
        },
    )
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    created = _capture_default_transport(monkeypatch, launch_root["workdir"])
    result = _run(xe.execute(request))
    assert result["outcome"] == xe.OUTCOME_ACCEPTED
    env_vars = created[0]["env_vars"]
    assert env_vars["CLAUDE_CONFIG_DIR"] == launch_root["home_dir"]
    assert env_vars["CLAUDE_CONFIG_DIR"] != os.path.expanduser("~/.claude")


def test_unmappable_profile_reference_requires_sealed_args_or_disables(launch_root, monkeypatch):
    """A selected reference with no defined provider lane must reach the
    launch through the material's explicit sealed profile args — otherwise
    the cell is typed-disabled before the successor is reserved."""
    mcp = os.path.realpath(os.path.join(os.path.dirname(launch_root["profile_path"]), "mcp.json"))
    with open(mcp, "wb") as handle:
        handle.write(b'{"mcpServers": {}}\n')
    mcp_digest = hashlib.sha256(open(mcp, "rb").read()).hexdigest()

    def _worker(tag: str):
        return _dormant_worker(
            launch_root,
            terminal_id=f"{tag}234567"[:8],
            native_session_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"m3b3-mcp-{tag}")),
            _contract_changes={
                "profile_material": _fact({"mcp_config_path": mcp, "mcp_config_sha256": mcp_digest})
            },
        )

    # No sealed application: typed-disabled before reservation/effects.
    bind, contract = _worker("aa")
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(xe.ExactExecutorRefused) as excinfo:
        _run(_execute(request, transport))
    assert "mcp_config_path" in str(excinfo.value)
    assert transport.created == 0
    assert oj.get_operation(request.operation_id)["successor_terminal_id"] is None
    assert oj.list_effect_intents(request.operation_id) == []

    # Sealed args referencing a FOREIGN path never verify against the
    # selected mapping.
    bind, contract = _worker("bb")
    request = _operation_request(bind, contract)
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(xe.ExactExecutorRefused):
        _run(
            _execute(
                request,
                transport,
                material=xe.LaunchMaterial(profile_args=["--mcp-config", "/etc/passwd"]),
            )
        )
    assert transport.created == 0

    # Sealed args referencing exactly the selected path: applied.
    bind, contract = _dormant_worker(
        launch_root,
        _contract_changes={
            "profile_material": _fact({"mcp_config_path": mcp, "mcp_config_sha256": mcp_digest})
        },
    )
    _prior_attachment()
    request = _operation_request(bind, contract)
    transport = FakeTransport(workdir=launch_root["workdir"])
    result = _run(
        _execute(
            request,
            transport,
            material=xe.LaunchMaterial(profile_args=["--mcp-config", mcp]),
        )
    )
    assert result["outcome"] == xe.OUTCOME_ACCEPTED
    mcp_at = transport.argv.index("--mcp-config")
    assert transport.argv[mcp_at + 1] == mcp


def test_stored_model_effort_fall_back_to_argv_and_terminal_metadata(launch_root, monkeypatch):
    """A request that omits model/effort uses the stored contract values —
    the SAME effective values pin the resume argv and the managed terminal
    metadata."""
    bind, contract = _dormant_worker(launch_root)
    request = _operation_request(bind, contract, model_requested=None, effort_requested=None)
    _reap_noop(monkeypatch)
    created = _capture_default_transport(monkeypatch, launch_root["workdir"])
    result = _run(xe.execute(request))
    assert result["outcome"] == xe.OUTCOME_ACCEPTED
    kwargs = created[0]
    assert kwargs["expected_model"] == "claude-sonnet-4-5"
    assert kwargs["expected_effort"] == "high"
    argv = kwargs["managed_native_command"]
    assert argv[3:5] == ["--model", "claude-sonnet-4-5"]
    assert argv[5:7] == ["--effort", "high"]


def test_cell_covered_non_anthropic_route_reaches_the_resume_launch(launch_root, monkeypatch):
    """A DeepSeek/GLM-style route through the claude_code harness: the exact
    harness-named cell covers the variation and the material's explicit
    route args — not the Anthropic validator — pin the resume argv."""
    bind, contract = _dormant_worker(launch_root)
    request = _operation_request(
        bind,
        contract,
        model_requested="deepseek-v4",
        compatibility_cell_ref="claude_code:deepseek:native_tui",
        compatibility_cell_digest="e" * 64,
    )
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    material = xe.LaunchMaterial(route_args=["--model", "deepseek-v4"])
    result = _run(_execute(request, transport, material=material))
    assert result["outcome"] == xe.OUTCOME_ACCEPTED
    assert transport.argv.count("--model") == 1
    model_at = transport.argv.index("--model")
    assert transport.argv[model_at + 1] == "deepseek-v4"
    # The stored effort is still pinned by the executor, after the material.
    effort_at = transport.argv.index("--effort")
    assert transport.argv[effort_at + 1] == "high"
    assert effort_at > model_at


def test_non_anthropic_route_without_cell_or_material_refuses(launch_root, monkeypatch):
    _reap_noop(monkeypatch)

    # No cell: the variation gate refuses before effects.
    bind, contract = _dormant_worker(
        launch_root, terminal_id="b2c3d4e5", native_session_id=_OTHER_NATIVE_ID
    )
    request = _operation_request(
        bind,
        contract,
        model_requested="deepseek-v4",
        compatibility_cell_ref=None,
        compatibility_cell_digest=None,
    )
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(xe.ExactExecutorRefused):
        _run(_execute(request, transport))
    assert transport.created == 0
    assert oj.list_effect_intents(request.operation_id) == []

    # The exact cell but no explicit route material: the Anthropic
    # validator cannot attest the route, so it refuses before effects.
    bind, contract = _dormant_worker(launch_root)
    request = _operation_request(
        bind,
        contract,
        model_requested="deepseek-v4",
        compatibility_cell_ref="claude_code:deepseek:native_tui",
        compatibility_cell_digest="e" * 64,
    )
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(xe.ExactExecutorRefused) as excinfo:
        _run(_execute(request, transport))
    assert "route" in str(excinfo.value).lower()
    assert transport.created == 0
    assert oj.list_effect_intents(request.operation_id) == []


def test_route_material_on_an_exact_restore_refuses(launch_root, monkeypatch):
    """Route args without a cell-covered variation would shadow the
    executor-derived pin: refused before any effect."""
    bind, contract = _dormant_worker(launch_root)
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    material = xe.LaunchMaterial(route_args=["--model", "claude-sonnet-4-5"])
    with pytest.raises(xe.ExactExecutorRefused) as excinfo:
        _run(_execute(request, transport, material=material))
    assert "route" in str(excinfo.value).lower()
    assert transport.created == 0
    assert oj.list_effect_intents(request.operation_id) == []


def test_provider_version_defaults_to_the_stored_executable_version(launch_root, monkeypatch):
    """The identity-proof selector is the contract's recorded executable
    version when the material names none — never an ambient guess."""
    bind, contract = _dormant_worker(
        launch_root,
        _contract_changes={
            "executable": _fact(
                {
                    "path": launch_root["binary"],
                    "sha256": launch_root["binary_sha256"],
                    "version": "2.1.0",
                }
            )
        },
    )
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)

    calls: list = []
    original_start = native_tui_launch.start

    def _spy_start(**kwargs):
        calls.append(kwargs)
        return original_start(**kwargs)

    monkeypatch.setattr(native_tui_launch, "start", _spy_start)
    transport = FakeTransport(workdir=launch_root["workdir"])
    result = _run(_execute(request, transport))
    assert result["outcome"] == xe.OUTCOME_ACCEPTED
    assert calls[0]["provider_version"] == "2.1.0"


def test_provider_version_difference_is_cell_gated(launch_root, monkeypatch):
    """A provider-version hint that differs from the recorded executable
    version changes the identity proof, so it is a variation: refused
    without the exact cell, applied with it."""
    versioned = {
        "executable": _fact(
            {
                "path": launch_root["binary"],
                "sha256": launch_root["binary_sha256"],
                "version": "2.1.0",
            }
        )
    }
    _reap_noop(monkeypatch)

    bind, contract = _dormant_worker(
        launch_root,
        terminal_id="b2c3d4e5",
        native_session_id=_OTHER_NATIVE_ID,
        _contract_changes=versioned,
    )
    request = _operation_request(
        bind, contract, compatibility_cell_ref=None, compatibility_cell_digest=None
    )
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(xe.ExactExecutorRefused) as excinfo:
        _run(_execute(request, transport, material=xe.LaunchMaterial(provider_version="9.9.9")))
    assert "provider version" in str(excinfo.value).lower()
    assert transport.created == 0
    assert oj.list_effect_intents(request.operation_id) == []

    bind, contract = _dormant_worker(launch_root, _contract_changes=versioned)
    _prior_attachment()
    request = _operation_request(bind, contract)
    transport = FakeTransport(workdir=launch_root["workdir"])
    result = _run(
        _execute(request, transport, material=xe.LaunchMaterial(provider_version="9.9.9"))
    )
    assert result["outcome"] == xe.OUTCOME_ACCEPTED


# ---------------------------------------------------------------------------
# 7. identity mismatch / ambiguous outcomes never bind
# ---------------------------------------------------------------------------


def test_identity_mismatch_freezes_and_stays_reconciliation_required(launch_root, monkeypatch):
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    # The pane observably runs a DIFFERENT session than the bound one.
    transport.observed_argv = [
        launch_root["binary"],
        "--resume",
        _OTHER_NATIVE_ID,
    ]
    with pytest.raises(xe.ExactExecutorReconciliation):
        _run(_execute(request, transport))

    stored = xe.get_result(request.operation_id)
    assert stored["result_state"] == xe.OUTCOME_RECONCILIATION_REQUIRED
    assert na.get("claude_code", _NATIVE_ID)["state"] == na.AMBIGUOUS
    agent = roster.get_agent(bind["agent"]["agent_id"])
    assert agent["current_incarnation"]["incarnation_id"] == bind["incarnation"]["incarnation_id"]
    assert len(roster.list_incarnations(agent_id=agent["agent_id"])) == 1
    # The durable reconciliation outcome is terminal for B3: a retry neither
    # re-freezes nor binds.
    transport2 = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(xe.ExactExecutorReconciliation):
        _run(_execute(request, transport2))


def test_pane_create_failure_is_reconciliation_required(launch_root, monkeypatch):
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    transport.create_error = RuntimeError("tmux refused the window")
    with pytest.raises(xe.ExactExecutorReconciliation):
        _run(_execute(request, transport))
    assert xe.get_result(request.operation_id)["result_state"] == (
        xe.OUTCOME_RECONCILIATION_REQUIRED
    )


# ---------------------------------------------------------------------------
# 8. the barrier linearization
# ---------------------------------------------------------------------------


def _materialize_after_stop_then_fail(request, failure_factory, *, entered=None, release=None):
    """A blocking launch that materializes after Stop's first reap scan."""

    def _start(**kwargs):
        authorize = kwargs["authorize"]
        transport = kwargs["transport"]
        authorize(native_tui_launch.AUTHORIZE_BOUNDARY_DECLARE)
        authorize(native_tui_launch.AUTHORIZE_BOUNDARY_CREATE_PANE)
        transport.create_pane(argv=[kwargs["binary"], "--resume", _NATIVE_ID])
        oj.claim_session_barrier(
            request.session_name, claimed_by="force-stop", reason="Stop won before materialization"
        )
        if entered is not None:
            entered.set()
        if release is not None:
            assert release.wait(timeout=5)
        raise failure_factory()

    return _start


@pytest.mark.parametrize("failure_kind", ["ambiguous", "conflict"])
def test_stop_reaps_late_successor_on_every_post_physical_launch_exit(
    launch_root, monkeypatch, failure_kind
):
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)
    reaper = _reap_noop(monkeypatch)
    caller_thread = threading.get_ident()

    def failure_factory():
        if failure_kind == "ambiguous":
            return native_tui_launch.NativeLaunchAmbiguous(
                native_tui_launch.AMBIGUOUS_PANE_CREATE,
                "pane materialized after force Stop's first scan",
            )
        return native_tui_launch.NativeLaunchConflict(
            "post-physical attachment publication conflicted"
        )

    monkeypatch.setattr(
        native_tui_launch,
        "start",
        _materialize_after_stop_then_fail(request, failure_factory),
    )
    transport = FakeTransport(workdir=launch_root["workdir"])

    with pytest.raises(xe.ExactExecutorReconciliation):
        _run(_execute(request, transport))

    reserved = oj.get_operation(request.operation_id)
    assert transport.created == 1
    assert reaper.calls[-1]["terminal_id"] == reserved["successor_terminal_id"]
    assert reaper.calls[-1]["expected_generation"] == reserved["successor_generation"]
    assert reaper.calls[-1]["thread_id"] != caller_thread
    result = oj.get_result(request.operation_id)
    assert result["result_state"] == oj.RESULT_RECONCILIATION_REQUIRED
    assert len(result["result_evidence"]["stop_reap_digest"]) == 64


@pytest.mark.asyncio
async def test_cancellation_waits_for_late_materialization_and_stop_reap(launch_root, monkeypatch):
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)
    reaper = _reap_noop(monkeypatch)
    entered = threading.Event()
    release = threading.Event()

    def _published_then_blocked(**kwargs):
        authorize = kwargs["authorize"]
        transport = kwargs["transport"]
        authorize(native_tui_launch.AUTHORIZE_BOUNDARY_DECLARE)
        authorize(native_tui_launch.AUTHORIZE_BOUNDARY_CREATE_PANE)
        pane_id = transport.create_pane(argv=[kwargs["binary"], "--resume", _NATIVE_ID])
        authorize(native_tui_launch.AUTHORIZE_BOUNDARY_PUBLISH)
        oj.claim_session_barrier(
            request.session_name,
            claimed_by="force-stop",
            reason="Stop won after publication but before worker return",
        )
        entered.set()
        assert release.wait(timeout=5)
        return {
            "outcome": native_tui_launch.OUTCOME_LAUNCHED,
            "session_proof": native_tui_launch.SESSION_PROOF_ARGV,
            "pane_observation": {"pane_id": pane_id},
        }

    monkeypatch.setattr(
        native_tui_launch,
        "start",
        _published_then_blocked,
    )
    transport = FakeTransport(workdir=launch_root["workdir"])
    execution = asyncio.create_task(_execute(request, transport))
    assert await asyncio.to_thread(entered.wait, 5)

    execution.cancel()
    await asyncio.sleep(0)
    assert not execution.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await execution

    reserved = oj.get_operation(request.operation_id)
    assert reaper.calls[-1]["terminal_id"] == reserved["successor_terminal_id"]
    assert reaper.calls[-1]["expected_generation"] == reserved["successor_generation"]
    result = oj.get_result(request.operation_id)
    assert result["result_state"] == oj.RESULT_RECONCILIATION_REQUIRED
    assert len(result["result_evidence"]["stop_reap_digest"]) == 64


@pytest.mark.asyncio
async def test_cancellation_durably_reconciles_a_failed_late_stop_reap(launch_root, monkeypatch):
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    cleanup_calls = []

    def _failed_resource_reap(operation, **_kwargs):
        cleanup_calls.append(dict(operation))
        raise RuntimeError("late exact successor teardown failed")

    from cli_agent_orchestrator.services import cohort_effects

    monkeypatch.setattr(cohort_effects, "reap_reincarnation_resources", _failed_resource_reap)
    entered = threading.Event()
    release = threading.Event()

    def _published_then_blocked(**kwargs):
        authorize = kwargs["authorize"]
        transport = kwargs["transport"]
        authorize(native_tui_launch.AUTHORIZE_BOUNDARY_DECLARE)
        authorize(native_tui_launch.AUTHORIZE_BOUNDARY_CREATE_PANE)
        pane_id = transport.create_pane(argv=[kwargs["binary"], "--resume", _NATIVE_ID])
        authorize(native_tui_launch.AUTHORIZE_BOUNDARY_PUBLISH)
        oj.claim_session_barrier(
            request.session_name,
            claimed_by="force-stop",
            reason="Stop won before the cancelled worker returned",
        )
        entered.set()
        assert release.wait(timeout=5)
        return {
            "outcome": native_tui_launch.OUTCOME_LAUNCHED,
            "session_proof": native_tui_launch.SESSION_PROOF_ARGV,
            "pane_observation": {"pane_id": pane_id},
        }

    monkeypatch.setattr(native_tui_launch, "start", _published_then_blocked)
    execution = asyncio.create_task(
        _execute(request, FakeTransport(workdir=launch_root["workdir"]))
    )
    assert await asyncio.to_thread(entered.wait, 5)

    execution.cancel()
    await asyncio.sleep(0)
    assert not execution.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await execution

    reserved = oj.get_operation(request.operation_id)
    assert cleanup_calls[-1]["successor_terminal_id"] == reserved["successor_terminal_id"]
    assert cleanup_calls[-1]["successor_generation"] == reserved["successor_generation"]
    result = oj.get_result(request.operation_id)
    assert result["result_state"] == oj.RESULT_RECONCILIATION_REQUIRED
    assert len(result["result_evidence"]["stop_reap_error_digest"]) == 64
    assert "stop_reap_digest" not in result["result_evidence"]


def _barrier_between_intents(monkeypatch, after_step: str):
    """Claim the session barrier from inside the executor's own authorize
    callback, immediately after ``after_step`` commits — the exact window in
    which Stop lands between two ordered intents."""
    original = oj.authorize_effect_intent
    state = {"armed": True}

    def _wrapped(operation_id, **kwargs):
        result = original(operation_id, **kwargs)
        if state["armed"] and kwargs.get("effect_step") == after_step:
            state["armed"] = False
            oj.claim_session_barrier(
                "cao-campaign-a", claimed_by="stop", reason="stop won the race"
            )
        return result

    monkeypatch.setattr(oj, "authorize_effect_intent", _wrapped)
    return state


def test_barrier_between_create_pane_and_launch_resume_creates_no_pane(launch_root, monkeypatch):
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    _barrier_between_intents(monkeypatch, oj.EFFECT_STEP_CREATE_PANE)
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(xe.ExactExecutorError):
        _run(_execute(request, transport))
    assert transport.created == 0
    steps = [i["effect_step"] for i in oj.list_effect_intents(request.operation_id)]
    assert oj.EFFECT_STEP_CREATE_PANE in steps
    assert oj.EFFECT_STEP_LAUNCH_RESUME not in steps


def test_barrier_first_at_every_callback_begins_no_effect(launch_root, monkeypatch):
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    # Claim the barrier after the operation claim but before the first effect.
    original_claim = oj.claim_operation

    def _claim_then_barrier(req, **kwargs):
        result = original_claim(req, **kwargs)
        oj.claim_session_barrier(req.session_name, claimed_by="stop", reason="stop won the race")
        return result

    monkeypatch.setattr(oj, "claim_operation", _claim_then_barrier)
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(xe.ExactExecutorError):
        _run(_execute(request, transport))
    assert oj.list_effect_intents(request.operation_id) == []
    assert transport.created == 0
    assert xe.get_result(request.operation_id)["result_state"] == xe.OUTCOME_REFUSED


def test_intent_first_preserves_the_inflight_intent(launch_root, monkeypatch):
    """When the intent commits just before the barrier, the in-flight truth
    is preserved for M3-C: the intent stays recorded, no later effect
    begins, and the outcome is durably bounded."""
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    _barrier_between_intents(monkeypatch, oj.EFFECT_STEP_FENCE_PRIOR)
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(xe.ExactExecutorError):
        _run(_execute(request, transport))
    steps = [i["effect_step"] for i in oj.list_effect_intents(request.operation_id)]
    assert steps == [oj.EFFECT_STEP_FENCE_PRIOR]
    assert transport.created == 0


@pytest.mark.parametrize(
    "armed_step",
    [
        oj.EFFECT_STEP_FENCE_PRIOR,
        oj.EFFECT_STEP_REAP_PRIOR,
        oj.EFFECT_STEP_RELEASE_ATTACHMENT,
        oj.EFFECT_STEP_ACQUIRE_NATIVE,
        oj.EFFECT_STEP_CREATE_PANE,
        oj.EFFECT_STEP_LAUNCH_RESUME,
        oj.EFFECT_STEP_VERIFY_IDENTITY,
    ],
)
def test_barrier_after_each_intent_resolves_without_a_later_effect(
    launch_root, monkeypatch, armed_step
):
    """Barrier-first at every effect callback: the intents up to and
    including the armed step stay recorded, no later effect begins, and the
    outcome is truthful for the position — refused while the successor pane
    cannot exist, reconciliation-required once it does (never bound, never
    admitted)."""
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)
    reaper = _reap_noop(monkeypatch)
    _barrier_between_intents(monkeypatch, armed_step)
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(xe.ExactExecutorError) as excinfo:
        _run(_execute(request, transport))

    order = list(oj._EFFECT_STEP_ORDER)
    recorded = [i["effect_step"] for i in oj.list_effect_intents(request.operation_id)]
    assert recorded == order[1 : order.index(armed_step) + 1]

    pane_may_exist = order.index(armed_step) >= order.index(oj.EFFECT_STEP_LAUNCH_RESUME)
    assert transport.created == (1 if pane_may_exist else 0)
    result = xe.get_result(request.operation_id)
    if pane_may_exist:
        assert isinstance(excinfo.value, xe.ExactExecutorReconciliation)
        assert result["result_state"] == xe.OUTCOME_RECONCILIATION_REQUIRED
        reserved = oj.get_operation(request.operation_id)
        assert reaper.calls[-1]["terminal_id"] == reserved["successor_terminal_id"]
        assert reaper.calls[-1]["expected_generation"] == reserved["successor_generation"]
        # The successor pane may exist but is never bound and never admitted.
        agent = roster.get_agent(bind["agent"]["agent_id"])
        assert (
            agent["current_incarnation"]["incarnation_id"] == bind["incarnation"]["incarnation_id"]
        )
        assert len(roster.list_incarnations(agent_id=agent["agent_id"])) == 1
    else:
        assert isinstance(excinfo.value, xe.ExactExecutorRefused)
        assert result["result_state"] == xe.OUTCOME_REFUSED


def test_barrier_before_final_bind_prevents_the_bind(launch_root, monkeypatch):
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)
    reaper = _reap_noop(monkeypatch)
    # The launch completes; Stop lands between verification and the bind.
    _barrier_between_intents(monkeypatch, oj.EFFECT_STEP_VERIFY_IDENTITY)
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(xe.ExactExecutorReconciliation):
        _run(_execute(request, transport))
    agent = roster.get_agent(bind["agent"]["agent_id"])
    assert agent["current_incarnation"]["incarnation_id"] == bind["incarnation"]["incarnation_id"]
    assert len(roster.list_incarnations(agent_id=agent["agent_id"])) == 1
    assert xe.get_result(request.operation_id)["result_state"] == (
        xe.OUTCOME_RECONCILIATION_REQUIRED
    )
    reserved = oj.get_operation(request.operation_id)
    assert [call["terminal_id"] for call in reaper.calls] == [
        request.prior_terminal_id,
        reserved["successor_terminal_id"],
    ]
    assert reaper.calls[-1]["expected_generation"] == reserved["successor_generation"]
    # The successor pane exists but was never bound and never admitted.
    attachment = na.get("claude_code", _NATIVE_ID)
    assert attachment["owner"]["terminal_id"] != bind["incarnation"]["terminal_id"]


def test_stopped_session_refuses_before_claim(launch_root, monkeypatch):
    bind, contract = _dormant_worker(launch_root)
    sl.declare("cao-campaign-a", sl.WORKING, declared_by="operator")
    sl.stop("cao-campaign-a", declared_by="operator")
    request = _operation_request(bind, contract, lifecycle_observation=sl.WORKING)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(oj.OperationJournalError):
        _run(_execute(request, transport))
    assert transport.created == 0


# ---------------------------------------------------------------------------
# 9. final success: bound (not admitted), roster bumped once, zero input
# ---------------------------------------------------------------------------


def test_success_binds_bound_not_admitted_and_bumps_once(launch_root, monkeypatch, input_spies):
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    revision_at_claim = roster.get_agent(bind["agent"]["agent_id"])["revision"]
    transport = FakeTransport(workdir=launch_root["workdir"])
    result = _run(_execute(request, transport))

    agent = roster.get_agent(bind["agent"]["agent_id"])
    assert agent["revision"] == revision_at_claim + 1
    successor = agent["current_incarnation"]
    assert successor["disposition"] == roster.INCARNATION_BOUND
    assert successor["disposition"] != roster.INCARNATION_ADMITTED
    assert result["admitted"] is False
    assert result["native_session_id"] == _NATIVE_ID
    # No task/input/conductor/supervisor surface was touched.
    assert input_spies.touched == []


# ---------------------------------------------------------------------------
# 12. launch material bounding
# ---------------------------------------------------------------------------


def test_launch_material_is_bounded_and_validated(launch_root, monkeypatch):
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    with pytest.raises(xe.ExactExecutorInvalid):
        _run(
            _execute(
                request,
                transport,
                material=xe.LaunchMaterial(environment={"BAD NAME": "x"}),
            )
        )
    with pytest.raises(xe.ExactExecutorInvalid):
        _run(
            _execute(
                request,
                transport,
                material=xe.LaunchMaterial(extra_args=[f"--opt-{i}" for i in range(200)]),
            )
        )
    with pytest.raises(xe.ExactExecutorInvalid):
        _run(_execute(request, transport, material=xe.LaunchMaterial(extra_args="--flag")))
    assert transport.created == 0


def test_composed_profile_args_have_a_larger_bounded_lane_than_extra_args():
    """Generated Codex profile text is launch material, not a task message.

    It can exceed the compact generic argv cap, while unrelated extra args
    retain that smaller bound and the profile lane retains its own hard cap.
    """
    composed = "developer_instructions=" + ("x" * 15000)
    validated = xe._validate_launch_material(xe.LaunchMaterial(profile_args=[composed]))
    assert validated.profile_args == (composed,)

    with pytest.raises(xe.ExactExecutorInvalid, match="extra_args entries"):
        xe._validate_launch_material(xe.LaunchMaterial(extra_args=[composed]))
    with pytest.raises(xe.ExactExecutorInvalid, match="profile_args entries"):
        xe._validate_launch_material(
            xe.LaunchMaterial(profile_args=["x" * (xe.MAX_PROFILE_ARG_LEN + 1)])
        )


def test_secret_bearing_carrier_requires_the_exact_cell(launch_root, monkeypatch):
    """Ephemeral carrier values cannot be compared with B1 because they are
    intentionally not persisted, so the harness-named canary cell is their
    bounded authorization instead of ambient caller material being trusted.
    """
    bind, contract = _dormant_worker(launch_root)
    request = _operation_request(
        bind, contract, compatibility_cell_ref=None, compatibility_cell_digest=None
    )
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])

    with pytest.raises(xe.ExactExecutorRefused) as excinfo:
        _run(
            _execute(
                request,
                transport,
                material=xe.LaunchMaterial(environment={"PROVIDER_TOKEN": "carrier"}),
            )
        )

    assert "compatibility" in str(excinfo.value).lower()
    assert transport.created == 0
    assert oj.list_effect_intents(request.operation_id) == []


def test_environment_values_are_never_stored(launch_root, monkeypatch):
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    carrier_path = "/tmp/b3-profile-carrier.md"
    result = _run(
        _execute(
            request,
            transport,
            material=xe.LaunchMaterial(environment={"PROFILE_FILE": carrier_path}),
        )
    )
    assert result["outcome"] == xe.OUTCOME_ACCEPTED
    stored = oj.get_operation(request.operation_id)
    blob = stored["request_json"] + (stored.get("result_evidence_json") or "")
    assert carrier_path not in blob
    assert "PROFILE_FILE" in (stored.get("result_evidence_json") or "")  # names only


def test_launch_effect_binds_a_value_free_material_digest(launch_root, monkeypatch):
    """Response-loss adoption must identify the exact sealed argv/env.

    Persist only a digest: never the carrier value itself.  A later retry with
    different material then conflicts at the already-authorized effect instead
    of starting a changed process for the same operation.
    """
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    secret_carrier = "provider-secret-not-for-the-journal"

    result = _run(
        _execute(
            request,
            transport,
            material=xe.LaunchMaterial(environment={"PROVIDER_TOKEN": secret_carrier}),
        )
    )

    assert result["outcome"] == xe.OUTCOME_ACCEPTED
    intents = oj.list_effect_intents(request.operation_id)
    launch = next(row for row in intents if row["effect_step"] == oj.EFFECT_STEP_LAUNCH_RESUME)
    digest = launch["effect_payload"]["effect_payload"]["launch_material_digest"]
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")
    assert secret_carrier not in json.dumps(intents, sort_keys=True)


def test_long_environment_key_set_keeps_result_evidence_bounded(launch_root, monkeypatch):
    """The accepted LaunchMaterial bounds permit more environment key-name
    bytes than one journal evidence value.  Summarize that rare case by digest
    before physical effects instead of failing the final bind after a pane exists.
    """
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    environment = {f"PROVIDER_CARRIER_{index:02d}_{'X' * 20}": "v" for index in range(32)}

    result = _run(_execute(request, transport, material=xe.LaunchMaterial(environment=environment)))

    assert result["outcome"] == xe.OUTCOME_ACCEPTED
    evidence = oj.get_operation(request.operation_id)["result_evidence"]
    assert "environment_keys" not in evidence
    assert len(evidence["environment_keys_digest"]) == 64


def test_overlong_refusal_detail_is_bounded_not_masked():
    """An over-long exception message is truncated to the journal's detail
    bound, so the durable refusal record is written instead of being masked
    by a validation error."""
    detail = "x" * (oj.MAX_RESULT_DETAIL_LEN * 2)
    bounded = xe._Execution._bounded_detail(detail)
    assert len(bounded) == oj.MAX_RESULT_DETAIL_LEN
    assert bounded.endswith("...(truncated)")
    assert xe._Execution._bounded_detail("short") == "short"


# ---------------------------------------------------------------------------
# repeated real-file SQLite race tests
# ---------------------------------------------------------------------------


def test_concurrent_reservations_allocate_one_successor(file_db, launch_root, monkeypatch):
    """Two threads racing different successor candidates for the SAME
    operation through a real SQLite file converge on one reservation."""
    bind, contract = _dormant_worker(launch_root)
    request = _operation_request(bind, contract)
    assert oj.claim_operation(request)["adopted"] is False

    results: list = []
    conflicts: list = []
    others: list = []

    def run() -> None:
        candidate = f"{uuid.uuid4().hex[:8]}"
        try:
            results.append(oj.reserve_successor(request.operation_id, candidate, str(uuid.uuid4())))
        except oj.OperationJournalError as exc:
            conflicts.append(exc)
        except BaseException as exc:  # noqa: BLE001 - surface for assertions
            others.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert others == [], f"unexpected reservation errors: {others}"
    assert len(results) + len(conflicts) == 2
    stored = oj.get_operation(request.operation_id)
    assert stored["successor_terminal_id"] is not None
    assert stored["result_state"] == xe.OUTCOME_PENDING
    for reserved in results:
        assert reserved["operation"]["successor_terminal_id"] == stored["successor_terminal_id"]


def test_reservation_race_adopts_the_durable_successor(launch_root, monkeypatch):
    """A cross-process duplicate that loses the reservation CAS adopts the
    winner's durable successor — never an untyped conflict, never a second
    successor id burned into the unique index."""
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    competitor_generation = str(uuid.uuid4())
    real_reserve = oj.reserve_successor
    state = {"raced": False}

    def _racing_reserve(operation_id, successor_terminal_id, successor_generation, **kwargs):
        if not state["raced"]:
            state["raced"] = True
            # The competitor commits its reservation first; our candidate
            # loses and must be abandoned, never recorded.
            real_reserve(operation_id, "eeee4444", competitor_generation)
            raise oj.OperationJournalConflict("lost the reservation CAS to a concurrent run")
        return real_reserve(operation_id, successor_terminal_id, successor_generation, **kwargs)

    monkeypatch.setattr(oj, "reserve_successor", _racing_reserve)
    transport = FakeTransport(workdir=launch_root["workdir"])
    result = _run(_execute(request, transport))
    assert result["outcome"] == xe.OUTCOME_ACCEPTED
    assert result["successor_terminal_id"] == "eeee4444"
    assert result["successor_generation"] == competitor_generation
    stored = oj.get_operation(request.operation_id)
    assert stored["successor_terminal_id"] == "eeee4444"
    assert stored["successor_generation"] == competitor_generation


def test_concurrent_executor_runs_produce_one_successor_and_one_pane(
    file_db, launch_root, monkeypatch
):
    """Two concurrent executor runs for the same operation (one event loop,
    one real SQLite file): exactly one performs the physical sequence — one
    pane, one bind, one successor — and both converge on the durable
    accepted outcome."""
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])

    async def _race() -> list:
        return await asyncio.gather(
            _execute(request, transport),
            _execute(request, transport),
            return_exceptions=True,
        )

    outcomes = _run(_race())
    accepted = [o for o in outcomes if isinstance(o, dict)]
    errors = [o for o in outcomes if isinstance(o, BaseException)]
    assert errors == [], f"unexpected concurrent-run errors: {errors}"
    assert len(accepted) == 2
    assert all(o["outcome"] == xe.OUTCOME_ACCEPTED for o in accepted)
    ids = {(o["successor_terminal_id"], o["successor_generation"]) for o in accepted}
    assert len(ids) == 1
    assert transport.created == 1
    assert len(roster.list_incarnations(agent_id=bind["agent"]["agent_id"])) == 2
    stored = oj.get_operation(request.operation_id)
    assert stored["result_state"] == xe.OUTCOME_ACCEPTED


@pytest.mark.parametrize("_race_iteration", range(3))
def test_cross_process_native_launch_race_creates_only_one_pane(
    file_db, launch_root, monkeypatch, _race_iteration
):
    """Two executor processes can reach the same native launch after adopting
    the same durable effect intent.  The attachment's DECLARED -> STARTING CAS
    must choose the sole pane creator; a same-owner loser reconciles that pane
    instead of starting a second provider process or freezing the winner."""
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    real_mark_starting = na.mark_starting
    both_declared = threading.Barrier(2)

    def _race_mark_starting(**kwargs):
        both_declared.wait(timeout=10)
        return real_mark_starting(**kwargs)

    monkeypatch.setattr(na, "mark_starting", _race_mark_starting)
    # Distinct OS processes have distinct in-memory operation locks.  Give
    # each call its own lock while keeping the shared real SQLite file.
    monkeypatch.setattr(xe, "_operation_lock", lambda _operation_id: asyncio.Lock())
    results: list[dict] = []
    errors: list[BaseException] = []

    def _launch() -> None:
        try:
            results.append(_run(_execute(request, transport)))
        except BaseException as exc:  # noqa: BLE001 - surface both race outcomes
            errors.append(exc)

    threads = [threading.Thread(target=_launch) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == [], f"concurrent exact launch errors: {errors}"
    assert len(results) == 2
    assert all(result["outcome"] == xe.OUTCOME_ACCEPTED for result in results)
    assert transport.created == 1
    attachment = na.get("claude_code", _NATIVE_ID)
    assert attachment["state"] == na.ATTACHED
    successor_ids = {
        (result["successor_terminal_id"], result["successor_generation"]) for result in results
    }
    assert len(successor_ids) == 1
    successor_terminal_id, successor_generation = successor_ids.pop()
    assert attachment["owner"]["terminal_id"] == successor_terminal_id
    assert attachment["owner"]["generation"] == successor_generation
    assert oj.get_result(request.operation_id)["result_state"] == xe.OUTCOME_ACCEPTED
    assert len(roster.list_incarnations(agent_id=bind["agent"]["agent_id"])) == 2


@pytest.mark.parametrize("arrival", ["before-pane", "before-publish"])
def test_duplicate_arriving_after_starting_waits_for_exact_owner(
    file_db, launch_root, monkeypatch, arrival
):
    """A later duplicate must not observe/publish over a healthy STARTING owner.

    The two cases hold the winner immediately after mark_starting and on its
    first post-create observation.  The duplicate reaches B3 after that state
    is durable; it must wait for this exact owner rather than taking the launch
    seam's genuine-crash reconciliation branch and freezing the winner.
    """
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)

    class GatedTransport(FakeTransport):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.winner_parked = threading.Event()
            self.release_winner = threading.Event()
            self._gate_lock = threading.Lock()
            self._create_gate_claimed = False
            self._observe_gate_claimed = False

        def create_pane(self, *, argv):
            if arrival == "before-pane":
                with self._gate_lock:
                    park = not self._create_gate_claimed
                    self._create_gate_claimed = True
                if park:
                    self.winner_parked.set()
                    assert self.release_winner.wait(timeout=10)
            return super().create_pane(argv=argv)

        def observe(self):
            if arrival == "before-publish":
                with self._gate_lock:
                    park = not self._observe_gate_claimed
                    self._observe_gate_claimed = True
                if park:
                    self.winner_parked.set()
                    assert self.release_winner.wait(timeout=10)
            return super().observe()

    transport = GatedTransport(workdir=launch_root["workdir"])
    monkeypatch.setattr(xe, "_operation_lock", lambda _operation_id: asyncio.Lock())
    duplicate_saw_starting = threading.Event()
    real_get = na.get

    def _signal_duplicate_preflight(provider, native_session_id):
        attachment = real_get(provider, native_session_id)
        if (
            threading.current_thread().name == "duplicate-executor"
            and attachment is not None
            and attachment["state"] == na.STARTING
        ):
            duplicate_saw_starting.set()
        return attachment

    monkeypatch.setattr(na, "get", _signal_duplicate_preflight)
    results: list[dict] = []
    errors: list[BaseException] = []

    def _launch() -> None:
        try:
            results.append(_run(_execute(request, transport)))
        except BaseException as exc:  # noqa: BLE001 - surface both race outcomes
            errors.append(exc)

    winner = threading.Thread(target=_launch, name="winner-executor")
    duplicate = threading.Thread(target=_launch, name="duplicate-executor")
    winner.start()
    assert transport.winner_parked.wait(timeout=10)
    duplicate.start()
    saw_starting = duplicate_saw_starting.wait(timeout=2)
    transport.release_winner.set()
    winner.join(timeout=15)
    duplicate.join(timeout=15)

    assert saw_starting, "the duplicate never preflighted the durable STARTING owner"
    assert not winner.is_alive()
    assert not duplicate.is_alive()
    assert errors == [], f"concurrent exact launch errors: {errors}"
    assert len(results) == 2
    assert all(result["outcome"] == xe.OUTCOME_ACCEPTED for result in results)
    assert transport.created == 1
    attachment = real_get("claude_code", _NATIVE_ID)
    assert attachment["state"] == na.ATTACHED
    assert oj.get_result(request.operation_id)["result_state"] == xe.OUTCOME_ACCEPTED
    assert len(roster.list_incarnations(agent_id=bind["agent"]["agent_id"])) == 2


@pytest.mark.parametrize(
    "error_type",
    [roster.StableAgentUnavailable, oj.OperationJournalUnavailable],
)
def test_typed_bind_contention_adopts_a_durable_accepted_result(
    file_db, launch_root, monkeypatch, error_type
):
    """A translated SQLite race cannot escape after the bind actually won."""
    bind, contract = _dormant_worker(launch_root)
    _prior_attachment()
    request = _operation_request(bind, contract)
    _reap_noop(monkeypatch)
    transport = FakeTransport(workdir=launch_root["workdir"])
    real_bind = xe._bind_successor
    state = {"raised": False}

    def _commit_then_report_contention(*args, **kwargs):
        result = real_bind(*args, **kwargs)
        if not state["raised"]:
            state["raised"] = True
            raise error_type("lost the final-bind SQLite race")
        return result

    monkeypatch.setattr(xe, "_bind_successor", _commit_then_report_contention)
    result = _run(_execute(request, transport))

    assert state["raised"] is True
    assert result["outcome"] == xe.OUTCOME_ACCEPTED
    assert oj.get_result(request.operation_id)["result_state"] == xe.OUTCOME_ACCEPTED
    assert len(roster.list_incarnations(agent_id=bind["agent"]["agent_id"])) == 2


# ---------------------------------------------------------------------------
# 11. no callback leaves the launch seam unchanged
# ---------------------------------------------------------------------------


def test_launch_seam_without_callback_behaves_unchanged(launch_root):
    """Supplying no authorize callback is the historical contract: the launch
    runs the same declare/start/publish sequence and succeeds."""
    fresh_native_id = _OTHER_NATIVE_ID
    transport = FakeTransport(workdir=launch_root["workdir"], native_session_id=fresh_native_id)
    result = native_tui_launch.start(
        provider="claude_code",
        native_session_id=fresh_native_id,
        terminal_id="c3d4e5f6",
        generation=str(uuid.uuid4()),
        execution_mode="native_tui",
        intent=na.acquire_intent(
            acquisition_method=na.ACQUISITION_RESUME,
            acquisition_receipt={"kind": "resume", "session_id": fresh_native_id},
            admits_only_new_instructions=True,
            replays_task_bytes=False,
        ),
        binary=launch_root["binary"],
        binary_sha256=launch_root["binary_sha256"],
        working_directory=launch_root["workdir"],
        transport=transport,
        launch_kind="resume",
    )
    assert result["outcome"] == native_tui_launch.OUTCOME_LAUNCHED
    assert transport.created == 1
    assert na.get("claude_code", fresh_native_id)["state"] == na.ATTACHED


def test_launch_seam_supports_the_authorize_callback(launch_root):
    """The optional callback is invoked at the three internal boundaries —
    attachment declaration, pane creation, and identity publication — and a
    refusal at any of them stops the launch before that boundary."""
    fresh_native_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    transport = FakeTransport(workdir=launch_root["workdir"], native_session_id=fresh_native_id)
    boundaries: list = []
    state = {"refuse_once": True}

    def _authorize(boundary: str) -> None:
        boundaries.append(boundary)
        if boundary == "create_pane" and state["refuse_once"]:
            state["refuse_once"] = False
            raise RuntimeError("barrier claimed before the pane")

    generation = str(uuid.uuid4())
    with pytest.raises(RuntimeError):
        native_tui_launch.start(
            provider="claude_code",
            native_session_id=fresh_native_id,
            terminal_id="d4e5f6a7",
            generation=generation,
            execution_mode="native_tui",
            intent=na.acquire_intent(
                acquisition_method=na.ACQUISITION_RESUME,
                acquisition_receipt={"kind": "resume", "session_id": fresh_native_id},
                admits_only_new_instructions=True,
                replays_task_bytes=False,
            ),
            binary=launch_root["binary"],
            binary_sha256=launch_root["binary_sha256"],
            working_directory=launch_root["workdir"],
            transport=transport,
            launch_kind="resume",
            authorize=_authorize,
        )
    assert boundaries == ["declare", "create_pane"]
    assert transport.created == 0
    attachment = na.get("claude_code", fresh_native_id)
    assert attachment["state"] == na.DECLARED  # nothing was started

    # A full run crosses all three boundaries in order, adopting the same
    # declared owner (same terminal AND generation) the refusal left behind.
    boundaries.clear()
    transport2 = FakeTransport(workdir=launch_root["workdir"], native_session_id=fresh_native_id)
    result = native_tui_launch.start(
        provider="claude_code",
        native_session_id=fresh_native_id,
        terminal_id="d4e5f6a7",
        generation=attachment["owner"]["generation"],
        execution_mode="native_tui",
        intent=na.acquire_intent(
            acquisition_method=na.ACQUISITION_RESUME,
            acquisition_receipt={"kind": "resume", "session_id": fresh_native_id},
            admits_only_new_instructions=True,
            replays_task_bytes=False,
        ),
        binary=launch_root["binary"],
        binary_sha256=launch_root["binary_sha256"],
        working_directory=launch_root["workdir"],
        transport=transport2,
        launch_kind="resume",
        authorize=_authorize,
    )
    assert boundaries == ["declare", "create_pane", "publish"]
    assert result["outcome"] == native_tui_launch.OUTCOME_LAUNCHED
