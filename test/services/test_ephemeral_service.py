"""Create-only ephemeral storage and the authoritative policy boundary."""

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import frontmatter
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.services import settings_service
from cli_agent_orchestrator.utils import agent_profiles

CALLER = "abcd1234"
SPEC = {"spec_version": 1, "purpose": "log_triage", "brief": "Inspect logs.", "tools": ["fs_read"]}


@pytest.fixture
def create_store(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services import ephemeral_service as service

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    database.create_terminal(
        CALLER,
        "cao-session",
        "window",
        "claude_code",
        agent_profile="reviewer",
        allowed_tools=["@cao-mcp-server", "fs_read", "fs_list"],
    )
    home = tmp_path / "cao"
    home.mkdir()
    installed = tmp_path / "installed"
    installed.mkdir()
    monkeypatch.setattr(service, "EPHEMERAL_DIR", home / "ephemeral")
    monkeypatch.setattr(agent_profiles, "EPHEMERAL_LIVE_DIR", home / "ephemeral" / "live")
    monkeypatch.setattr(agent_profiles, "LOCAL_AGENT_STORE_DIR", installed)
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", home / "settings.json")
    monkeypatch.setattr(settings_service, "get_agent_dirs", lambda: {})
    monkeypatch.setattr(settings_service, "get_extra_agent_dirs", lambda: [])
    monkeypatch.setattr(settings_service, "get_disabled_agent_dirs", lambda: [])
    monkeypatch.setattr(service.secrets, "choice", lambda _: "Ramones")
    monkeypatch.setattr(service.secrets, "token_hex", lambda _: "3f9a")
    config = {"ephemeral": {"enabled": True}}
    monkeypatch.setattr(settings_service, "_load_or_raise", lambda: config)
    yield service, factory, home, installed, config
    engine.dispose()


def create(env, spec=None, caller=CALLER):
    return env[0].create_ephemeral_agent(dict(SPEC if spec is None else spec), caller)


def test_create_files_hashes_and_response(create_store):
    service, factory, home, _, _ = create_store
    result = create(create_store)
    assert set(result) == {
        "name",
        "provider",
        "effective_tools",
        "model_tier",
        "effort",
        "expires_at",
        "spec_sha256",
        "notes",
    }
    assert result["effective_tools"] == ["fs_read", "@cao-mcp-server"]
    assert result["model_tier"] is None and result["effort"] is None
    assert result["provider"] == "claude_code"
    assert datetime.fromisoformat(result["expires_at"]).utcoffset().total_seconds() == 0
    assert result["notes"] == [
        "model: provider default (tier omitted, no default_tier)",
        "effort: provider default (effort omitted, no default_effort)",
        "launch: not supported by this server version",
    ]
    row = database.get_ephemeral_agent(result["name"])
    assert row["owner_kind"] == "terminal" and row["owner_id"] == CALLER
    assert row["session_name"] == "cao-session" and row["state"] == "pending"
    assert row["launched_terminal_id"] is None
    live = home / "ephemeral" / "live"
    stored = live / (result["name"] + ".spec.json")
    profile = live / (result["name"] + ".md")
    archive = Path(row["audit_path"])
    assert (
        hashlib.sha256(stored.read_bytes()).hexdigest()
        == row["spec_sha256"]
        == result["spec_sha256"]
    )
    assert hashlib.sha256(profile.read_bytes()).hexdigest() == row["profile_sha256"]
    assert set(json.loads(stored.read_bytes())) == {
        "spec_version",
        "purpose",
        "brief",
        "description",
        "provider",
        "tools",
        "model_tier",
        "effort",
    }
    for path in (stored, profile, archive):
        assert path.stat().st_mode & 0o777 == 0o600
    for path in (live.parent, live, archive.parent.parent, archive.parent):
        assert path.stat().st_mode & 0o777 == 0o700
    audit = json.loads(archive.read_bytes())
    assert set(audit) == {
        "audit_version",
        "name",
        "spec",
        "spec_sha256",
        "profile_sha256",
        "provider",
        "effective_tools",
        "creator",
        "session_name",
        "created_at",
        "expires_at",
        "events",
    }
    assert audit["creator"] == {
        "terminal_id": CALLER,
        "agent_profile": "reviewer",
        "tools": ["@cao-mcp-server", "fs_read", "fs_list"],
    }
    assert audit["events"] == [{"at": audit["created_at"], "event": "created"}]
    assert result["name"] not in {p["name"] for p in agent_profiles.list_agent_profiles()}
    assert database.get_terminal_metadata(CALLER)["ephemeral"] is False


@pytest.mark.parametrize(
    "brief",
    [
        " \t\nlead and tail\t \n",
        "[[BEGIN CREATOR BRIEF]] and [[END CREATOR BRIEF]]",
        "---\nmcpServers: injected\n${HOME} $X $$",
    ],
)
def test_brief_is_internal_verbatim_prompt_structure(create_store, brief):
    service, _, home, _, _ = create_store
    result = create(create_store, {**SPEC, "brief": brief})
    raw = (home / "ephemeral" / "live" / (result["name"] + ".md")).read_bytes()
    parsed, source = agent_profiles.load_launch_profile(result["name"])
    prefix, suffix = service.render_prompt_parts(
        result["name"], CALLER, SPEC["purpose"], result["effective_tools"]
    )
    assert parsed.system_prompt == prefix + brief + suffix
    assert source == agent_profiles.ProfileSource.EPHEMERAL
    assert set(frontmatter.loads(raw.decode()).metadata) == {
        "name",
        "description",
        "provider",
        "allowedTools",
        "mcpServers",
    }
    assert set(parsed.mcpServers) == {"cao-mcp-server"}
    assert parsed.native_agent is None and parsed.hooks is None


@pytest.mark.parametrize(
    "patch,rule",
    [
        ({"tools": ["execute_bash"]}, "tool_exceeds_creator"),
        ({"brief": "AKIAIOSFODNN7EXAMPLE"}, "secret_detected"),
        ({"brief": "x" * 8193}, "brief_too_large"),
        ({"provider": "codex"}, "provider_not_allowed"),
    ],
)
def test_policy_rejections_leave_nothing(create_store, patch, rule, caplog):
    service, factory, home, _, _ = create_store
    with pytest.raises(service.EphemeralPolicyError) as error:
        create(create_store, {**SPEC, **patch})
    assert error.value.rule == rule
    assert not list((home / "ephemeral").rglob("*.md"))
    assert not list((home / "ephemeral").rglob("*.json"))
    with factory() as db:
        assert db.query(database.EphemeralAgentModel).count() == 0
    if "brief" in patch:
        assert patch["brief"] not in str(error.value) + caplog.text


@pytest.mark.parametrize(
    "key,values",
    [
        ("model_tier", ["small", "medium", "large", "auto"]),
        ("effort", ["low", "medium", "high", "auto"]),
    ],
)
def test_tiers_and_effort_not_yet_supported(create_store, key, values):
    service = create_store[0]
    for value in values:
        with pytest.raises(service.EphemeralPolicyError) as err:
            create(create_store, {**SPEC, key: value})
        assert err.value.rule == (
            "auto_requires_decision_platform"
            if value == "auto"
            else "tier_not_supported" if key == "model_tier" else "effort_not_supported"
        )
    with pytest.raises(service.EphemeralPolicyError) as err:
        create(create_store, {**SPEC, "model_tier": "small", "effort": "auto"})
    assert err.value.rule == "tier_not_supported"


@pytest.mark.parametrize(
    "key,value",
    [
        ("max_tier", "small"),
        ("default_tier", "small"),
        ("max_effort", "low"),
        ("default_effort", "low"),
        ("model_tiers", {"claude_code": {"small": "mapped-model"}}),
    ],
)
def test_ignored_policy_keys_warn_and_do_not_apply(create_store, key, value, caplog):
    config = create_store[4]
    if key == "model_tiers":
        config[key] = value
    else:
        config["ephemeral"][key] = value
    result = create(create_store)
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and key in warnings[0].message
    assert any(key in note for note in result["notes"])
    assert result["model_tier"] is None and result["effort"] is None


@pytest.mark.parametrize("value", [None, 0, 2, "1", True])
def test_max_depth_is_exactly_one_before_creator_check(create_store, value):
    service = create_store[0]
    create_store[4]["ephemeral"]["max_depth"] = value
    with pytest.raises(service.EphemeralPolicyError) as err:
        create(create_store, caller=None)
    assert err.value.rule == "policy_config_error:max_depth"


@pytest.mark.parametrize("caller", [None, "not-a-terminal", "deadbeef"])
def test_creator_required(create_store, caller):
    with pytest.raises(create_store[0].EphemeralPolicyError) as err:
        create(create_store, caller=caller)
    assert err.value.rule == "creator_unresolved"


@pytest.mark.parametrize(
    "allowed,requested,rule",
    [
        (["fs_read"], ["fs_read"], "tool_exceeds_creator"),
        (["@builtin", "@cao-mcp-server"], ["fs_read"], "tool_exceeds_creator"),
        (["@unknown", "@cao-mcp-server"], ["fs_list"], "tool_exceeds_creator"),
        (["@cao-mcp-server", "fs_*"], ["fs_write", "fs_read"], None),
        (["*"], ["web_fetch", "fs_read"], None),
    ],
)
def test_literal_creator_ceiling(create_store, allowed, requested, rule):
    service, factory, _, _, _ = create_store
    with factory() as db:
        db.get(database.TerminalModel, CALLER).allowed_tools = json.dumps(allowed)
        db.commit()
    if rule:
        with pytest.raises(service.EphemeralPolicyError) as err:
            create(create_store, {**SPEC, "tools": requested})
        assert err.value.rule == rule
    else:
        result = create(create_store, {**SPEC, "tools": requested})
        assert result["effective_tools"] == [
            atom for atom in service.TOOL_ATOMS if atom in requested
        ] + ["@cao-mcp-server"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("mcpServers", {}),
        ("native_agent", "actor"),
        ("model", "model"),
        ("purpose", "invalid space"),
        ("tools", ["*"]),
        ("effort", "extreme"),
        ("model_tier", "unknown"),
        ("description", "bad\ncontrol"),
    ],
)
def test_spec_rejects_extra_and_invalid_fields(field, value):
    from pydantic import ValidationError

    from cli_agent_orchestrator.models.ephemeral import EphemeralSpec

    with pytest.raises(ValidationError):
        EphemeralSpec.model_validate({**SPEC, field: value})


