"""M3-B4 installed, isolated, dark exact-provider canaries.

The source generation is a real managed-v2 native TUI launched and bound
without admission.  A fresh subprocess then publishes the B1 contract,
transitions that exact incarnation dormant, and drives the merged B3 executor
against the installed provider.  No task or prompt bytes are sent.

Run the Codex cell with::

    CAO_M3B4_EVIDENCE_DIR=/absolute/evidence/root \
      uv run pytest -m e2e test/e2e/test_exact_executor_canary.py -v

M3-F owns reading these receipts into a compatibility matrix and activation;
this module writes evidence only.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from test.e2e.exact_canary import matrix
from test.e2e.exact_canary.evidence import EvidenceSanitizer
from test.e2e.exact_canary.readiness import codex_composer_ready
from test.e2e.exact_canary.receipt import RECEIPT_SCHEMA, validate_receipt, write_receipt
from test.fixtures.cao_server import _pick_free_port, _start_cao_server
from test.fixtures.tmux_server import (
    assert_shared_server_untouched,
    isolated_tmux_server,
    shared_server_sentinel,
)
from typing import Any

import pytest
import requests

from cli_agent_orchestrator.providers import claude_code as claude_provider
from cli_agent_orchestrator.providers import codex as codex_provider
from cli_agent_orchestrator.providers import muse_cli as muse_provider
from cli_agent_orchestrator.services import (
    managed_launch_v2,
    managed_provider_bridge,
    muse_native_launch,
    provider_contracts,
)
from cli_agent_orchestrator.utils.agent_profiles import parse_agent_profile_text

pytestmark = pytest.mark.e2e

PROTOCOL_V2 = "cao-managed-launch-v2"
V2_ROOT = "/managed-launch/v2/reservations"
CODEX_PIN = "0.147.0"
CODEX_MODEL = "gpt-5.6-terra"
CODEX_EFFORT = "xhigh"
CLAUDE_PIN = "2.1.232"
CLAUDE_MODEL = "claude-opus-5"
CLAUDE_EFFORT = "high"
KIMI_PIN = "0.34.0"
KIMI_MODEL = "kimi-code/k3"
KIMI_EFFORT = "max"
MUSE_PIN = "0.1.0"
MUSE_MODEL = "muse-spark-1.2"
MUSE_EFFORT = "high"
AGENT_PROFILE = "m3b4-canary"
EVIDENCE_ENV = "CAO_M3B4_EVIDENCE_DIR"
_PROFILE = """---
name: m3b4-canary
description: Disposable zero-task exact-restore canary profile
---

