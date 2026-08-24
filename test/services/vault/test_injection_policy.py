"""U7 requester-identity policy for vault candidate construction."""

from __future__ import annotations

import ast
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from test.fixtures.vault_factory import build_vault_fixture
from types import SimpleNamespace
from unittest.mock import AsyncMock

import frontmatter
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel, TerminalModel
from cli_agent_orchestrator.models.memory import Memory
from cli_agent_orchestrator.services.memory_service import MemoryService
from cli_agent_orchestrator.services.vault.binding import (
    VaultBinding,
    collect_binding_warnings,
    resolve,
)
from cli_agent_orchestrator.services.vault.reader import (
    NO_REQUESTER_IDENTITY,
    VaultCandidate,
    VaultInjectionPolicy,
    load_candidate,
    resolve_candidates,
)
from cli_agent_orchestrator.services.vault.reconcile import reconcile

SOURCE_ROOT = Path(__file__).parents[3] / "src" / "cli_agent_orchestrator"
_CURATOR_SCOPE_FILTERED_MEMORY_TOOLS = {"memory_recall"}


def _indexed_non_injectable_binding(tmp_path, monkeypatch, *, mapping_folder="Projects/CAO Design"):
    from cli_agent_orchestrator.services.vault import reader
    from cli_agent_orchestrator.services.vault import reconcile as reconcile_module

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(reader, "SessionLocal", Session)
    monkeypatch.setattr(reconcile_module, "SessionLocal", Session)
    monkeypatch.setattr(reconcile_module, "_replace_vault_edges", lambda _notes: None)
    monkeypatch.setattr(reconcile_module, "_emit_audit_events", lambda *_args: None)

    fixture = build_vault_fixture(tmp_path)
    reconcile(
        fixture.vault,
        apply=True,
        run_id="injection-policy",
        run_started_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    mapping = next(item for item in fixture.vault.mappings if item.folder == mapping_folder)
    return Session, VaultBinding(
        scope=mapping.scope,
        scope_id=mapping.scope_id,
        vault_id=fixture.vault.id,
        root=fixture.vault.root,
        mapping=mapping,
    )


def test_memory_manager_cannot_waive_injectable_gate_with_false(tmp_path, monkeypatch, caplog):
    """A curator's explicit False cannot override resolver-owned policy."""
    caplog.set_level(logging.INFO, logger="cli_agent_orchestrator.services.vault.reader")
    Session, binding = _indexed_non_injectable_binding(tmp_path, monkeypatch)
    terminal_id = "manager01"
    with Session() as db:
        db.add(
            TerminalModel(
                id=terminal_id,
                tmux_session="policy",
                tmux_window="manager",
                provider="claude_code",
                agent_profile="memory_manager",
            )
        )
        db.commit()
    monkeypatch.setenv("CAO_TERMINAL_ID", "missing-terminal")

    candidates = resolve_candidates(
        binding,
        scope="project",
        scope_id="fixture-project",
        require_injectable=False,
        terminal_id=terminal_id,
        consumer="explicit_recall",
    )

    injectable_binding = VaultBinding(
        scope=binding.scope,
        scope_id=binding.scope_id,
        vault_id=binding.vault_id,
        root=binding.root,
        mapping=binding.mapping.model_copy(update={"inject": True}),
    )
    proof_candidates = resolve_candidates(
        injectable_binding,
        scope="project",
        scope_id="fixture-project",
        require_injectable=False,
        terminal_id=terminal_id,
        consumer="explicit_recall",
    )

    assert proof_candidates
    assert {candidate.policy_arm for candidate in proof_candidates} == {"memory_manager"}
    assert all(candidate.require_injectable for candidate in proof_candidates)
    assert candidates == []


def test_explicit_requester_identity_overrides_the_mcp_environment(tmp_path, monkeypatch, caplog):
    """Server-side callers identify the requester without inheriting its pane env."""
    Session, binding = _indexed_non_injectable_binding(tmp_path, monkeypatch)
    terminal_id = "worker01"
    with Session() as db:
        db.add(
            TerminalModel(
                id=terminal_id,
                tmux_session="policy",
                tmux_window="worker",
                provider="claude_code",
                agent_profile="developer",
            )
        )
        db.commit()
    monkeypatch.setenv("CAO_TERMINAL_ID", "missing-terminal")

    candidates = resolve_candidates(
        binding,
        scope="project",
        scope_id="fixture-project",
        require_injectable=False,
        terminal_id=terminal_id,
        consumer="explicit_recall",
    )

    assert candidates
    assert {candidate.policy_arm for candidate in candidates} == {"caller"}
    assert not any(candidate.require_injectable for candidate in candidates)
    assert "identity_mismatch" in caplog.text
    assert "policy_arm=caller" in caplog.text


def test_no_terminal_honors_caller_policy_and_announces_arm(tmp_path, monkeypatch, caplog):
    """An ordinary CLI recall has no injected-context consumer to gate."""
    caplog.set_level(logging.DEBUG, logger="cli_agent_orchestrator.services.vault.reader")
    _Session, binding = _indexed_non_injectable_binding(tmp_path, monkeypatch)
    # This arm must not inherit test/conftest.py's unrelated hermetic env fixture.
    monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)

    candidates = resolve_candidates(
        binding,
        scope="project",
        scope_id="fixture-project",
        require_injectable=False,
        terminal_id=None,
        consumer="explicit_recall",
    )

    assert candidates
    assert {candidate.policy_arm for candidate in candidates} == {"no_terminal"}
    assert not any(candidate.require_injectable for candidate in candidates)