def test_hash_golden_and_normalization():
    from cli_agent_orchestrator.models.ephemeral import EphemeralSpec
    from cli_agent_orchestrator.services import ephemeral_service as service

    base = {
        "spec_version": 1,
        "purpose": "log_triage",
        "brief": "café\nline\tend",
        "tools": ["fs_read", "fs_list"],
    }
    value = EphemeralSpec.model_validate(base)
    payload = service.canonical_spec_bytes(value)
    expected = b'{"brief":"caf\xc3\xa9\\nline\\tend","description":null,"effort":null,"model_tier":null,"provider":null,"purpose":"log_triage","spec_version":1,"tools":["fs_read","fs_list"]}'
    assert payload == expected
    assert (
        hashlib.sha256(payload).hexdigest()
        == "7cb70e5af69e0329ea7805f12f1ce57198f7217af7d4497efb1a8c1ff05177b7"
    )
    changed = EphemeralSpec.model_validate(
        {**base, "brief": "café\r\nline\tend\x00", "tools": ["fs_list", "fs_read", "fs_list"]}
    )
    assert service.canonical_spec_bytes(changed) == payload
    assert (
        service.canonical_spec_bytes(
            EphemeralSpec.model_validate({**base, "brief": "café\nline\tend!"})
        )
        != payload
    )
    assert (
        service.canonical_spec_bytes(EphemeralSpec.model_validate({**base, "model_tier": "auto"}))
        != payload
    )


