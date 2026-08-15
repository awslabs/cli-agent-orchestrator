"""Fresh-process runner for one installed exact-restore canary.

The parent test fixes ``CAO_STATE_ROOT`` and the private-tmux shim on PATH
before importing this module.  Keeping all CAO service imports here makes the
process boundary enforce that ordering instead of relying on pytest import
order or cache resets.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
from pathlib import Path
from test.e2e.exact_canary.harness import (
    CanaryHarnessInvalid,
    CanarySource,
    build_operation_request,
    build_restore_contract,
)
from test.e2e.exact_canary.receipt import EXPECTED_EFFECT_STEPS
from typing import Any

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import exact_executor as xe
from cli_agent_orchestrator.services import native_attachment as na
from cli_agent_orchestrator.services import operation_journal as oj
from cli_agent_orchestrator.services import restore_contract as rc
from cli_agent_orchestrator.services import stable_agent_roster as roster


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CanaryHarnessInvalid(f"{path} must contain one JSON object")
    return value


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _prepare(spec_path: Path, output_path: Path) -> None:
    database.init_db()
    spec = _read(spec_path)
    try:
        agent = roster.get_agent(spec["agent_id"])
    except roster.StableAgentNotFound:
        source_launch = spec["source_launch"]
        attachment = na.get(source_launch["harness"], source_launch["native_session_id"])
        owner = (attachment or {}).get("owner") or {}
        if (
            attachment is None
            or attachment.get("state") != na.ATTACHED
            or owner.get("terminal_id") != spec["terminal_id"]
            or owner.get("generation") != spec["generation"]
        ):
            raise CanaryHarnessInvalid(
                "the zero-turn source attachment is not held by the exact launched generation"
            )
        bound = roster.bind_generation(
            roster.BindingContract(
                agent_id=spec["agent_id"],
                session_name=source_launch["session_name"],
                role=roster.ROLE_WORKER,
                profile_family=source_launch["profile_family"],
                harness=source_launch["harness"],
                native_session_id=source_launch["native_session_id"],
                acquisition_method=roster.ACQUISITION_ZERO_TURN_BOOTSTRAP,
                route_provenance=source_launch["route_provenance"],
                terminal_id=spec["terminal_id"],
                generation=spec["generation"],
                pane_id=owner.get("pane_id"),
                pane_pid=(
                    int((owner.get("process_identity") or {})["pid"])
                    if (owner.get("process_identity") or {}).get("pid")
                    else None
                ),
                process_identity=owner.get("process_identity"),
                execution_mode="native_tui",
                lineage_origin=roster.LINEAGE_ORIGIN_INITIAL,
            )
        )
        agent = roster.get_agent(bound["agent"]["agent_id"])
    lineage = agent.get("current_lineage")
    incarnation = agent.get("current_incarnation")
    if not isinstance(lineage, dict) or not isinstance(incarnation, dict):
        raise CanaryHarnessInvalid("the installed source is not a current bound roster generation")
    if (
        incarnation.get("terminal_id") != spec["terminal_id"]
        or incarnation.get("generation") != spec["generation"]
    ):
        raise CanaryHarnessInvalid("the installed source changed before contract publication")
    source = CanarySource.from_roster_and_launch(
        agent=agent,
        lineage=lineage,
        incarnation=incarnation,
        launch=spec["launch"],
    )
    contract = build_restore_contract(source)
    stored = rc.publish_contract(contract)
    roster.transition_dormant(
        terminal_id=source.terminal_id,
        generation=source.generation,
        agent_id=source.agent_id,
        lineage_id=source.lineage_id,
        contract_digest=contract.digest(),
        reason="M3-B4 installed exact-restore canary",
    )
    request = build_operation_request(
        source,
        stored,
        compatibility_cell_ref=spec.get("compatibility_cell_ref"),
        compatibility_cell_digest=spec.get("compatibility_cell_digest"),
    )
    _write(
        output_path,
        {
            "source": dataclasses.asdict(source),
            "contract": {
                "contract_id": stored["contract_id"],
                "contract_digest": stored["contract_digest"],
            },
            "request": dataclasses.asdict(request),
            "launch_material": spec.get("launch_material") or {},
        },
    )


async def _execute_async(prepared_path: Path, output_path: Path) -> None:
    database.init_db()
    prepared = _read(prepared_path)
    source = CanarySource(**prepared["source"])
    request = oj.OperationRequest(**prepared["request"])
    outcome = await xe.execute(request, material=xe.LaunchMaterial(**prepared["launch_material"]))
    result = xe.get_result(request.operation_id)
    effects = oj.list_effect_intents(request.operation_id)
    effect_steps = [item["effect_step"] for item in effects]
    agent = roster.get_agent(source.agent_id)
    successor = agent.get("current_incarnation")
    lineage = agent.get("current_lineage")
    incarnations = roster.list_incarnations(agent_id=source.agent_id)
    prior = next(
        (
            item
            for item in incarnations
            if item["terminal_id"] == source.terminal_id and item["generation"] == source.generation
        ),
        None,
    )
    attachment = na.get(source.harness, source.native_session_id)

    if outcome.get("outcome") != oj.RESULT_ACCEPTED or outcome.get("admitted") is not False:
        raise AssertionError(f"exact executor did not accept a bound-only successor: {outcome}")
    if effect_steps != list(EXPECTED_EFFECT_STEPS):
        raise AssertionError(f"unexpected exact-executor effects: {effect_steps}")
    if not isinstance(successor, dict) or successor.get("disposition") != roster.INCARNATION_BOUND:
        raise AssertionError(f"successor is not bound: {successor}")
    if successor.get("generation") == source.generation:
        raise AssertionError("exact executor reused the prior generation")
    if successor.get("lineage_id") != source.lineage_id:
        raise AssertionError("exact executor changed the native lineage")
    if not isinstance(prior, dict) or prior.get("disposition") != roster.INCARNATION_RETIRED:
        raise AssertionError(f"prior incarnation is not retired: {prior}")
    if (
        not isinstance(lineage, dict)
        or lineage.get("native_session_id") != source.native_session_id
    ):
        raise AssertionError("successor lineage did not retain the exact native session id")
    owner = (attachment or {}).get("owner") or {}
    if (
        attachment is None
        or attachment.get("state") != na.ATTACHED
        or owner.get("terminal_id") != successor.get("terminal_id")
        or owner.get("generation") != successor.get("generation")
    ):
        raise AssertionError(f"native attachment does not belong to the successor: {attachment}")

    evidence = result.get("result_evidence") or {}
    _write(
        output_path,
        {
            "outcome": outcome,
            "result": result,
            "effect_steps": effect_steps,
            "agent": agent,
            "prior": prior,
            "successor": successor,
            "attachment": attachment,
            "session_proof": evidence.get("session_proof"),
            "launch_material_digest": evidence.get("launch_material_digest"),
        },
    )


def _execute(prepared_path: Path, output_path: Path) -> None:
    asyncio.run(_execute_async(prepared_path, output_path))


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--spec", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    execute = subparsers.add_parser("execute")
    execute.add_argument("--prepared", type=Path, required=True)
    execute.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        _prepare(args.spec, args.output)
    else:
        _execute(args.prepared, args.output)


if __name__ == "__main__":
    main()