def test_unknown_consumer_fails_closed(tmp_path, monkeypatch):
    """A runtime typo cannot relax the injection floor behind a Literal annotation."""
    _Session, binding = _indexed_non_injectable_binding(tmp_path, monkeypatch)
    monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)

    result = resolve_candidates(
        binding,
        scope="project",
        scope_id="fixture-project",
        require_injectable=False,
        terminal_id=None,
        consumer="injected-context",
    )

    assert result == []
    assert result.policy_arm == "no_terminal"
    assert result.exit_arm == "not_injectable"


def test_unresolvable_requester_fails_closed_and_announces_policy(tmp_path, monkeypatch, caplog):
    """An asserted but unresolvable terminal identity defaults deny."""
    caplog.set_level(logging.INFO, logger="cli_agent_orchestrator.services.vault.reader")
    _Session, binding = _indexed_non_injectable_binding(tmp_path, monkeypatch)
    monkeypatch.setenv("CAO_TERMINAL_ID", "missing-terminal")

    candidates = resolve_candidates(
        binding,
        scope="project",
        scope_id="fixture-project",
        require_injectable=False,
        terminal_id=None,
        consumer="explicit_recall",
    )

    assert candidates.policy_arm == "unresolved"
    assert candidates.exit_arm == "not_injectable"
    assert candidates == []
    assert "identity_source=ambient" in caplog.text
    assert "policy_arm=unresolved" in caplog.text


def test_memory_manager_policy_flip_excludes_mapping_on_next_resolve(tmp_path, monkeypatch, caplog):
    """A later curator recall cannot retrieve a mapping after inject flips off."""
    caplog.set_level(logging.INFO, logger="cli_agent_orchestrator.services.vault.reader")
    Session, binding = _indexed_non_injectable_binding(tmp_path, monkeypatch)
    terminal_id = "manager-flip"
    with Session() as db:
        db.add(
            TerminalModel(
                id=terminal_id,
                tmux_session="policy",
                tmux_window="manager",
                provider="claude_code",
                agent_profile="memory_manager",
            )
        )
        db.commit()
    monkeypatch.setenv("CAO_TERMINAL_ID", terminal_id)
    injectable_binding = VaultBinding(
        scope=binding.scope,
        scope_id=binding.scope_id,
        vault_id=binding.vault_id,
        root=binding.root,
        mapping=binding.mapping.model_copy(update={"inject": True}),
    )

    first_dispatch = resolve_candidates(
        injectable_binding,
        scope="project",
        scope_id="fixture-project",
        require_injectable=False,
        terminal_id=None,
        consumer="explicit_recall",
    )
    second_dispatch = resolve_candidates(
        binding,
        scope="project",
        scope_id="fixture-project",
        require_injectable=False,
        terminal_id=None,
        consumer="explicit_recall",
    )

    assert first_dispatch
    assert all(candidate.require_injectable for candidate in first_dispatch)
    assert first_dispatch.policy_arm == "memory_manager"
    assert second_dispatch.policy_arm == "memory_manager"
    assert second_dispatch.exit_arm == "not_injectable"
    assert second_dispatch == []


