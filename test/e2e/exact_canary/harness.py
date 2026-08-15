"""Builders shared by deterministic tests and installed exact-restore canaries."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any

from cli_agent_orchestrator.services import operation_journal as oj
from cli_agent_orchestrator.services import restore_contract as rc
from cli_agent_orchestrator.services import session_lifecycle as sl
from cli_agent_orchestrator.services import stable_agent_roster as roster


class CanaryHarnessInvalid(ValueError):
    """The observed source cannot truthfully authorize an exact canary."""


@dataclass(frozen=True)
class CanarySource:
    session_name: str
    terminal_id: str
    generation: str
    agent_id: str
    lineage_id: str
    incarnation_id: str
    native_session_id: str
    role: str
    profile_family: str
    harness: str
    route_provenance: dict[str, str]
    execution_mode: str
    model: str
    effort: str
    working_directory: str
    trusted_project_root: str | None
    executable_path: str
    executable_sha256: str
    provider_home_path: str | None
    profile_material: dict[str, str] | None = None
    profile_unavailable_reason: str | None = None
    provider: str = "codex"
    executable_version: str | None = None

    def launch_facts(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "effort": self.effort,
            "working_directory": self.working_directory,
            "trusted_project_root": self.trusted_project_root,
            "executable_path": self.executable_path,
            "executable_sha256": self.executable_sha256,
            "provider_home_path": self.provider_home_path,
            "provider": self.provider,
            "executable_version": self.executable_version,
        }

    @classmethod
    def from_roster_and_launch(
        cls,
        *,
        agent: dict[str, Any],
        lineage: dict[str, Any],
        incarnation: dict[str, Any],
        launch: dict[str, Any],
    ) -> "CanarySource":
        native_id = lineage.get("native_session_id")
        if not isinstance(native_id, str) or not native_id:
            raise CanaryHarnessInvalid("the source roster lineage has no native session id")
        return cls(
            session_name=agent["session_name"],
            terminal_id=incarnation["terminal_id"],
            generation=incarnation["generation"],
            agent_id=agent["agent_id"],
            lineage_id=lineage["lineage_id"],
            incarnation_id=incarnation["incarnation_id"],
            native_session_id=native_id,
            role=agent["role"],
            profile_family=agent["profile_family"],
            harness=lineage["harness"],
            route_provenance=dict(lineage.get("route_provenance") or {}),
            execution_mode=incarnation["execution_mode"],
            model=launch["model"],
            effort=launch["effort"],
            working_directory=launch["working_directory"],
            trusted_project_root=launch.get("trusted_project_root"),
            executable_path=launch["executable_path"],
            executable_sha256=launch["executable_sha256"],
            provider_home_path=launch.get("provider_home_path"),
            profile_material=(
                dict(launch["profile_material"])
                if launch.get("profile_material") is not None
                else None
            ),
            profile_unavailable_reason=launch.get("profile_unavailable_reason"),
            provider=launch.get("provider") or lineage["harness"],
            executable_version=launch.get("executable_version"),
        )


def _canonical_directory(path: str, *, field: str) -> str:
    if not isinstance(path, str) or not os.path.isdir(path) or os.path.realpath(path) != path:
        raise CanaryHarnessInvalid(f"{field} must be an existing canonical directory")
    return path


def build_restore_contract(source: CanarySource) -> rc.RestoreContract:
    _canonical_directory(source.working_directory, field="working_directory")
    if source.provider_home_path is not None:
        _canonical_directory(source.provider_home_path, field="provider_home_path")
    if (
        not os.path.isfile(source.executable_path)
        or os.path.realpath(source.executable_path) != source.executable_path
    ):
        raise CanaryHarnessInvalid("executable_path must be an existing canonical file")
    executable = {"path": source.executable_path, "sha256": source.executable_sha256}
    if source.executable_version:
        executable["version"] = source.executable_version
    return rc.RestoreContract(
        agent_id=source.agent_id,
        lineage_id=source.lineage_id,
        terminal_id=source.terminal_id,
        generation=source.generation,
        native_session_id=source.native_session_id,
        harness=source.harness,
        provider=source.provider,
        route_provenance=source.route_provenance,
        execution_mode=source.execution_mode,
        model=rc.ContractFact.present(source.model),
        effort=rc.ContractFact.present(source.effort),
        working_directory=source.working_directory,
        trusted_project_root=source.trusted_project_root,
        executable=rc.ContractFact.present(executable),
        profile_material=(
            rc.ContractFact.present(source.profile_material)
            if source.profile_material is not None
            else rc.ContractFact.unavailable(
                source.profile_unavailable_reason
                or f"{source.harness} profile material has no standalone reference lane"
            )
        ),
        provider_home_facts=(
            rc.ContractFact.present({"provider_home_path": source.provider_home_path})
            if source.provider_home_path is not None
            else rc.ContractFact.unavailable(
                f"{source.harness} has no explicit provider-home carrier for this cell"
            )
        ),
    )


def build_operation_request(
    source: CanarySource,
    stored_contract: dict[str, Any],
    *,
    operation_id: str | None = None,
    compatibility_cell_ref: str | None = None,
    compatibility_cell_digest: str | None = None,
) -> oj.OperationRequest:
    agent = roster.get_agent(source.agent_id)
    lifecycle = sl.describe(source.session_name)
    return oj.OperationRequest(
        operation_id=operation_id or str(uuid.uuid4()),
        session_name=source.session_name,
        agent_id=source.agent_id,
        roster_revision=agent["revision"],
        role=source.role,
        profile_family=source.profile_family,
        lineage_id=source.lineage_id,
        harness=source.harness,
        native_session_id=source.native_session_id,
        prior_terminal_id=source.terminal_id,
        prior_generation=source.generation,
        prior_incarnation_id=source.incarnation_id,
        lifecycle_epoch=lifecycle["epoch"],
        lifecycle_observation=lifecycle["lifecycle"],
        restore_contract_id=stored_contract["contract_id"],
        restore_contract_digest=stored_contract["contract_digest"],
        restore_contract_schema=rc.SCHEMA_VERSION,
        route_provider=source.provider,
        model_requested=source.model,
        effort_requested=source.effort,
        execution_mode_requested=source.execution_mode,
        compatibility_cell_ref=compatibility_cell_ref,
        compatibility_cell_digest=compatibility_cell_digest,
    )
