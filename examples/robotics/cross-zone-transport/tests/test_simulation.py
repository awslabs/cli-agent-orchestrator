from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from pydantic import ValidationError
from simulation import Scene, World

EXAMPLE = Path(__file__).resolve().parents[1]


@pytest.fixture
def scene():
    return Scene.model_validate_json((EXAMPLE / "site.json").read_text())


@pytest.fixture
def world(scene):
    return World(scene, allow_motion=True)


def finish(world, actor, command_id):
    for _ in range(math.ceil(world.scene.action_timeout_seconds / world.scene.step_seconds)):
        world.tick()
        result = world.command(actor, command_id)
        if result["status"] not in ("accepted", "running"):
            return result
    pytest.fail("the bounded simulator action did not terminate")


def test_transport_requires_measured_arrival_and_receiver_acceptance(world):
    first = world.move("west", "leg-one", "cart-west", "tote", "dock")
    assert first["status"] == "accepted"
    assert world.offer("west", "too-early", "tote", "east")["status"] == "rejected"
    assert world.move("east", "premature", "cart-east", "tote", "etch")["status"] == "rejected"

    assert finish(world, "west", "leg-one")["status"] == "finished"
    state = world.observe()
    assert state["payloads"]["tote"]["xy"] == pytest.approx([0, 0], abs=0.01)
    assert state["payloads"]["tote"]["owner"] == "west"
    offered = world.offer("west", "offer-at-dock", "tote", "east")
    assert offered["status"] == "finished"
    assert world.observe()["payloads"]["tote"]["owner"] == "west"
    assert (
        world.accept("east", "take-custody", "tote", "cart-east", "offer-at-dock")["status"]
        == "finished"
    )
    assert world.move("east", "leg-two", "cart-east", "tote", "etch")["status"] == "accepted"
    assert finish(world, "east", "leg-two")["status"] == "finished"
    final = world.observe()
    assert final["payloads"]["tote"]["xy"] == pytest.approx([2, 0], abs=0.01)
    assert final["payloads"]["tote"]["at"] == "etch"
    assert final["payloads"]["tote"]["owner"] == "east"
    assert final["observed_at"]
    assert final["simulation_seconds"] > 0


def test_different_coordinates_names_fixture_capacity_and_direction():
    scene = Scene.model_validate_json((EXAMPLE / "return-site.json").read_text())
    world = World(scene, allow_motion=True)
    world.move("stores", "return-leg", "return-cart", "sample-tray", "transfer")
    assert finish(world, "stores", "return-leg")["status"] == "finished"
    assert world.offer("stores", "offer", "sample-tray", "assembly")["status"] == "finished"
    assert (
        world.accept("assembly", "accept", "sample-tray", "inspection-cart", "offer")["status"]
        == "finished"
    )
    world.move("assembly", "inspect-leg", "inspection-cart", "sample-tray", "inspection")
    assert finish(world, "assembly", "inspect-leg")["status"] == "finished"
    assert world.observe()["payloads"]["sample-tray"]["xy"] == pytest.approx([1, -1], abs=0.01)


def test_same_zone_transport_does_not_require_a_handoff(world):
    world.move("west", "one-leg", "cart-west", "tote", "dock")
    assert finish(world, "west", "one-leg")["status"] == "finished"
    assert world.observe()["payloads"]["tote"]["owner"] == "west"
    assert world.observe()["payloads"]["tote"]["offer"] is None


@pytest.mark.parametrize(
    "actor,robot,payload,destination,reason",
    [
        ("observer", "cart-west", "tote", "dock", "not_robot_owner"),
        ("east", "cart-west", "tote", "dock", "not_robot_owner"),
        ("west", "missing", "tote", "dock", "robot_unavailable"),
        ("west", "cart-west", "missing", "dock", "payload_unavailable"),
        ("west", "cart-west", "tote", "cleanroom", "destination_unavailable"),
        ("west", "cart-west", "tote", "etch", "destination_unavailable"),
        ("east", "cart-east", "tote", "etch", "not_payload_owner"),
    ],
)
def test_refusals_do_not_move_anything(world, actor, robot, payload, destination, reason):
    before = world.observe()["payloads"]
    result = world.move(actor, "refused", robot, payload, destination)
    assert result["status"] == "rejected"
    assert result["reason"] == reason
    world.tick()
    assert world.observe()["payloads"] == before


@pytest.mark.parametrize(
    "mass,fixture,reason",
    [(9, "tote_clamp", "payload_too_heavy"), (3, "unavailable", "fixture_unavailable")],
)
def test_capability_checks_use_scene_values(scene, mass, fixture, reason):
    scene.payloads["tote"].mass_kg = mass
    scene.payloads["tote"].fixture = fixture
    world = World(scene, allow_motion=True)
    result = world.move("west", "capability", "cart-west", "tote", "dock")
    assert result["reason"] == reason


def test_motion_requires_operator_opt_in(scene):
    world = World(scene)
    assert world.move("west", "no-approval", "cart-west", "tote", "dock")["reason"] == (
        "motion_not_approved"
    )
    assert world.offer("west", "offer", "tote", "east")["reason"] == "motion_not_approved"
    assert world.accept("east", "accept", "tote", "cart-east", "offer")["reason"] == (
        "motion_not_approved"
    )