def test_deterministic_build_resolves_policy_once_across_all_scope_bindings(
    tmp_path, monkeypatch
) -> None:
    """One injected build cannot mix policy arms after an intermittent lookup failure."""
    from test.services.vault.test_vault_injection_renderer import _injectable_renderer

    from cli_agent_orchestrator.services import memory_service

    service, _session_factory, _vault_root, _config = _injectable_renderer(tmp_path, monkeypatch)
    real_resolve_policy = memory_service._resolve_injection_policy
    resolutions: list[tuple[bool, str, str | None]] = []

    def record_policy(require_injectable, *, consumer, terminal_id):
        resolutions.append((require_injectable, consumer, terminal_id))
        return real_resolve_policy(
            require_injectable,
            consumer=consumer,
            terminal_id=terminal_id,
        )

    monkeypatch.setattr(memory_service, "_resolve_injection_policy", record_policy)

    service.get_memory_context_for_terminal("worker")

    assert resolutions == [(True, "injected_context", "worker")]


def test_vault_candidate_batch_retains_each_binding_exit_arm(tmp_path, monkeypatch) -> None:
    """Flattening candidates must not collapse a gate refusal into an empty scope."""
    _Session, non_injectable = _indexed_non_injectable_binding(tmp_path, monkeypatch)
    monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
    injectable = VaultBinding(
        scope=non_injectable.scope,
        scope_id=non_injectable.scope_id,
        vault_id=non_injectable.vault_id,
        root=non_injectable.root,
        mapping=non_injectable.mapping.model_copy(update={"inject": True}),
    )
    service = MemoryService(base_dir=tmp_path / "native")

    batch = service._vault_candidates(
        [non_injectable, injectable],
        require_injectable=True,
        terminal_id=None,
        consumer="injected_context",
    )

    assert [resolution.policy_arm for resolution in batch.resolutions] == [
        "no_terminal",
        "no_terminal",
    ]
    assert [resolution.exit_arm for resolution in batch.resolutions] == [
        "not_injectable",
        "candidates",
    ]
    assert batch.candidates


def test_bm25_keeps_identity_free_vault_corpus_but_gates_curator_results(monkeypatch):
    """The synthetic corpus policy cannot become the policy that returns rows."""
    from cli_agent_orchestrator.services import memory_service

    now = datetime.now(timezone.utc)
    candidates = (
        VaultCandidate(
            binding=SimpleNamespace(inject=True),
            metadata=SimpleNamespace(key="allowed", memory_type="reference"),
            note=SimpleNamespace(),
            require_injectable=False,
            policy_arm="bm25_corpus",
        ),
        VaultCandidate(
            binding=SimpleNamespace(inject=False),
            metadata=SimpleNamespace(key="hidden", memory_type="reference"),
            note=SimpleNamespace(),
            require_injectable=False,
            policy_arm="bm25_corpus",
        ),
    )
    memories = {
        "allowed": Memory(
            id="allowed",
            key="allowed",
            memory_type="reference",
            scope="project",
            scope_id="fixture-project",
            file_path="allowed.md",
            content="needle allowed",
            created_at=now,
            updated_at=now,
        ),
        "hidden": Memory(
            id="hidden",
            key="hidden",
            memory_type="reference",
            scope="project",
            scope_id="fixture-project",
            file_path="hidden.md",
            content="needle hidden",
            created_at=now,
            updated_at=now,
        ),
    }
    corpora: list[list[list[str]]] = []

    class RecordingBm25:
        def __init__(self, corpus):
            corpora.append(corpus)

        def get_scores(self, _query):
            return [1.0] * len(corpora[-1])

    monkeypatch.setattr(
        memory_service,
        "load_candidate",
        lambda candidate, **_kwargs: memories[candidate.metadata.key],
    )
    monkeypatch.setitem(sys.modules, "rank_bm25", SimpleNamespace(BM25Okapi=RecordingBm25))
    service = MemoryService()
    corpus_policies: list[VaultInjectionPolicy] = []
    monkeypatch.setattr(
        service,
        "_resolve_sources",
        lambda *_args, **_kwargs: ([], ["project-binding"], 4096),
    )
    monkeypatch.setattr(
        service,
        "_vault_candidates",
        lambda *_args, policy, **_kwargs: (corpus_policies.append(policy) or candidates),
    )
    common = dict(
        query="needle",
        scope="project",
        scope_id="fixture-project",
        memory_type=None,
        limit=10,
        exclude_keys=set(),
        terminal_context=None,
        scan_all=False,
    )

    ordinary = service._bm25_search(**common, policy=VaultInjectionPolicy(False, "caller", False))
    curator = service._bm25_search(
        **common, policy=VaultInjectionPolicy(True, "memory_manager", True)
    )

    assert corpus_policies == [memory_service._BM25_CORPUS_POLICY] * 2
    assert corpora == [[["needle", "allowed"], ["needle", "hidden"]]] * 2
    assert [memory.key for memory in ordinary] == ["allowed", "hidden"]
    assert [memory.key for memory in curator] == ["allowed"]