def test_band_names_and_installed_collision(create_store, monkeypatch):
    service, _, _, installed, _ = create_store
    assert len(service.BAND_NAMES) == 94
    assert len({name.lower() for name in service.BAND_NAMES}) == 94
    for band in service.BAND_NAMES:
        name = band + "-log_triage-3f9a"
        agent_profiles._validate_agent_name(name)
        assert agent_profiles.routes_to_ephemeral_store(name)
    (installed / "Ramones-log_triage-3f9a.md").write_text("occupied")
    values = iter(["3f9a", "aaaa"])
    monkeypatch.setattr(service.secrets, "token_hex", lambda _: next(values))
    assert create(create_store)["name"] == "Ramones-log_triage-aaaa"


@pytest.mark.parametrize("failed_step", [1, 2, 3])
def test_write_failure_cleans_exact_attempt_paths(create_store, monkeypatch, failed_step):
    service, factory, home, _, _ = create_store
    original = service._write_exclusive
    count = 0

    def fail_after(path, payload, owned):
        nonlocal count
        count += 1
        original(path, payload, owned)
        if count == failed_step:
            raise OSError("injected write failure")

    monkeypatch.setattr(service, "_write_exclusive", fail_after)
    with pytest.raises(service.EphemeralPolicyError) as err:
        create(create_store)
    assert err.value.status_code == 500
    assert not [p for p in (home / "ephemeral").rglob("*") if p.is_file()]
    with factory() as db:
        assert db.query(database.EphemeralAgentModel).count() == 0


