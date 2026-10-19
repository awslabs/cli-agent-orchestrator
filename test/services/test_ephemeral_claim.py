"""Claim ownership, lease and finalize boundaries against the real registry."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from test.services.test_ephemeral_service import CALLER, SPEC, create, create_store  # noqa: F401

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from cli_agent_orchestrator.clients import database

NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)


@pytest.fixture
def claimed_store(create_store, monkeypatch):
    service = create_store[0]
    monkeypatch.setattr(database, "_utcnow", lambda: NOW)
    name = create(create_store)["name"]
    tokens = iter(range(1, 100))
    monkeypatch.setattr(service.secrets, "token_hex", lambda n: f"{next(tokens):0{n * 2}x}")
    return create_store, name


def claim(env, name, caller=CALLER, **body):
    return env[0].claim_ephemeral_agent(name, body, caller)


def row_update(env, name, **values):
    with env[1]() as db:
        db.execute(
            update(database.EphemeralAgentModel)
            .where(database.EphemeralAgentModel.name == name)
            .values(**values)
        )
        db.commit()


def audit(name):
    return json.loads(Path(database.get_ephemeral_agent(name)["audit_path"]).read_bytes())


def test_claim_finalizes_identical_profile_and_shared_clock(claimed_store):
    env, name = claimed_store
    service, _, home, _, _ = env
    before = (home / "ephemeral/live" / (name + ".md")).read_bytes()
    assert database.get_ephemeral_agent(name)["created_at"] == NOW
    result = claim(env, name)
    assert result == dict(
        claim_id="00000000000000000000000000000001",
        provider="claude_code",
        effective_tools=["fs_read", "@cao-mcp-server"],
        replayed=False,
    )
    row = database.get_ephemeral_agent(name)
    assert row["state"] == "claimed" and row["claim_expires_at"] == NOW + timedelta(seconds=60)
    assert (home / "ephemeral/live" / (name + ".md")).read_bytes() == before
    assert row["profile_sha256"] == hashlib.sha256(before).hexdigest()
    assert audit(name)["events"][-1]["event"] == "finalized"
    assert not list((home / "ephemeral/live").glob("*.tmp"))


@pytest.mark.parametrize(
    "caller,body,rule",
    [
        (None, {}, "creator_unresolved"),
        ("ffffffff", {}, "creator_unresolved"),
        ("eeeeeeee", {"model": "secret"}, "not_owner"),
        (CALLER, {"model": "secret"}, "model_override_not_allowed"),
    ],
)
def test_early_refusal_order_and_unchanged_row(claimed_store, caller, body, rule):
    env, name = claimed_store
    if caller == "eeeeeeee":
        database.create_terminal(caller, "cao-session", "other", "claude_code", allowed_tools=["*"])
    before = database.get_ephemeral_agent(name)
    with pytest.raises(env[0].EphemeralPolicyError) as e:
        claim(env, name, caller, **body)
    assert e.value.rule == rule
    assert database.get_ephemeral_agent(name) == before
    assert len(database.list_all_terminals()) == (2 if caller == "eeeeeeee" else 1)


@pytest.mark.parametrize("field", ["idempotency_key", "claim_id"])
def test_launched_retry_precedes_model_and_tightened_policy(claimed_store, monkeypatch, field):
    env, name = claimed_store
    row_update(
        env,
        name,
        state="launched",
        launched_terminal_id="eeeeeeee",
        claim_id="a" * 32,
        idempotency_key="retry",
    )
    env[4]["ephemeral"]["allowed_providers"] = []
    result = claim(
        env, name, model="ignored", **{field: "a" * 32 if field == "claim_id" else "retry"}
    )
    assert result == dict(terminal_id="eeeeeeee", replayed=True)
    with pytest.raises(env[0].EphemeralPolicyError, match="already_claimed"):
        claim(env, name)


@pytest.mark.parametrize("bad", [None, "60", True, 0])
def test_invalid_lease_reverts_and_retries_after_fix(claimed_store, bad):
    env, name = claimed_store
    env[4]["ephemeral"]["claim_lease_seconds"] = bad
    for _ in range(2):
        with pytest.raises(env[0].EphemeralPolicyError) as e:
            claim(env, name)
        assert e.value.rule == "policy_config_error:claim_lease_seconds"
        assert "re-create" not in str(e.value)
        row = database.get_ephemeral_agent(name)
        assert (
            row["state"] == "pending" and row["claim_id"] is None and row["idempotency_key"] is None
        )
    assert audit(name)["refusals"]["policy_config_error:claim_lease_seconds"]["count"] == 2
    env[4]["ephemeral"]["claim_lease_seconds"] = 30
    assert claim(env, name)["replayed"] is False


def test_lease_overflow_refuses_before_even_lapse(claimed_store):
    env, name = claimed_store
    row_update(
        env, name, state="claimed", claim_id="a" * 32, claim_expires_at=NOW - timedelta(seconds=1)
    )
    before = database.get_ephemeral_agent(name)
    env[4]["ephemeral"]["claim_lease_seconds"] = 10**30
    with pytest.raises(env[0].EphemeralPolicyError) as e:
        claim(env, name)
    assert e.value.rule == "policy_config_error:claim_lease_seconds" and e.value.status_code == 400
    assert e.value.detail == "ephemeral.claim_lease_seconds is too large"
    assert database.get_ephemeral_agent(name) == before


@pytest.mark.parametrize("broken", [False, True])
def test_provider_removed_and_double_fault(claimed_store, broken):
    env, name = claimed_store
    row_update(env, name, provider="codex")
    if broken:
        env[4]["ephemeral"]["max_depth"] = 2
    with pytest.raises(env[0].EphemeralPolicyError) as e:
        claim(env, name)
    assert e.value.rule == (
        "policy_config_error:max_depth"
        if broken
        else "policy_changed_since_create:provider_not_allowed"
    )
    row = database.get_ephemeral_agent(name)
    assert row["state"] == ("pending" if broken else "gc")
    if not broken:
        assert row["gc_reason"] == "policy_changed"
        assert not list((env[2] / "ephemeral/live").iterdir())
        assert Path(row["audit_path"]).exists()


@pytest.mark.parametrize("state", ["pending", "claimed"])
def test_expired_name_collects_live_files(claimed_store, state):
    env, name = claimed_store
    row_update(
        env, name, state=state, expires_at=NOW, claim_expires_at=NOW if state == "claimed" else None
    )
    with pytest.raises(env[0].EphemeralPolicyError, match="ephemeral_expired"):
        claim(env, name)
    assert database.get_ephemeral_agent(name)["gc_reason"] == "ephemeral_expired"
    assert not list((env[2] / "ephemeral/live").iterdir())
    assert audit(name)["gc_reason"] == "ephemeral_expired"


def test_lapsed_claim_can_be_claimed_again_and_stale_end_does_nothing(claimed_store, monkeypatch):
    env, name = claimed_store
    first = claim(env, name)
    monkeypatch.setattr(database, "_utcnow", lambda: NOW + timedelta(seconds=60))
    second = claim(env, name)
    assert first["claim_id"] != second["claim_id"]
    env[0].end_claim(name, first["claim_id"])
    assert database.get_ephemeral_agent(name)["claim_id"] == second["claim_id"]


@pytest.mark.parametrize("damage", ["missing", "tampered", "symlink"])
def test_unavailable_spec_collects_or_redacts_io_failure(claimed_store, damage):
    env, name = claimed_store
    path = env[2] / "ephemeral/live" / (name + ".spec.json")
    if damage == "tampered":
        path.write_bytes(b"changed")
    else:
        path.unlink()
        if damage == "symlink":
            path.symlink_to(env[2] / "outside")
    with pytest.raises(env[0].EphemeralPolicyError) as e:
        claim(env, name)
    assert e.value.rule == ("unexpected_failure" if damage == "symlink" else "spec_unavailable")
    assert database.get_ephemeral_agent(name)["state"] == (
        "pending" if damage == "symlink" else "gc"
    )


@pytest.mark.parametrize(
    "body",
    [
        {"unknown_secret": "SECRET_VALUE"},
        {"model": ["SECRET_VALUE"]},
        ["SECRET_VALUE"],
        {"claim_id": "SECRET_VALUE"},
    ],
)
def test_claim_route_body_is_redacted(claimed_store, body, caplog):
    from cli_agent_orchestrator.api.main import app

    env, name = claimed_store
    client = TestClient(app, base_url="http://localhost")
    response = client.post(
        "/ephemeral-agents/" + name + "/claim", params={"caller_id": CALLER}, json=body
    )
    assert response.status_code == 422
    assert response.json()["detail"]["rule"] == "invalid_request"
    assert (
        "SECRET_VALUE" not in response.text + caplog.text
        and "unknown_secret" not in response.text + caplog.text
    )
    assert database.get_ephemeral_agent(name)["state"] == "pending"


def test_disabled_claim_never_reads_row_and_non_json_is_redacted(claimed_store, monkeypatch):
    from cli_agent_orchestrator.api.main import app

    env, name = claimed_store
    client = TestClient(app, base_url="http://localhost")
    env[4]["ephemeral"]["enabled"] = False
    before = database.get_ephemeral_agent(name)
    response = client.post(
        "/ephemeral-agents/" + name + "/claim", params={"caller_id": CALLER}, content="SECRET_VALUE"
    )
    assert response.status_code == 404 and response.json()["detail"]["rule"] == "ephemeral_disabled"
    assert database.get_ephemeral_agent(name) == before
    env[4]["ephemeral"]["enabled"] = True
    response = client.post(
        "/ephemeral-agents/" + name + "/claim", params={"caller_id": CALLER}, content="SECRET_VALUE"
    )
    assert response.status_code == 422 and "SECRET_VALUE" not in response.text


@pytest.mark.parametrize(
    "value",
    [
        datetime(2026, 10, 4, 12),
        NOW,
        datetime(2026, 10, 4, 14, tzinfo=timezone(timedelta(hours=2))),
    ],
)
def test_ephemeral_times_store_identical_utc_text(claimed_store, value):
    env, name = claimed_store
    row_update(
        env, name, created_at=value, expires_at=value, claim_expires_at=value, bound_at=value
    )
    with env[1]() as db:
        # SELECT only: inspect SQLite's exact stored wall-clock bytes.
        stored = (
            db.connection()
            .exec_driver_sql(
                "SELECT created_at, expires_at, claim_expires_at, bound_at FROM ephemeral_agents"
            )
            .one()
        )
    assert list(stored) == ["2026-10-04 12:00:00.000000"] * 4
    row = database.get_ephemeral_agent(name)
    assert all(
        row[key] == NOW for key in ["created_at", "expires_at", "claim_expires_at", "bound_at"]
    )


def test_archive_cleanup_failure_cannot_abort_claim(claimed_store, monkeypatch):
    env, name = claimed_store
    service = env[0]
    cleanup = service._cleanup

    def fail_audit_cleanup(owned):
        if owned and ".json." in owned[0][0].name:
            raise PermissionError("archive cleanup unavailable")
        cleanup(owned)

    monkeypatch.setattr(service, "_cleanup", fail_audit_cleanup)
    assert claim(env, name)["replayed"] is False
    assert database.get_ephemeral_agent(name)["state"] == "claimed"


def test_unknown_claim_and_finalize_are_structured(claimed_store):
    env, _ = claimed_store
    for call in [
        lambda: claim(env, "Ramones-unknown_name-abcd"),
        lambda: env[0].finalize("Ramones-unknown_name-abcd", "a" * 32),
    ]:
        with pytest.raises(env[0].EphemeralPolicyError) as exc:
            call()
        assert exc.value.rule == "unknown_ephemeral" and exc.value.status_code == 404


def test_stale_finalize_guard_cannot_resurrect_claim(claimed_store):
    env, name = claimed_store
    token = claim(env, name)["claim_id"]
    env[0].end_claim(name, token)
    before = database.get_ephemeral_agent(name)
    with pytest.raises(env[0].EphemeralPolicyError, match="claim_expired"):
        env[0].finalize(name, token)
    assert database.get_ephemeral_agent(name) == before
    assert not list((env[2] / "ephemeral/live").glob("*.tmp"))


def test_nonregular_spec_refuses_without_reading_it(claimed_store):
    env, name = claimed_store
    path = env[2] / "ephemeral/live" / (name + ".spec.json")
    path.unlink()
    path.mkdir()
    with pytest.raises(env[0].EphemeralPolicyError, match="unexpected_failure"):
        claim(env, name)
    assert database.get_ephemeral_agent(name)["state"] == "pending"


def test_missing_archive_does_not_control_claim(claimed_store):
    env, name = claimed_store
    Path(database.get_ephemeral_agent(name)["audit_path"]).unlink()
    assert claim(env, name)["replayed"] is False
    assert database.get_ephemeral_agent(name)["state"] == "claimed"


def test_stale_claim_id_cannot_touch_a_newer_claim(claimed_store):
    env, name = claimed_store
    row_update(
        env,
        name,
        state="claimed",
        claim_id="b" * 32,
        claim_expires_at=NOW + timedelta(seconds=30),
        profile_sha256="0" * 64,
    )
    before = database.get_ephemeral_agent(name)
    with pytest.raises(env[0].EphemeralPolicyError, match="claim_expired"):
        env[0].finalize(name, "a" * 32)
    config = env[0].read_settings()
    config["allowed_providers"] = ["codex"]
    with pytest.raises(env[0].EphemeralPolicyError, match="provider_not_allowed"):
        env[0]._recheck_policy(dict(before), config, "a" * 32)
    assert database.get_ephemeral_agent(name) == before


def test_workflow_policy_error_reverts_to_pending(claimed_store):
    env, name = claimed_store
    row_update(
        env,
        name,
        owner_kind="workflow_step",
        state="claimed",
        claim_id="a" * 32,
        claim_expires_at=NOW + timedelta(seconds=30),
        idempotency_key="retry",
    )
    config = env[0].read_settings()
    config["max_depth"] = 2
    with pytest.raises(env[0].EphemeralPolicyError, match="policy_config_error:max_depth"):
        env[0]._recheck_policy(database.get_ephemeral_agent(name), config, "a" * 32)
    row = database.get_ephemeral_agent(name)
    assert row["state"] == "pending" and row["gc_reason"] is None
    assert row["claim_id"] is row["claim_expires_at"] is row["idempotency_key"] is None


@pytest.mark.parametrize("kind", ["spec", "archive"])
def test_fifo_store_reads_refuse_without_blocking(claimed_store, kind):
    import os
    import threading

    env, name = claimed_store
    path = (
        env[2] / "ephemeral/live" / (name + ".spec.json")
        if kind == "spec"
        else Path(database.get_ephemeral_agent(name)["audit_path"])
    )
    path.unlink()
    os.mkfifo(path)
    done = threading.Event()
    errors = []

    def read():
        try:
            env[0]._read_regular(path)
        except OSError as exc:
            errors.append(exc)
        finally:
            done.set()

    worker = threading.Thread(target=read, daemon=True)
    worker.start()
    completed = done.wait(timeout=2)
    if not completed:
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
    worker.join(timeout=2)
    assert completed, "store read blocked on FIFO"
    assert len(errors) == 1 and "unsafe" in str(errors[0])


def test_policy_messages_use_contract_parentheses(claimed_store):
    env, name = claimed_store
    config = env[0].read_settings()
    config["max_depth"] = 2
    with pytest.raises(env[0].EphemeralPolicyError) as exc:
        env[0]._recheck_policy(database.get_ephemeral_agent(name), config, None)
    assert exc.value.detail.startswith("(") and "); the operator must fix" in exc.value.detail
    config["max_depth"] = 1
    config["allowed_providers"] = ["codex"]
    with pytest.raises(env[0].EphemeralPolicyError) as exc:
        env[0]._recheck_policy(database.get_ephemeral_agent(name), config, None)
    assert exc.value.detail == "(claude_code); re-create the ephemeral agent"