def test_related_expansion_cannot_cross_injection_policy_because_it_shares_primary_binding(
    tmp_path, monkeypatch
):
    """A cross-scope/per-note related model must make this invariant red before it can bypass policy."""
    from test.services.vault.test_vault_injection_renderer import _injectable_renderer

    from cli_agent_orchestrator.services import memory_service

    service, session_factory, vault_root, config = _injectable_renderer(tmp_path, monkeypatch)
    (vault_root / "Projects" / "CAO Design" / "Related.md").write_text(
        "---\ncao:\n  key: related\n---\nRelated vault body.\n",
        encoding="utf-8",
    )
    reconcile(
        config.vaults[0],
        apply=True,
        run_id="related-binding-invariant",
        run_started_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
    )
    with session_factory() as db:
        db.query(MemoryMetadataModel).filter(MemoryMetadataModel.key == "related").update(
            {"updated_at": datetime(2024, 1, 1, tzinfo=timezone.utc)}
        )
        db.commit()
    resolved_scopes: list[tuple[str, str | None]] = []
    real_load_related = service._load_related_vault_memory

    def record_related_resolution(
        key,
        scope,
        scope_id,
        *,
        require_injectable,
        terminal_id=None,
        consumer,
        policy=None,
    ):
        resolved_scopes.append((scope, scope_id))
        return real_load_related(
            key,
            scope,
            scope_id,
            require_injectable=require_injectable,
            terminal_id=terminal_id,
            consumer=consumer,
            policy=policy,
        )

    monkeypatch.setattr(
        service,
        "_related_keys_lookup",
        lambda _keys, _scope, _scope_id, *, source_kind="native": (
            {"design": "related"} if source_kind == "vault" else {}
        ),
    )
    monkeypatch.setattr(memory_service, "MEMORY_MAX_PER_SCOPE", 1)
    monkeypatch.setattr(service, "_load_related_vault_memory", record_related_resolution)

    block = service.get_memory_context_for_terminal("worker")

    assert resolved_scopes == [("project", "fixture-project")]
    assert "- [project] design: Design" in block
    assert "- [project] related [related]: Related vault body." in block


def test_candidate_level_injection_gate_refuses_non_injectable_binding(
    tmp_path, monkeypatch
) -> None:
    """The byte boundary refuses a directly constructed effective-gated candidate."""
    _Session, binding = _indexed_non_injectable_binding(tmp_path, monkeypatch)
    monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
    resolved = resolve_candidates(
        binding,
        keys=["design"],
        scope="project",
        scope_id="fixture-project",
        require_injectable=False,
        terminal_id=None,
        consumer="explicit_recall",
    )[0]
    candidate = VaultCandidate(
        binding=binding,
        metadata=resolved.metadata,
        note=resolved.note,
        require_injectable=True,
        policy_arm="test",
    )

    assert load_candidate(candidate, max_body_chars=4096, require_injectable=True) is None