@pytest.mark.parametrize("directory", ["ephemeral", "live", "audit", "session"])
def test_symlinked_store_refused(create_store, directory):
    service, _, home, _, _ = create_store
    outside = home.parent / "outside"
    outside.mkdir()
    root = home / "ephemeral"
    if directory == "ephemeral":
        link = root
    else:
        root.mkdir()
        if directory == "session":
            (root / "audit").mkdir()
            link = root / "audit" / "cao-session"
        else:
            link = root / directory
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(service.EphemeralPolicyError):
        create(create_store)
    assert not list(outside.iterdir())


def test_closed_atom_order_and_default_declared_hash(create_store):
    from cli_agent_orchestrator.models.ephemeral import EphemeralSpec

    service, factory, _, _, _ = create_store
    with factory() as db:
        db.get(database.TerminalModel, CALLER).allowed_tools = json.dumps(["*"])
        db.commit()
    all_atoms = ["web_fetch", "execute_bash", "fs_write", "fs_list", "fs_read"]
    spec = EphemeralSpec.model_validate({**SPEC, "tools": all_atoms})
    assert json.loads(service.canonical_spec_bytes(spec))["tools"] == list(service.TOOL_ATOMS)
    result = create(create_store, {**SPEC, "tools": all_atoms})
    assert result["effective_tools"] == list(service.TOOL_ATOMS) + ["@cao-mcp-server"]
    omitted = EphemeralSpec.model_validate({"purpose": "log_triage", "brief": "Inspect logs."})
    explicit = EphemeralSpec.model_validate({**omitted.model_dump(), "provider": "claude_code"})
    assert service.canonical_spec_bytes(omitted) != service.canonical_spec_bytes(explicit)
    assert json.loads(service.canonical_spec_bytes(omitted))["tools"] is None
    args = (result["name"], spec, "claude_code", result["effective_tools"], CALLER)
    assert service.render_profile(*args) == service.render_profile(*args)


@pytest.mark.parametrize("session", ["../escape", ".", "..", "a/b", "a\\b", "a\n"])
def test_session_is_one_safe_segment(create_store, session):
    service, factory, home, _, _ = create_store
    with factory() as db:
        db.get(database.TerminalModel, CALLER).tmux_session = session
        db.commit()
    with pytest.raises(service.EphemeralPolicyError, match="creator_unresolved"):
        create(create_store)
    assert not (home / "ephemeral").exists()


@pytest.mark.parametrize(
    "key,value",
    [
        ("allowed_providers", []),
        ("allowed_providers", None),
        ("allowed_providers", ["kiro_cli"]),
        ("max_brief_bytes", 0),
        ("pending_ttl_seconds", True),
        ("claim_lease_seconds", "60"),
    ],
)
def test_invalid_config_precedes_creator(create_store, key, value):
    service = create_store[0]
    create_store[4]["ephemeral"][key] = value
    with pytest.raises(service.EphemeralPolicyError) as err:
        create(create_store, caller=None)
    assert err.value.rule == "policy_config_error:" + key