def test_capacity_limit_is_inclusive(scene):
    scene.payloads["tote"].mass_kg = scene.robots["cart-west"].payload_kg
    world = World(scene, allow_motion=True)
    assert world.move("west", "limit", "cart-west", "tote", "dock")["status"] == "accepted"
    scene.payloads["tote"].mass_kg += 0.001
    heavier = World(scene, allow_motion=True)
    assert heavier.move("west", "too-heavy", "cart-west", "tote", "dock")["reason"] == (
        "payload_too_heavy"
    )


def test_receiving_robot_must_actually_be_at_the_handoff(scene):
    scene.robots["cart-east"].at = "etch"
    world = World(scene, allow_motion=True)
    world.move("west", "dock", "cart-west", "tote", "dock")
    finish(world, "west", "dock")
    world.offer("west", "offer", "tote", "east")
    assert world.accept("east", "accept", "tote", "cart-east", "offer")["reason"] == (
        "not_at_handoff"
    )
    assert world.observe()["payloads"]["tote"]["owner"] == "west"


def test_duplicate_command_is_not_executed_twice(world):
    first = world.move("west", "once", "cart-west", "tote", "dock")
    assert world.move("west", "once", "cart-west", "tote", "dock") == first
    assert finish(world, "west", "once")["status"] == "finished"
    before = world.observe()["simulation_seconds"]
    assert world.move("west", "once", "cart-west", "tote", "dock")["status"] == "finished"
    assert len(world.observe()["commands"]) == 1
    assert world.observe()["simulation_seconds"] == before
    assert world.move("west", "once", "cart-west", "tote", "stock")["reason"] == (
        "command_id_conflict"
    )


def test_concurrent_commands_cannot_take_a_busy_robot_or_tote(world):
    world.move("west", "first", "cart-west", "tote", "dock")
    assert world.move("west", "second", "cart-west", "tote", "stock")["reason"] == "busy"
    assert world.offer("west", "offer", "tote", "east")["reason"] == "busy"


def test_unknown_command_is_not_success(world):
    assert world.command("west", "not-submitted")["status"] == "unknown"


def test_interrupt_preserves_position_and_custody_and_locks_out_future_motion(world):
    world.move("west", "interrupted", "cart-west", "tote", "dock")
    for _ in range(10):
        world.tick()
    position = world.observe()["payloads"]["tote"]["xy"]
    assert -2 < position[0] < 0
    stopped = world.stop()
    assert stopped["stopped"] is True
    assert world.command("west", "interrupted")["status"] == "interrupted"
    for _ in range(20):
        world.tick()
    final = world.observe()
    assert final["payloads"]["tote"]["xy"] == position
    assert final["payloads"]["tote"]["at"] is None
    assert final["payloads"]["tote"]["owner"] == "west"
    assert world.move("west", "retry", "cart-west", "tote", "dock")["reason"] == "stopped"
    assert world.offer("west", "offer", "tote", "east")["reason"] == "stopped"


def test_timeout_is_not_a_completed_transport(world):
    world.move("west", "timeout", "cart-west", "tote", "dock")
    world.tick(now=world.started_at + world.scene.action_timeout_seconds + 1)
    result = world.command("west", "timeout")
    assert result["status"] == "failed"
    assert result["reason"] == "action_timeout"
    assert world.observe()["payloads"]["tote"]["at"] == "stock"


def test_sender_cannot_move_an_offered_tote(world):
    world.move("west", "dock", "cart-west", "tote", "dock")
    finish(world, "west", "dock")
    world.offer("west", "offer", "tote", "east")
    assert world.move("west", "take-back", "cart-west", "tote", "stock")["reason"] == (
        "handoff_pending"
    )
    assert world.accept("west", "wrong-receiver", "tote", "cart-west", "offer")["reason"] == (
        "not_handoff_receiver"
    )
    assert world.accept("east", "wrong-offer", "tote", "cart-east", "other-offer")["reason"] == (
        "offer_mismatch"
    )


def test_custody_transfer_checks_actual_pose_not_only_the_previous_reply(world):
    world.move("west", "dock", "cart-west", "tote", "dock")
    finish(world, "west", "dock")
    world.offer("west", "offer", "tote", "east")
    # Perturb the simulator after a successful reply, not the bookkeeping.
    world.data.mocap_pos[world.payload_mocap["tote"], 0] = 0.02
    world.forward()
    result = world.accept("east", "accept", "tote", "cart-east", "offer")
    assert result["status"] == "rejected"
    assert result["reason"] == "not_at_handoff"
    assert world.observe()["payloads"]["tote"]["owner"] == "west"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data["locations"]["stock"].update(xy=[float("nan"), 0]),
        lambda data: data["robots"]["cart-west"].update(speed_m_s=0),
        lambda data: data["robots"]["cart-west"].update(zone="unowned"),
        lambda data: data["locations"]["dock"].update(xy=[0.5, 0]),
        lambda data: data.update(action_timeout_seconds=-1),
        lambda data: data.update(hardware_url="tcp://a-real-robot"),
    ],
)
def test_invalid_or_hardware_configuration_fails_closed(mutate):
    data = json.loads((EXAMPLE / "site.json").read_text())
    mutate(data)
    with pytest.raises(ValidationError):
        Scene.model_validate(data)