def test_agent_scoped_mapping_is_not_recallable_without_a_confirmed_non_curator(
    tmp_path, monkeypatch
):
    """Unknown requesters cannot receive agent-scoped vault candidates."""
    from test.services.vault.test_vault_injection_renderer import _injectable_renderer

    service, _session_factory, _vault_root, config = _injectable_renderer(tmp_path, monkeypatch)
    monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
    binding = resolve("agent", "fixture-agent", vault_config=config)

    assert isinstance(binding, VaultBinding)
    candidates = resolve_candidates(
        binding,
        scope="agent",
        scope_id="fixture-agent",
        require_injectable=False,
        terminal_id=None,
        consumer="explicit_recall",
    )
    warning = next(
        warning
        for warning in collect_binding_warnings(config)
        if warning.kind == "agent_scope_recall_only" and warning.mapping == "Injectable"
    )
    block = service.get_memory_context_for_terminal("worker")

    assert candidates == []
    assert candidates.policy_arm == "no_terminal"
    assert candidates.exit_arm == "curator_agent_scope_refused"
    assert "Handbook" not in block
    assert warning.detail == (
        "agent-scoped mapping 'Injectable' is recall-only and is not injected in this release"
    )


def test_identityless_server_projection_suppresses_ambient_terminal_identity(tmp_path, monkeypatch):
    """The graph sentinel must not inherit a contradictory process terminal."""
    from test.services.vault.test_vault_injection_renderer import _injectable_renderer

    _service, _session_factory, _vault_root, config = _injectable_renderer(tmp_path, monkeypatch)
    binding = resolve("agent", "fixture-agent", vault_config=config)
    assert isinstance(binding, VaultBinding)
    monkeypatch.setenv("CAO_TERMINAL_ID", "missing-terminal")

    result = resolve_candidates(
        binding,
        scope="agent",
        scope_id="fixture-agent",
        require_injectable=False,
        terminal_id=NO_REQUESTER_IDENTITY,
        consumer="explicit_recall",
    )

    assert result == []
    assert result.policy_arm == "no_terminal"
    assert result.exit_arm == "curator_agent_scope_refused"


def test_curator_refuses_indexed_agent_mapping_before_it_reaches_worker_context(
    tmp_path, monkeypatch
):
    """Curator identity, not an empty vault, refuses agent-scoped candidates."""
    Session, binding = _indexed_non_injectable_binding(
        tmp_path, monkeypatch, mapping_folder="Injectable"
    )
    manager_id = "manager-agent-scope"
    developer_id = "developer-agent-scope"
    with Session() as db:
        db.add_all(
            [
                TerminalModel(
                    id=manager_id,
                    tmux_session="policy",
                    tmux_window="manager",
                    provider="claude_code",
                    agent_profile="memory_manager",
                ),
                TerminalModel(
                    id=developer_id,
                    tmux_session="policy",
                    tmux_window="developer",
                    provider="claude_code",
                    agent_profile="developer",
                ),
            ]
        )
        db.commit()

    developer_result = resolve_candidates(
        binding,
        scope=binding.scope,
        scope_id=binding.scope_id,
        require_injectable=False,
        terminal_id=developer_id,
        consumer="explicit_recall",
    )
    curator_result = resolve_candidates(
        binding,
        scope=binding.scope,
        scope_id=binding.scope_id,
        require_injectable=False,
        terminal_id=manager_id,
        consumer="explicit_recall",
    )
    monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
    no_terminal_result = resolve_candidates(
        binding,
        scope=binding.scope,
        scope_id=binding.scope_id,
        require_injectable=False,
        terminal_id=None,
        consumer="explicit_recall",
    )
    monkeypatch.setenv("CAO_TERMINAL_ID", "missing-terminal")
    unresolved_result = resolve_candidates(
        binding,
        scope=binding.scope,
        scope_id=binding.scope_id,
        require_injectable=False,
        terminal_id=None,
        consumer="explicit_recall",
    )

    assert [candidate.metadata.file_path for candidate in developer_result] == [
        "Injectable/Team Handbook.md"
    ]
    assert developer_result.policy_arm == "caller"
    assert developer_result.exit_arm == "candidates"
    assert curator_result == []
    assert curator_result.policy_arm == "memory_manager"
    assert curator_result.exit_arm == "curator_agent_scope_refused"
    assert no_terminal_result == []
    assert no_terminal_result.policy_arm == "no_terminal"
    assert no_terminal_result.exit_arm == "curator_agent_scope_refused"
    assert unresolved_result == []
    assert unresolved_result.policy_arm == "unresolved"
    assert unresolved_result.exit_arm == "curator_agent_scope_refused"