@pytest.mark.parametrize(
    "creator_tools,spec_patch,config_patch,provider,depth,rule",
    [
        (["fs_read"], {"model_tier": "small"}, {}, "claude_code", True, "max_depth_exceeded"),
        (["fs_read"], {"model_tier": "small"}, {}, "claude_code", False, "tool_exceeds_creator"),
        (
            ["*"],
            {"model_tier": "small", "effort": "auto"},
            {},
            "kiro_cli",
            False,
            "tier_not_supported",
        ),
        (["*"], {"effort": "low"}, {}, "kiro_cli", False, "effort_not_supported"),
        (["*"], {}, {"allowed_providers": ["codex"]}, "kiro_cli", False, "provider_unsupported"),
        (
            ["@cao-mcp-server"],
            {"tools": ["execute_bash"], "brief": "AKIAIOSFODNN7EXAMPLE"},
            {"max_brief_bytes": 1},
            "claude_code",
            False,
            "tool_exceeds_creator",
        ),
        (
            ["*"],
            {"brief": "AKIAIOSFODNN7EXAMPLE"},
            {"max_brief_bytes": 1},
            "claude_code",
            False,
            "brief_too_large",
        ),
    ],
)
def test_fixed_policy_order(
    create_store, monkeypatch, creator_tools, spec_patch, config_patch, provider, depth, rule
):
    service, factory, home, _, config = create_store
    config["ephemeral"].update(config_patch)
    with factory() as db:
        row = db.get(database.TerminalModel, CALLER)
        row.allowed_tools = json.dumps(creator_tools)
        row.provider = provider
        db.commit()
    monkeypatch.setattr(database, "is_ephemeral_terminal", lambda _: depth)
    with pytest.raises(service.EphemeralPolicyError) as err:
        create(create_store, {**SPEC, **spec_patch})
    assert err.value.rule == rule
    assert not (home / "ephemeral").exists()


def test_unresolvable_creator_profile(create_store):
    service, factory, _, _, _ = create_store
    with factory() as db:
        row = db.get(database.TerminalModel, CALLER)
        row.allowed_tools = None
        row.agent_profile = "missing-profile"
        db.commit()
    with pytest.raises(service.EphemeralPolicyError, match="creator_unresolved"):
        create(create_store)


def test_description_secret_and_single_redacted_warning(create_store, caplog):
    service = create_store[0]
    secret = "AKIAIOSFODNN7EXAMPLE"
    with pytest.raises(service.EphemeralPolicyError) as err:
        create(create_store, {**SPEC, "description": secret})
    assert err.value.rule == "secret_detected"
    assert secret not in str(err.value) + caplog.text
    assert len([r for r in caplog.records if "ephemeral refusal" in r.message]) == 1


@pytest.mark.parametrize("suffix", [".json", ".spec.json", ".md"])
def test_exclusive_collision_preserves_existing_file(create_store, monkeypatch, suffix):
    service, factory, home, _, _ = create_store
    root = home / "ephemeral"
    (root / "live").mkdir(parents=True)
    (root / "audit" / "cao-session").mkdir(parents=True)
    name = "Ramones-log_triage-3f9a"
    occupied = (root / "audit" / "cao-session" if suffix == ".json" else root / "live") / (
        name + suffix
    )
    occupied.write_bytes(b"occupied")
    with pytest.raises(service.EphemeralPolicyError) as err:
        create(create_store)
    assert err.value.rule == "name_space_exhausted" and err.value.status_code == 409
    assert occupied.read_bytes() == b"occupied"
    assert [p for p in root.rglob("*") if p.is_file()] == [occupied]
    with factory() as db:
        assert db.query(database.EphemeralAgentModel).count() == 0


def test_database_failure_cleans_all_files(create_store, monkeypatch):
    from sqlalchemy.orm import Session

    service, factory, home, _, _ = create_store
    monkeypatch.setattr(Session, "commit", Mock(side_effect=OSError("injected commit failure")))
    with pytest.raises(service.EphemeralPolicyError) as err:
        create(create_store)
    assert err.value.status_code == 500
    assert not [p for p in (home / "ephemeral").rglob("*") if p.is_file()]
    with factory() as db:
        assert db.query(database.EphemeralAgentModel).count() == 0