This is a zero-task provider canary. Never use tools.
"""


@dataclass(frozen=True)
class InstalledCell:
    key: str
    provider: str
    binary: str
    pin: str
    model: str
    effort: str
    expected_session_proof: str
    route: str


CODEX_CELL = InstalledCell(
    key="codex",
    provider="codex",
    binary="codex",
    pin=CODEX_PIN,
    model=CODEX_MODEL,
    effort=CODEX_EFFORT,
    expected_session_proof="argv",
    route="openai",
)
CLAUDE_CELL = InstalledCell(
    key="claude",
    provider="claude_code",
    binary="claude",
    pin=CLAUDE_PIN,
    model=CLAUDE_MODEL,
    effort=CLAUDE_EFFORT,
    expected_session_proof="argv",
    route="anthropic",
)
KIMI_CELL = InstalledCell(
    key="kimi",
    provider="kimi_cli",
    binary="kimi",
    pin=KIMI_PIN,
    model=KIMI_MODEL,
    effort=KIMI_EFFORT,
    expected_session_proof="kimi-native-header-v1",
    route="moonshot",
)
MUSE_CELL = InstalledCell(
    key="muse",
    provider="muse_cli",
    binary="muse",
    pin=MUSE_PIN,
    model=MUSE_MODEL,
    effort=MUSE_EFFORT,
    expected_session_proof="argv",
    route="meta",
)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _installed_cell(cell: InstalledCell) -> tuple[str, str, str, str]:
    binary = shutil.which(cell.binary)
    if not binary:
        pytest.skip(f"{cell.binary} CLI is not installed")
    assert binary is not None
    executable = os.path.realpath(binary)
    probe = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, timeout=20, check=False
    )
    banner = (probe.stdout or probe.stderr or "").strip()
    normalized = str(provider_contracts.normalized_version(banner))
    assessment = matrix.assess_cell(
        cell.provider,
        normalized_version=normalized,
        executable_path=executable,
        executable_sha256=_sha256_file(Path(executable)),
        version_banner=banner,
    )
    if not assessment.runnable or normalized != cell.pin:
        pytest.skip(
            f"installed {cell.provider} {banner!r} is not the exact B4 cell {cell.pin}: "
            f"{assessment.reason}"
        )
    return executable, _sha256_file(Path(executable)), banner, normalized


def _build_codex_home(real_home: Path, scratch: Path) -> str:
    """Share auth/session history while insulating the operator config file."""
    source = real_home / ".codex"
    destination = scratch / "codex-home"
    destination.mkdir(parents=True)
    if not source.is_dir():
        pytest.skip(f"Codex provider home is absent: {source}")
    for entry in source.iterdir():
        target = destination / entry.name
        if entry.name == "config.toml":
            if entry.is_file():
                shutil.copy2(entry, target)
            continue
        try:
            target.symlink_to(entry, target_is_directory=entry.is_dir())
        except OSError:
            continue
    return os.path.realpath(destination)


def _build_claude_home(real_home: Path, scratch: Path) -> str:
    """Share auth/session history while insulating mutable Claude settings."""
    source = real_home / ".claude"
    destination = scratch / "claude-home"
    destination.mkdir(parents=True)
    if not source.is_dir():
        pytest.skip(f"Claude provider home is absent: {source}")
    for entry in source.iterdir():
        target = destination / entry.name
        if entry.name == "settings.json":
            if entry.is_file():
                shutil.copy2(entry, target)
            continue
        try:
            target.symlink_to(entry, target_is_directory=entry.is_dir())
        except OSError:
            continue
    return os.path.realpath(destination)


def _build_kimi_home(real_home: Path, scratch: Path) -> str:
    """Share Kimi auth/sessions while insulating mutable operator config."""
    source = real_home / ".kimi-code"
    destination = scratch / "kimi-provider-home"
    destination.mkdir(parents=True)
    if not source.is_dir():
        pytest.skip(f"Kimi provider home is absent: {source}")
    for entry in source.iterdir():
        if entry.name in {"config.toml", "mcp.json", "workspace-trust"}:
            continue
        target = destination / entry.name
        try:
            target.symlink_to(entry, target_is_directory=entry.is_dir())
        except OSError:
            continue
    config = source / "config.toml"
    if config.is_file():
        shutil.copy2(config, destination / "config.toml")
    return os.path.realpath(destination)


def _git_worktree(path: Path) -> str:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "m3b4@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "m3b4-canary"], cwd=path, check=True)
    (path / "README.txt").write_text("M3-B4 zero-task exact-restore canary\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "initialize canary"], cwd=path, check=True)
    return os.path.realpath(path)


def _launch_source(
    *,
    cell: InstalledCell,
    server_url: str,
    session_name: str,
    workdir: str,
    executable: str,
    executable_sha256: str,
) -> dict[str, Any]:
    payload = {
        "protocol_version": PROTOCOL_V2,
        "reservation_id": str(uuid.uuid4()),
        "session_name": session_name,
        "provider": cell.provider,
        "agent_profile": AGENT_PROFILE,
        "caller_id": uuid.uuid4().hex[:8],
        "working_directory": workdir,
        "expected_model": cell.model,
        "expected_effort": cell.effort,
        "provider_executable": executable,
        "provider_executable_sha256": executable_sha256,
        "obligation_generation": f"m3b4-{uuid.uuid4().hex[:12]}",
        "task_id": f"cond-0378-m3b4-{cell.key}",
        "run_id": f"m3b4-{uuid.uuid4().hex[:12]}",
        "delivery_id": str(uuid.uuid4()),
        "launch_nonce": uuid.uuid4().hex + uuid.uuid4().hex,
        "execution_mode": "native_tui",
    }
    if cell.provider == "codex":
        payload["trusted_project_root"] = workdir
    reserved = requests.post(f"{server_url}{V2_ROOT}", json=payload, timeout=30)
    assert reserved.status_code == 201, f"reserve failed: {reserved.status_code} {reserved.text}"
    launched = requests.post(
        f"{server_url}{V2_ROOT}/{payload['reservation_id']}/launch", timeout=420
    )
    record = requests.get(f"{server_url}{V2_ROOT}/{payload['reservation_id']}", timeout=30).json()
    assert launched.status_code == 200, (
        f"launch failed: {launched.status_code} {launched.text}; "
        f"state={record.get('state')} preflight={record.get('preflight_failure')}"
    )
    # Codex 0.147.0 is deliberately in the narrow bootstrap/resume gate but
    # not in the older broad native-readiness table. B4 must not widen that
    # production table merely to manufacture its source generation. The
    # runner binds this observed zero-turn source through the public roster
    # seam; M3-F remains the publication and activation authority.
    assert record["durable_state"] == "launching", (
        f"source launch did not reach the zero-turn binding seam: "
        f"{record.get('preflight_failure')}"
    )
    assert record["native_session_id"]
    assert record["pane_id"]
    assert record["pane_pid"]
    assert record["stable_agent_id"]
    return {"reserve": payload, "launch": launched.json(), "record": record}


def _runner(
    command: str,
    *,
    environment: dict[str, str],
    scratch: Path,
    evidence: EvidenceSanitizer,
    arguments: list[str],
) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "test.e2e.exact_canary.runner", command, *arguments],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    evidence.write_text(scratch / f"runner-{command}.log", result.stdout + result.stderr)
    assert result.returncode == 0, (
        f"exact canary runner {command!r} failed with {result.returncode}; "
        f"see {scratch / f'runner-{command}.log'}"
    )


def _wait_for_exit(pid: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            pass
        time.sleep(0.1)
    raise AssertionError(f"source pane pid {pid} remained live after exact private-pane teardown")


def _wait_for_ready(
    cell: InstalledCell,
    tmux,
    pane_id: str,
    native_session_id: str,
    timeout: float = 90.0,
) -> str:
    """Require the resumed TUI to clear startup interstitials without input."""
    deadline = time.monotonic() + timeout
    transcript = ""
    while time.monotonic() < deadline:
        transcript = str(tmux.out("capture-pane", "-p", "-S-300", "-t", pane_id))
        trust_prompt = re.search(codex_provider.TRUST_PROMPT_PATTERN, transcript) or (
            re.search(codex_provider.TRUST_PROMPT_PATTERN_V2, transcript)
            and re.search(codex_provider.TRUST_PROMPT_FOOTER, transcript)
        )
        if trust_prompt:
            raise AssertionError(
                f"the exact {cell.provider} successor stopped at a trust interstitial"
            )
        if cell.provider == "claude_code" and re.search(
            claude_provider.TRUST_PROMPT_PATTERN, transcript
        ):
            raise AssertionError(
                "the exact claude_code successor stopped at its workspace trust interstitial"
            )
        if cell.provider == "muse_cli" and "Do you trust this workspace?" in transcript:
            raise AssertionError(
                "the exact muse_cli successor stopped at its workspace trust interstitial"
            )
        if cell.provider == "codex" and codex_composer_ready(transcript):
            return transcript
        if cell.provider == "claude_code" and re.search(
            claude_provider.IDLE_PROMPT_PATTERN, transcript
        ):
            return transcript
        if (
            cell.provider == "kimi_cli"
            and "Kimi Code" in transcript
            and native_session_id in transcript
        ):
            return transcript
        if cell.provider == "muse_cli" and re.search(
            muse_provider.IDLE_PROMPT_PATTERN, transcript, re.MULTILINE
        ):
            return transcript
        time.sleep(0.25)
    raise AssertionError(f"the exact {cell.provider} successor did not reach its ready TUI")


def _provider_home(
    cell: InstalledCell, real_home: Path, scratch: Path
) -> tuple[dict[str, str], str | None]:
    if cell.provider == "codex":
        home = _build_codex_home(real_home, scratch)
        return {"CODEX_HOME": home}, home
    if cell.provider == "claude_code":
        home = _build_claude_home(real_home, scratch)
        return {"CLAUDE_CONFIG_DIR": home}, home
    if cell.provider == "kimi_cli":
        home = _build_kimi_home(real_home, scratch)
        return {"KIMI_CODE_HOME": home}, None
    if cell.provider == "muse_cli":
        return {"MUSE_NO_AUTO_UPDATE": "1"}, None
    return {}, None


def _source_provider_home(
    cell: InstalledCell,
    *,
    state_root: Path,
    record: dict[str, Any],
    initial_home: str | None,
) -> str | None:
    if cell.provider == "kimi_cli":
        home = state_root / "companion" / record["terminal_id"] / record["generation"] / "kimi-home"
        assert home.is_dir(), "managed Kimi launch did not materialize its exact private home"
        return os.path.realpath(home)
    return initial_home


def _source_profile_and_material(
    cell: InstalledCell,
    *,
    state_root: Path,
    record: dict[str, Any],
) -> tuple[dict[str, str] | None, dict[str, Any], str | None, str | None]:
    profile = parse_agent_profile_text(_PROFILE, AGENT_PROFILE)
    composed = managed_provider_bridge._profile_material_from_profile(
        profile, record["terminal_id"]
    )
    profile_fact = {
        "profile_ref": AGENT_PROFILE,
        "profile_sha256": composed["profile_sha256"],
    }
    if cell.provider == "codex":
        core = codex_provider.compose_codex_core_args(
            codex_profile=profile.codexProfile,
            codex_config=profile.codexConfig,
            system_prompt=composed.get("system_prompt") or "",
            mcp_servers=composed.get("mcp_servers") or [],
            allowed_tools=composed.get("allowed_tools") or [],
            # Trust is contract-owned and inserted once by exact_executor.
            trusted_project_root=None,
        )
        choice_len = 2 if core and core[0] == "--profile" else 1
        profile_args = [
            *core[:choice_len],
            *managed_launch_v2.CODEX_TUI_FLAGS,
            *core[choice_len:],
        ]
        cell_ref = f"codex:{cell.model}:native_tui:{cell.pin}:composed-profile"
        profile_args_sha256 = _sha256_text(
            json.dumps(profile_args, ensure_ascii=False, separators=(",", ":"))
        )
        cell_digest = _sha256_text(
            f"{cell_ref}\n{record['request']['provider_executable_sha256']}\n"
            f"{composed['profile_sha256']}\n{profile_args_sha256}"
        )
        return profile_fact, {"profile_args": profile_args}, cell_ref, cell_digest
    if cell.provider == "kimi_cli":
        return (
            profile_fact,
            {"profile_args": managed_launch_v2._kimi_profile_launch_args(composed)},
            None,
            None,
        )
    if cell.provider == "claude_code":
        path = state_root / "companion" / record["terminal_id"] / record["generation"]
        path /= "profile-system-prompt.md"
        assert path.is_file(), "managed Claude launch did not materialize its exact profile"
        canonical = os.path.realpath(path)
        digest = _sha256_file(Path(canonical))
        cell_ref = f"claude_code:{cell.model}:native_tui:{cell.pin}"
        cell_digest = _sha256_text(
            f"{cell_ref}\n{record['request']['provider_executable_sha256']}\n{digest}"
        )
        return (
            {
                "profile_system_prompt_path": canonical,
                "profile_system_prompt_sha256": digest,
                **profile_fact,
            },
            {
                "profile_args": [
                    "--dangerously-skip-permissions",
                    "--append-system-prompt-file",
                    canonical,
                ]
            },
            cell_ref,
            cell_digest,
        )
    if cell.provider != "muse_cli":
        return None, {}, None, None
    path = state_root / "companion" / record["terminal_id"] / record["generation"]
    path /= muse_native_launch.PROFILE_SYSTEM_PROMPT_FILENAME
    assert path.is_file(), "managed Muse launch did not materialize its exact profile carrier"
    canonical = os.path.realpath(path)
    digest = _sha256_file(Path(canonical))
    carrier = muse_native_launch.installed_profile_carrier_capability()
    assert carrier.supported and carrier.inner_executable_sha256
    assert carrier.proof in (
        muse_native_launch.PROOF_PROBED,
        muse_native_launch.PROOF_PROBED_BY_OPERATOR,
        muse_native_launch.PROOF_UNPROVEN,
    )
    if carrier.cell is not None:
        cell_ref = f"muse_cli:{cell.model}:native_tui:{carrier.cell}"
        cell_digest = _sha256_text(
            f"{cell_ref}\n{carrier.full_banner}\n{carrier.inner_executable_sha256}"
        )
    else:
        cell_ref = None
        cell_digest = None
    return (
        {
            "profile_system_prompt_path": canonical,
            "profile_system_prompt_sha256": digest,
            **profile_fact,
        },
        {
            # These are the exact profile-derived flags used by the managed
            # source cell.  In particular, omitting --trust-workspace strands
            # a physically restored Muse session before its composer exists.
            "profile_args": ["--trust-workspace", "--yolo"],
            "profile_environment": {
                muse_native_launch.PROFILE_SYSTEM_PROMPT_ENV: canonical,
                "MUSE_NO_AUTO_UPDATE": "1",
            },
        },
        cell_ref,
        cell_digest,
    )


@pytest.mark.parametrize(
    "cell",
    [CLAUDE_CELL, CODEX_CELL, KIMI_CELL, MUSE_CELL],
    ids=lambda cell: cell.key,
)
def test_installed_exact_restore_is_bound_without_admission(tmp_path, cell: InstalledCell):
    executable, executable_sha256, banner, normalized = _installed_cell(cell)
    real_home_text = os.environ.get("HOME")
    if not real_home_text or not Path(real_home_text).is_dir():
        pytest.skip("the installed canary requires the operator provider-auth HOME")
    real_home = Path(real_home_text)
    state_root = Path(os.path.realpath(tmp_path / "state"))
    scratch = Path(os.path.realpath(tmp_path / "scratch"))
    state_root.mkdir()
    scratch.mkdir()
    profile_store = state_root / "agent-store"
    profile_store.mkdir()
    (profile_store / f"{AGENT_PROFILE}.md").write_text(_PROFILE, encoding="utf-8")
    provider_env, initial_provider_home = _provider_home(cell, real_home, scratch)
    workdir = _git_worktree(scratch / "worktree")
    evidence_root = Path(os.environ.get(EVIDENCE_ENV) or (scratch / "evidence"))

    with shared_server_sentinel("cao-m3b4-shared") as shared_canary:
        shared, shared_session, shared_identity = shared_canary
        with isolated_tmux_server("cao-m3b4-private-anchor") as tmux:
            assert tmux.owned_root is not None
            shim = tmux.write_shim(tmux.owned_root / "bin")
            session_name = f"cao-m3b4-{cell.key}-{uuid.uuid4().hex[:8]}"
            # Muse's status panel must render the canonical disposable worktree
            # without ellipsis or it truthfully cannot prove cwd equality.
            tmux.new_session(session_name, "-x", "240", "-y", "36", "--", "sh", "-c", "sleep 3600")
            sanitizer = EvidenceSanitizer(
                {
                    str(real_home): "<HOME>",
                    str(state_root): "<STATE_ROOT>",
                    str(scratch): "<SCRATCH>",
                    str(tmux.socket_path): "<TMUX_SOCKET>",
                    os.environ.get("USER", ""): "<USER>",
                }
            )
            child_env = tmux.subprocess_env(shim)
            child_env.update(
                {
                    "HOME": str(real_home),
                    "CAO_STATE_ROOT": str(state_root),
                    "CAO_A2A_DISABLED": "true",
                    "OTEL_SDK_DISABLED": "true",
                    "PYTHONUNBUFFERED": "1",
                    **provider_env,
                }
            )
            server = _start_cao_server(
                scratch / "server-home",
                _pick_free_port(),
                extra_env=child_env,
                deadline=30,
            )
            try:
                launched = _launch_source(
                    cell=cell,
                    server_url=server.url,
                    session_name=session_name,
                    workdir=workdir,
                    executable=executable,
                    executable_sha256=executable_sha256,
                )
                record = launched["record"]
                provider_home = _source_provider_home(
                    cell,
                    state_root=state_root,
                    record=record,
                    initial_home=initial_provider_home,
                )
                profile_material, launch_material, cell_ref, cell_digest = (
                    _source_profile_and_material(
                        cell,
                        state_root=state_root,
                        record=record,
                    )
                )
                sanitizer.add(record["native_session_id"], "<NATIVE_SESSION_ID>")
                sanitizer.write_json(evidence_root / cell.key / "source-launch.json", launched)

                spec_path = scratch / "source-spec.json"
                prepared_path = scratch / "prepared.json"
                result_path = scratch / "result.json"
                spec_path.write_text(
                    json.dumps(
                        {
                            "agent_id": record["stable_agent_id"],
                            "terminal_id": record["terminal_id"],
                            "generation": record["generation"],
                            "source_launch": {
                                "session_name": record["session_name"],
                                "profile_family": record["agent_profile"],
                                "harness": record["provider"],
                                "native_session_id": record["native_session_id"],
                                "route_provenance": {"provider_route": cell.route},
                            },
                            "launch": {
                                "model": cell.model,
                                "effort": cell.effort,
                                "working_directory": workdir,
                                "trusted_project_root": (
                                    workdir if cell.provider == "codex" else None
                                ),
                                "executable_path": executable,
                                "executable_sha256": executable_sha256,
                                "provider_home_path": provider_home,
                                "profile_material": profile_material,
                                "profile_unavailable_reason": (
                                    f"{cell.provider} canary profile has no standalone "
                                    "reference file"
                                ),
                                "provider": cell.provider,
                                "executable_version": (
                                    banner if cell.provider == "muse_cli" else normalized
                                ),
                            },
                            "compatibility_cell_ref": cell_ref,
                            "compatibility_cell_digest": cell_digest,
                            "launch_material": launch_material,
                        },
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                _runner(
                    "prepare",
                    environment=child_env,
                    scratch=evidence_root / cell.key,
                    evidence=sanitizer,
                    arguments=["--spec", str(spec_path), "--output", str(prepared_path)],
                )

                tmux.run("kill-pane", "-t", record["pane_id"])
                _wait_for_exit(int(record["pane_pid"]))
                _runner(
                    "execute",
                    environment=child_env,
                    scratch=evidence_root / cell.key,
                    evidence=sanitizer,
                    arguments=["--prepared", str(prepared_path), "--output", str(result_path)],
                )
                prepared = json.loads(prepared_path.read_text(encoding="utf-8"))
                result = json.loads(result_path.read_text(encoding="utf-8"))
                successor_pane = result["successor"].get("pane_id")
                assert successor_pane
                assert result["session_proof"] == cell.expected_session_proof
                try:
                    transcript = _wait_for_ready(
                        cell, tmux, successor_pane, record["native_session_id"]
                    )
                except AssertionError:
                    failed = str(tmux.out("capture-pane", "-p", "-S-300", "-t", successor_pane))
                    sanitizer.write_text(
                        evidence_root / cell.key / "successor-pane-failed.txt", failed
                    )
                    raise
                sanitizer.write_text(evidence_root / cell.key / "successor-pane.txt", transcript)
                sanitizer.write_json(evidence_root / cell.key / "executor-result.json", result)

                assert_shared_server_untouched(shared, shared_session, shared_identity)
                receipt = validate_receipt(
                    {
                        "schema": RECEIPT_SCHEMA,
                        "canary_id": str(uuid.uuid4()),
                        "recorded_at": datetime.now(timezone.utc)
                        .isoformat(timespec="seconds")
                        .replace("+00:00", "Z"),
                        "provider": cell.provider,
                        "harness": cell.provider,
                        "execution_mode": "native_tui",
                        "installed": {
                            "executable_path_sha256": _sha256_text(executable),
                            "executable_sha256": executable_sha256,
                            "version_banner_sha256": _sha256_text(banner),
                            "normalized_version": normalized,
                        },
                        "operation_id": prepared["request"]["operation_id"],
                        "restore_contract_id": prepared["contract"]["contract_id"],
                        "restore_contract_digest": prepared["contract"]["contract_digest"],
                        "launch_material_digest": result["launch_material_digest"],
                        "native_session_id_sha256": _sha256_text(record["native_session_id"]),
                        "session_proof": result["session_proof"],
                        "prior_generation_sha256": _sha256_text(record["generation"]),
                        "successor_generation_sha256": _sha256_text(
                            result["successor"]["generation"]
                        ),
                        "generation_changed": result["successor"]["generation"]
                        != record["generation"],
                        "effect_steps_observed": result["effect_steps"],
                        "admit_input_absent": "admit_input" not in result["effect_steps"],
                        "task_bytes_sent": False,
                        "outcome": result["outcome"]["outcome"],
                        "error_class": None,
                        "environment": {
                            "tmux_server_socket_sha256": _sha256_text(str(tmux.socket_path)),
                            "state_root_sha256": _sha256_text(str(state_root)),
                            "private_tmux": True,
                            "shared_server_untouched": True,
                        },
                    }
                )
                receipt_path = (
                    evidence_root / cell.key / (f"exact-restore-canary-{receipt['canary_id']}.json")
                )
                write_receipt(receipt_path, receipt)
            finally:
                server.stop()
                with contextlib.suppress(Exception):
                    tmux.kill_session(session_name, check=False)