def test_curator_recall_never_returns_its_own_agent_scoped_memory(monkeypatch):
    """Curated output follows the builder's injectable scopes, never agent scope."""
    from cli_agent_orchestrator.mcp_server import server
    from cli_agent_orchestrator.services import memory_service, settings_service

    now = datetime.now(timezone.utc)
    curator_agent_memory = Memory(
        id="agent-note",
        key="curator-note",
        memory_type="reference",
        scope="agent",
        scope_id="memory_manager",
        file_path="agent.md",
        content="curator operational note",
        created_at=now,
        updated_at=now,
    )
    project_memory = Memory(
        id="project-note",
        key="project-note",
        memory_type="reference",
        scope="project",
        scope_id="fixture-project",
        file_path="project.md",
        content="injectable project note",
        created_at=now,
        updated_at=now,
    )
    recall = AsyncMock(return_value=[curator_agent_memory, project_memory])
    service = type("CuratorRecallService", (), {"recall": recall})()
    terminal_context = {
        "terminal_id": "curator",
        "agent_profile": "memory_manager",
    }
    monkeypatch.setattr(settings_service, "is_memory_enabled", lambda: True)
    monkeypatch.setattr(memory_service, "MemoryService", lambda: service)
    monkeypatch.setattr(server, "_get_terminal_context_from_env", lambda: terminal_context)

    result = asyncio.run(server.memory_recall(query="note", scope=None))

    assert recall.await_count == 1
    assert recall.await_args.kwargs["terminal_context"] == terminal_context
    assert [memory.scope for memory in [curator_agent_memory, project_memory]] == [
        "agent",
        "project",
    ]
    assert [memory["key"] for memory in result["memories"]] == ["project-note"]


def test_memory_manager_profile_tools_are_all_scope_filtered():
    """A new curator memory tool must implement the agent-scope injection bound."""
    profile = frontmatter.load(SOURCE_ROOT / "agent_store" / "memory_manager.md")

    curator_tools = set(profile.metadata["tools"])

    assert curator_tools <= _CURATOR_SCOPE_FILTERED_MEMORY_TOOLS


def test_injection_builder_reachable_vault_calls_do_not_hardcode_policy_false():
    """The renderer's effective policy reaches both related-vault checks."""
    path = SOURCE_ROOT / "services" / "memory_service.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    relevant_functions = {
        "get_memory_context_for_terminal",
        "_vault_candidates",
        "_load_related_vault_memory",
    }
    calls: list[ast.Call] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in relevant_functions:
            continue
        for descendant in ast.walk(node):
            if not isinstance(descendant, ast.Call):
                continue
            if isinstance(descendant.func, ast.Name):
                name = descendant.func.id
            elif isinstance(descendant.func, ast.Attribute):
                name = descendant.func.attr
            else:
                continue
            if name in {
                "_vault_candidates",
                "_load_related_vault_memory",
                "resolve_candidates",
                "load_candidate",
            }:
                calls.append(descendant)

    assert calls, "policy-call matcher must find builder-reachable vault calls"
    policy_arguments = [
        keyword.value
        for call in calls
        for keyword in call.keywords
        if keyword.arg == "require_injectable"
    ]
    assert policy_arguments, "builder-reachable vault calls must carry the policy explicitly"
    assert not any(
        isinstance(value, ast.Constant) and value.value is False for value in policy_arguments
    )