def test_files_are_fsynced_in_order_before_insert(create_store, monkeypatch):
    service = create_store[0]
    from sqlalchemy.orm import Session

    writes, syncs = [], []
    original_write, original_commit = service._write_exclusive, Session.commit
    original_sync = service.os.fsync

    def sync(fd):
        syncs.append(fd)
        original_sync(fd)

    def write(path, payload, owned):
        assert len(syncs) == len(writes)
        original_write(path, payload, owned)
        writes.append(path.name)

    def commit(db):
        assert writes == [
            "Ramones-log_triage-3f9a.json",
            "Ramones-log_triage-3f9a.spec.json",
            "Ramones-log_triage-3f9a.md",
        ]
        assert len(syncs) == 3
        original_commit(db)

    monkeypatch.setattr(service.os, "fsync", sync)
    monkeypatch.setattr(service, "_write_exclusive", write)
    monkeypatch.setattr(Session, "commit", commit)
    create(create_store)


def test_primary_key_collision_retries_and_cleans(create_store, monkeypatch):
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import Session

    service = create_store[0]
    original = Session.commit
    calls = 0

    def commit(db):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise IntegrityError(
                "INSERT", {}, Exception("UNIQUE constraint failed: ephemeral_agents.name")
            )
        original(db)

    monkeypatch.setattr(Session, "commit", commit)
    values = iter(["3f9a", "aaaa"])
    monkeypatch.setattr(service.secrets, "token_hex", lambda _: next(values))
    assert create(create_store)["name"].endswith("-aaaa")
    assert calls == 2


@pytest.mark.parametrize("case", ["routing", "agreement", "installed_errors"])
def test_future_profile_source_contract(create_store, monkeypatch, case):
    import importlib

    target = Path(__file__).parents[2] / "src/cli_agent_orchestrator/decisions/targets.py"
    if not target.exists():
        pytest.skip("decisions/targets.py absent: #810 classification contract pending")
    targets = importlib.import_module("cli_agent_orchestrator.decisions.targets")
    service, _, home, installed, _ = create_store
    name = create(create_store)["name"]

    def source(value):
        answer = targets.profile_source(value)
        return getattr(answer, "value", answer).upper()

    if case == "routing":
        assert source(name) == "EPHEMERAL"
        (home / "ephemeral/live" / (name + ".md")).write_text("malformed")
        assert source(name) == "EPHEMERAL"
        (home / "ephemeral/live" / (name + ".md")).unlink()
        monkeypatch.setattr(Path, "read_text", Mock(side_effect=AssertionError("no file read")))
        assert source(name) == "EPHEMERAL"
        assert source(None) == source("reviewer") == "INSTALLED"
    elif case == "agreement":
        assert source(None) == "INSTALLED"
        for value in (name, "reviewer"):
            assert source(value) == agent_profiles.resolve_agent_profile_source(value).value.upper()
        monkeypatch.setattr(agent_profiles, "routes_to_ephemeral_store", lambda _: True)
        assert source("reviewer") == "EPHEMERAL"
        assert source(None) == "INSTALLED"
        with pytest.raises(agent_profiles.EphemeralProfileUnavailable):
            agent_profiles.load_launch_profile("reviewer")
    else:
        (installed / "malformed.md").write_text("not a valid profile")
        assert source("missing") == source("malformed") == "INSTALLED"
        # Decision mode defaults off. Errors must still belong to the create path.
        import asyncio

        from cli_agent_orchestrator.api import main
        from cli_agent_orchestrator.plugins.registry import PluginRegistry
        from cli_agent_orchestrator.services import terminal_service
        from cli_agent_orchestrator.utils import orchestration

        def fail_create(*args, **kwargs):
            raise ValueError("create path refused installed profile")

        monkeypatch.setenv("CAO_TERMINAL_ID", CALLER)
        monkeypatch.setattr(
            orchestration,
            "_resolve_handoff_provider",
            lambda _: orchestration.HandoffContext("claude_code", "cao-session", CALLER, None),
        )
        monkeypatch.setattr(terminal_service, "get_max_terminals", lambda: None)
        monkeypatch.setattr(terminal_service, "get_backend", lambda: Mock())
        monkeypatch.setattr(terminal_service, "generate_terminal_id", fail_create)
        monkeypatch.setattr(main.app.state, "plugin_registry", PluginRegistry(), raising=False)
        monkeypatch.setattr(agent_profiles, "resolve_env_vars", lambda text: text)
        for value in ("missing", "malformed"):
            with pytest.raises(ValueError) as create_error:
                asyncio.run(
                    terminal_service.create_terminal(
                        "claude_code", value, session_name="cao-session"
                    )
                )
            expected = str(create_error.value)
            monkeypatch.setattr(
                orchestration, "_create_terminal", Mock(side_effect=ValueError(expected))
            )
            assert expected in orchestration._assign_impl(value, "Inspect logs.")["message"]
            result = asyncio.run(orchestration._handoff_impl(value, "Inspect logs.", wait=False))
            assert not result.success and expected in result.message
            response = TestClient(main.app, base_url="http://localhost").post(
                "/terminals/run-step",
                json={
                    "provider": "claude_code",
                    "agent": value,
                    "prompt": "Inspect logs.",
                    "session_name": "cao-session",
                },
            )
            assert response.status_code == 404 and response.json()["detail"] == expected


def test_non_primary_integrity_failure_is_unexpected(create_store, monkeypatch):
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import Session

    service = create_store[0]
    monkeypatch.setattr(
        Session,
        "commit",
        Mock(
            side_effect=IntegrityError(
                "INSERT", {}, Exception("NOT NULL constraint failed: ephemeral_agents.provider")
            )
        ),
    )
    with pytest.raises(service.EphemeralPolicyError) as err:
        create(create_store)
    assert err.value.status_code == 500
    assert not [p for p in (create_store[2] / "ephemeral").rglob("*") if p.is_file()]


@pytest.mark.parametrize(
    "field,value",
    [
        ("spec_version", True),
        ("spec_version", "1"),
        ("description", "x" * 281),
        ("provider", "kiro_cli"),
        ("purpose", "valid_name\n"),
        ("tools", ["@cao-mcp-server"]),
    ],
)
def test_additional_shape_refusals(create_store, field, value):
    service = create_store[0]
    with pytest.raises(service.EphemeralPolicyError) as err:
        create(create_store, {**SPEC, field: value})
    assert err.value.rule == (
        "provider_unsupported" if field == "provider" and value == "kiro_cli" else "invalid_spec"
    )
    assert err.value.status_code == (400 if field == "provider" and value == "kiro_cli" else 422)
    assert not (create_store[2] / "ephemeral").exists()


def test_registry_collision_preserves_existing_attempt(create_store):
    service, _, home, _, _ = create_store
    result = create(create_store)
    snapshots = {p: p.read_bytes() for p in (home / "ephemeral").rglob("*") if p.is_file()}
    with pytest.raises(service.EphemeralPolicyError) as err:
        create(create_store)
    assert err.value.rule == "name_space_exhausted"
    assert {p: p.read_bytes() for p in snapshots} == snapshots
    assert database.get_ephemeral_agent(result["name"])["state"] == "pending"
    from cli_agent_orchestrator.services import terminal_service

    assert terminal_service.get_terminal(CALLER)["ephemeral"] is False
    assert database.get_terminal_metadata(CALLER)["ephemeral"] is False
    assert all(
        not terminal["ephemeral"] for terminal in database.list_terminals_by_session("cao-session")
    )


def test_creator_lookup_failure_precedes_depth(create_store, monkeypatch):
    service = create_store[0]
    monkeypatch.setattr(
        service, "caller_effective_allowed_tools", Mock(side_effect=OSError("unresolved"))
    )
    metadata = database.get_terminal_metadata(CALLER)
    monkeypatch.setattr(database, "get_terminal_metadata", lambda _: metadata)
    depth = Mock(side_effect=AssertionError("not yet"))
    monkeypatch.setattr(database, "is_ephemeral_terminal", depth)
    with pytest.raises(service.EphemeralPolicyError, match="creator_unresolved"):
        create(create_store)
    depth.assert_not_called()


def test_leaf_symlink_never_overwritten(create_store):
    service, _, home, _, _ = create_store
    live = home / "ephemeral/live"
    live.mkdir(parents=True)
    outside = home / "outside.md"
    outside.write_bytes(b"outside")
    (live / "Ramones-log_triage-3f9a.md").symlink_to(outside)
    with pytest.raises(service.EphemeralPolicyError, match="name_space_exhausted"):
        create(create_store)
    assert outside.read_bytes() == b"outside"
    assert not list(live.glob("*.spec.json"))


def test_note_build_failure_precedes_all_writes(create_store, monkeypatch):
    service, factory, home, _, _ = create_store
    monkeypatch.setattr(
        service, "_policy_notes", Mock(side_effect=RuntimeError("note builder failed"))
    )
    with pytest.raises(service.EphemeralPolicyError) as err:
        create(create_store)
    assert err.value.rule == "unexpected_failure"
    with factory() as db:
        assert db.query(database.EphemeralAgentModel).count() == 0
    assert not (home / "ephemeral").exists()


def test_success_warning_failure_cannot_fail_committed_create(create_store, monkeypatch):
    service = create_store[0]
    create_store[4]["ephemeral"]["max_tier"] = "small"
    monkeypatch.setattr(service.logger, "warning", Mock(side_effect=RuntimeError("warning failed")))
    result = create(create_store)
    assert database.get_ephemeral_agent(result["name"])["state"] == "pending"


@pytest.mark.parametrize(
    "block,top,model_note,effort_note,notice",
    [
        (
            {"max_tier": "private-tier"},
            {},
            "model: provider default (tier omitted; ephemeral.max_tier is not applied yet)",
            "effort: provider default (effort omitted, no default_effort)",
            "ephemeral.max_tier is not applied yet; the provider default is used",
        ),
        (
            {"max_effort": "private-effort"},
            {},
            "model: provider default (tier omitted, no default_tier)",
            "effort: provider default (effort omitted; ephemeral.max_effort is not applied yet)",
            "ephemeral.max_effort is not applied yet; the provider default is used",
        ),
        (
            {
                "max_tier": "private-tier",
                "default_tier": "private-tier",
                "max_effort": "private-effort",
                "default_effort": "private-effort",
            },
            {"model_tiers": {"claude_code": {"small": "private-model-id"}}},
            "model: provider default (tier omitted; ephemeral.max_tier, ephemeral.default_tier, model_tiers are not applied yet)",
            "effort: provider default (effort omitted; ephemeral.max_effort, ephemeral.default_effort are not applied yet)",
            "ephemeral.max_tier, ephemeral.default_tier, ephemeral.max_effort, ephemeral.default_effort, model_tiers are not applied yet; the provider default is used",
        ),
    ],
)
def test_ignored_notes_split_keys_without_values(
    create_store, caplog, block, top, model_note, effort_note, notice
):
    config = create_store[4]
    config["ephemeral"].update(block)
    config.update(top)
    result = create(create_store)
    assert result["notes"] == [
        model_note,
        effort_note,
        "launch: not supported by this server version",
    ]
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert warnings == ["ephemeral create " + result["name"] + ": " + notice]
    assert "private-tier" not in str(result) + caplog.text
    assert "private-effort" not in str(result) + caplog.text
    assert "private-model-id" not in str(result) + caplog.text


@pytest.mark.parametrize("declared", [True, False])
@pytest.mark.parametrize(
    "provider", [p.value for p in ProviderType if p.value not in {"claude_code", "codex"}]
)
def test_known_non_v1_provider_is_policy_refusal(create_store, declared, provider):
    service, factory, home, _, config = create_store
    config["ephemeral"]["allowed_providers"] = ["codex"]
    with factory() as db:
        db.get(database.TerminalModel, CALLER).provider = provider
        db.commit()
    spec = {**SPEC, "provider": provider} if declared else SPEC
    with pytest.raises(service.EphemeralPolicyError) as err:
        create(create_store, spec)
    assert err.value.rule == "provider_unsupported" and err.value.status_code == 400
    expected = provider + (
        "; supported providers: claude_code, codex" if declared else "; set provider explicitly"
    )
    assert err.value.detail == expected
    assert not (home / "ephemeral").exists()


def test_declared_non_v1_provider_obeys_fixed_check_order(create_store):
    service, factory, _, _, config = create_store
    spec = {**SPEC, "provider": "kiro_cli", "model_tier": "small"}
    with pytest.raises(service.EphemeralPolicyError) as err:
        create(create_store, spec)
    assert err.value.rule == "tier_not_supported"
    config["ephemeral"]["max_depth"] = 2
    with pytest.raises(service.EphemeralPolicyError) as err:
        create(create_store, spec)
    assert err.value.rule == "policy_config_error:max_depth"
    with pytest.raises(service.EphemeralPolicyError) as err:
        create(create_store, {**SPEC, "provider": "not-a-provider"})
    assert err.value.rule == "invalid_spec" and err.value.status_code == 422
