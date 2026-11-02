"""Tests for freezing a script-tier run's execution manifest (issue #583 Bolt 2, ``manifest-freeze``).

Two of these carry the unit's load:

* ``test_a_secret_in_inputs_does_not_reach_the_manifest`` is the concrete reason
  ``execution_manifest.build`` is the only door. ``inputs`` is arbitrary user JSON and the most likely
  place a credential appears in a workflow run.
* ``test_changing_the_source_hash_changes_the_plan_id`` converts this unit's transitive-coverage
  ARGUMENT into a PROPERTY. Six of FR-8's nine manifest fields are omitted because they have no
  run-level source in this tier; the omission is only safe because the script's own source chooses
  them, so a source change must move the identifier. If this test fails, the omission is unsafe and a
  changed provider could execute under a stale approval.
"""

import json
import logging
import subprocess

import pytest

from cli_agent_orchestrator.services import (
    approval_gate,
    approval_store,
    execution_manifest,
    manifest_freeze,
    settings_service,
)
from cli_agent_orchestrator.utils import agent_profiles

SECRET = "AKIAIOSFODNN7EXAMPLE"  # matches secret_gate's ``aws_access_key`` pattern

OMITTED = ("provider", "model", "profile", "permissions", "limits", "retry_policy")


def _frozen(**overrides) -> dict:
    kwargs = {"source_hash": "abc123", "inputs": {"k": "v"}}
    kwargs.update(overrides)
    value = manifest_freeze.build_manifest_json(**kwargs)
    assert value is not None
    return json.loads(value)


# ---------------------------------------------------------------------------
# The two load-bearing properties
# ---------------------------------------------------------------------------


def test_a_secret_in_inputs_does_not_reach_the_manifest():
    """Everything reaching the column passes through ``build``'s tree redaction."""
    value = manifest_freeze.build_manifest_json(source_hash="h", inputs={"tok": SECRET})
    assert value is not None
    assert SECRET not in value
    assert json.loads(value)["redacted"] is True


def test_changing_the_source_hash_changes_the_plan_id():
    """The transitive-coverage property. See the module docstring.

    The six omitted fields are chosen by the script's own source, so a source change must move the
    identifier. This is what makes omitting them safe rather than merely convenient.
    """
    a = _frozen(source_hash="hash-of-script-v1")["plan_id"]
    b = _frozen(source_hash="hash-of-script-v2")["plan_id"]
    assert a != b


# ---------------------------------------------------------------------------
# What is recorded, and what is not
# ---------------------------------------------------------------------------


def test_the_six_absent_fields_are_omitted_not_null():
    """A null would assert "exists but empty"; these fields have no run-level existence in this tier."""
    document = _frozen()
    for name in OMITTED:
        assert name not in document, f"{name} should be omitted, not present as null"


def test_the_note_explaining_the_omission_travels_in_the_envelope():
    """The envelope is what an agent reads when diagnosing a failed run, so the reason lives there."""
    document = _frozen()
    assert "notes" in document
    for name in OMITTED:
        assert name in document["notes"]
    assert "source_hash" in document["notes"]


def test_the_three_recorded_fields_are_present():
    document = _frozen()
    assert document["source_hash"] == "abc123"
    assert document["inputs"] == {"k": "v"}
    assert "repo_baseline" in document
    assert document["plan_id"].startswith("plan-v2:")


def test_the_memory_record_is_empty_at_freeze_time():
    """There is no resolved memory at run start; ``memory-resolve-once`` (pass 2B) fills it."""
    memory = _frozen()["memory"]
    assert memory["content"] == ""
    assert memory["truncated"] is False


def test_the_frozen_value_round_trips_through_parse():
    value = manifest_freeze.build_manifest_json(source_hash="h", inputs={"a": [1, 2]})
    assert value is not None
    assert execution_manifest.parse(value) is not None


# ---------------------------------------------------------------------------
# Totality and identity stability
# ---------------------------------------------------------------------------


def test_returns_none_and_logs_rather_than_raising(monkeypatch, caplog):
    """Best-effort: the run must not fail because its manifest could not be assembled.

    A ``None`` writes NULL, which the approval gate reads as "never approved" and REFUSES — so the
    failure direction is fail-closed, never an unapproved run that executes.
    """

    def _boom(*_args, **_kwargs):
        raise RuntimeError("assembly exploded")

    monkeypatch.setattr(manifest_freeze.plan_identifier, "compute", _boom)
    with caplog.at_level("WARNING"):
        assert manifest_freeze.build_manifest_json(source_hash="h", inputs={}) is None
    assert any("manifest freeze failed" in r.message for r in caplog.records)


def test_the_plan_id_does_not_depend_on_the_working_directory(monkeypatch, tmp_path):
    """Path-independence: two machines running an identical plan must agree.

    The baseline deliberately excludes the worktree path for exactly this reason, so a differing cwd
    with the same repository state cannot move the identifier.
    """
    monkeypatch.setattr(
        manifest_freeze,
        "derive_baseline",
        lambda _cwd, **_kw: {"available": True, "commit": "c", "dirty": False},
    )
    a = _frozen(cwd=str(tmp_path))["plan_id"]
    b = _frozen(cwd="/some/other/path")["plan_id"]
    assert a == b


def test_a_missing_baseline_still_freezes():
    """An unavailable baseline has no identifier that could be approved or replayed."""
    assert (
        manifest_freeze.build_manifest_json(
            source_hash="abc123", inputs={"k": "v"}, cwd="/nonexistent/path/for/test"
        )
        is None
    )


def test_an_unavailable_worktree_snapshot_has_no_approvable_plan_id(monkeypatch):
    monkeypatch.setattr(
        manifest_freeze,
        "derive_baseline",
        lambda _cwd, **_kw: {
            "available": False,
            "commit": "deadbeef",
            "worktree_state": {"status": "unavailable"},
        },
    )

    assert manifest_freeze.build_manifest_json(source_hash="abc123", inputs={"k": "v"}) is None


def test_an_unavailable_snapshot_reaches_the_gate_without_a_plan_id(monkeypatch):
    available = {
        "available": True,
        "commit": "deadbeef",
        "worktree_state": {"status": "dirty", "digest": "sha256:first"},
    }
    unavailable = {
        "available": False,
        "commit": "deadbeef",
        "worktree_state": {"status": "unavailable"},
    }
    baselines = iter((available, unavailable))
    monkeypatch.setattr(manifest_freeze, "derive_baseline", lambda _cwd, **_kw: next(baselines))
    monkeypatch.setattr(approval_gate, "is_workflow_approval_required", lambda: True)

    approved_manifest = manifest_freeze.build_manifest_json(source_hash="abc123", inputs={"k": "v"})
    assert approved_manifest is not None
    approved_plan_id = json.loads(approved_manifest)["plan_id"]
    monkeypatch.setattr(
        approval_store,
        "approval_state",
        lambda plan_id: (
            approval_store.APPROVED if plan_id == approved_plan_id else approval_store.ABSENT
        ),
    )
    approval_gate.ensure_plan_approved(tier="script", manifest_json=approved_manifest)

    unavailable_manifest = manifest_freeze.build_manifest_json(
        source_hash="abc123", inputs={"k": "v"}
    )
    with pytest.raises(approval_gate.PlanApprovalRequiredError) as excinfo:
        approval_gate.ensure_plan_approved(tier="script", manifest_json=unavailable_manifest)

    assert excinfo.value.plan_id is None


def test_an_available_baseline_is_part_of_the_identity_and_unavailable_baselines_are_not(
    monkeypatch,
):
    """Two available commits are distinct plans; an unavailable snapshot has no plan."""
    monkeypatch.setattr(
        manifest_freeze,
        "derive_baseline",
        lambda _cwd, **_kw: {
            "available": True,
            "commit": "first",
            "worktree_state": {"status": "clean"},
        },
    )
    a = _frozen()["plan_id"]
    monkeypatch.setattr(
        manifest_freeze,
        "derive_baseline",
        lambda _cwd, **_kw: {
            "available": True,
            "commit": "second",
            "worktree_state": {"status": "clean"},
        },
    )
    b = _frozen()["plan_id"]
    assert a != b
    monkeypatch.setattr(
        manifest_freeze,
        "derive_baseline",
        lambda _cwd, **_kw: {
            "available": False,
            "commit": "second",
            "worktree_state": {"status": "unavailable"},
        },
    )
    assert manifest_freeze.build_manifest_json(source_hash="abc123", inputs={"k": "v"}) is None


def test_non_git_plan_ids_bind_root_and_git_golden_is_unchanged(tmp_path, monkeypatch):
    """The golden value is pinned under an empty profile store (see ``_store`` below).

    Without pinning ``usable_agent_dirs``/extra/disabled dirs and the local store, this golden hash
    would also depend on whatever agent profiles happen to be installed on the machine running the
    test — the same leakage ``_store`` exists to prevent for the profile-set-digest tests below.
    """
    _store(tmp_path, monkeypatch)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    sub = a / "sub"
    sub.mkdir()
    ids = [_frozen(cwd=str(root))["plan_id"] for root in [a, b, sub, "/"]]
    assert len(set(ids)) == 4
    assert _frozen(cwd=str(a))["plan_id"] == ids[0]
    monkeypatch.setattr(
        manifest_freeze,
        "derive_baseline",
        lambda root, **_kw: {
            "available": True,
            "commit": "0123456789abcdef0123456789abcdef01234567",
            "worktree_state": {"status": "clean"},
        },
    )
    assert (
        _frozen()["plan_id"]
        == "plan-v2:c89991b472d5f8aed662a8677d00770f3ae19224d17c95112f3863545f248d38"
    )


# ---------------------------------------------------------------------------
# The freeze's ONE root stat reaches the guard for EITHER root kind, while only a
# non-Git root's pair reaches plan_id (fanhongy's ask 2 on PR #1051, pins 1 and 2)
# ---------------------------------------------------------------------------


def _git_repository(path, *, content: str):
    """A real one-commit repository at ``path``, so the Git branch of the baseline is exercised.

    A patched ``derive_baseline`` cannot stand in here: the whole question is what the REAL derivation
    puts in, and leaves out of, the baseline for a directory that is genuinely inside Git.
    """
    path.mkdir(parents=True, exist_ok=True)
    for args in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@example.com"],
        ["git", "config", "user.name", "T"],
    ):
        subprocess.run(args, cwd=path, check=True, capture_output=True)
    (path / "f.txt").write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True, capture_output=True)
    return path


def test_a_git_roots_baseline_carries_no_root_identity_so_git_plan_ids_do_not_move(
    tmp_path, monkeypatch
):
    """PIN 1. The guard now binds a Git root; ``plan_id`` still must not.

    ``freeze_manifest`` hands ``launch_guard.capture`` its one stat for either root kind, because
    nothing recomputes ``plan_id`` inside a run or at resume. The IDENTIFIER side has to be untouched
    by that: the pair must stay out of ``repo_baseline``, or "two checkouts in the same state are ONE
    plan" dies and every Git plan on every machine needs a fresh approval.

    Two assertions, because each fails on its own. The absent key is the mechanism. The two clones
    sharing one ``plan_id`` is the property the mechanism exists for, and it is the thing a reader of
    ``git_baseline``'s module docstring is promised. The golden value in
    ``test_non_git_plan_ids_bind_root_and_git_golden_is_unchanged`` carries the arithmetic: it was
    computed before this change and is unchanged by it.
    """
    _store(tmp_path, monkeypatch)
    repo = _git_repository(tmp_path / "repo", content="one")
    document = _frozen(cwd=str(repo))
    assert "root_identity" not in document["repo_baseline"], (
        "a Git root's device and inode reached repo_baseline, therefore plan_id: every existing Git "
        "approval is now invalid and two checkouts in the same state are two plans"
    )

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(repo), str(clone)], check=True, capture_output=True)
    assert _frozen(cwd=str(clone))["plan_id"] == document["plan_id"], (
        "two checkouts of the same commit with the same clean worktree are no longer one plan, so a "
        "Git root is being identified by location rather than by content"
    )


def test_an_unbindable_non_git_root_logs_its_reason_at_warning_and_no_path(
    tmp_path, monkeypatch, caplog
):
    """fanhongy's P3-4(b) on PR #1051: the promise that the server SAYS so was not pinned.

    ``CHANGELOG.md`` and ``docs/workflows.md`` both promise that where the filesystem supplies no
    usable inode, CAO records the fallback instead of skipping it and "the server logs that reason
    with no path". fanhongy downgraded this ``logger.warning`` to ``debug`` and all 422 focused tests
    passed, so the promise was documentation only.

    WARNING specifically, not merely "a log line": a debug line is invisible at the default level,
    which is the whole difference between a recorded fallback and a silent one. And no path, mirroring
    ``test_returns_none_and_logs_rather_than_raising`` and this module's path-free diagnostics rule --
    the unbound marker in the manifest is where the operator reads WHICH plan this is.
    """
    _store(tmp_path, monkeypatch)
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(
        manifest_freeze,
        "root_identity",
        lambda path: {"bound": False, "reason": "root_identity_unsupported"},
    )
    with caplog.at_level("WARNING"):
        frozen = manifest_freeze.freeze_manifest(source_hash="h", inputs={}, cwd=str(root))

    assert frozen.manifest_json is not None, "an unbindable root is approvable, not a refusal"
    unbound = [
        record
        for record in caplog.records
        if "could not bind the working-directory root identity" in record.message
    ]
    assert len(unbound) == 1 and unbound[0].levelno == logging.WARNING, (
        "the unbound-root fallback is not reported at WARNING, so an operator running at the default "
        f"level never learns the plan they approved has a weaker binding (records={caplog.records})"
    )
    assert "root_identity_unsupported" in unbound[0].getMessage()
    assert str(tmp_path) not in caplog.text, "this module's diagnostics must stay path-free"


@pytest.mark.parametrize("kind", ["git", "non-git"])
def test_the_freeze_hands_the_guard_the_one_stat_it_took_for_either_root_kind(
    tmp_path, monkeypatch, kind
):
    """PIN 2. ``FrozenManifest.root_identity`` IS the single stat, for a Git root as much as a non-Git one.

    Two regressions this catches, and both are silent:

    * reading the guard's value back off the baseline (``baseline.get("root_identity")``) rather than
      from the stat. That is what left a Git root arriving at the guard as ``None``, so a Git worktree
      replaced mid-run or across a resume was never detected (ask 2).
    * taking a SECOND stat for the guard. The second would be read after the approval gate, so a root
      replaced in that window would be recorded as the approved one -- the P3-3 window this PR exists
      to keep closed. Exactly one call is the only form of that assertion that cannot be satisfied by
      two stats that happen to agree on an idle filesystem.
    """
    _store(tmp_path, monkeypatch)
    if kind == "git":
        root = _git_repository(tmp_path / "root", content="one")
    else:
        root = tmp_path / "root"
        root.mkdir()
        from cli_agent_orchestrator.utils import git_baseline

        assert git_baseline.derive_baseline(str(root)).get("repository") is False, (
            "fixture assumption broken: this root must be outside Git for the non-Git case to mean "
            "anything"
        )

    observed = []
    real = manifest_freeze.root_identity

    def counting(path):
        value = real(path)
        observed.append(value)
        return value

    monkeypatch.setattr(manifest_freeze, "root_identity", counting)
    frozen = manifest_freeze.freeze_manifest(source_hash="h", inputs={}, cwd=str(root))

    assert len(observed) == 1, (
        f"the freeze stat'd the root {len(observed)} times for a {kind} root; the identifier and the "
        "guard must describe one observation of the directory, not two either side of the gate"
    )
    assert frozen.root_identity == observed[0], (
        f"a {kind} root's guard value is not the stat the freeze took "
        f"(got {frozen.root_identity!r}, stat was {observed[0]!r})"
    )
    assert frozen.root_identity is not None and frozen.root_identity["bound"] is True

    baseline = json.loads(frozen.manifest_json or "{}")["repo_baseline"]
    if kind == "git":
        assert "root_identity" not in baseline, "see PIN 1: the pair must not reach plan_id here"
    else:
        assert (
            baseline["root_identity"] == frozen.root_identity
        ), "a non-Git root's plan_id and its guard disagree about which directory was approved"


# ---------------------------------------------------------------------------
# The agent profile set is part of plan identity (issue #827)
# ---------------------------------------------------------------------------


def _store(tmp_path, monkeypatch, **profiles) -> None:
    """Point the local agent store at ``tmp_path`` and write ``name -> source text`` into it.

    Patches the module-global the real scan and the real read both consult, so these tests exercise
    ``_list_installed_profiles`` and ``_read_agent_profile_source`` rather than a stub of them. The
    built-in packaged store is still scanned and is identical across the cases, so it cancels out.

    Provider and extra directories are pinned to empty so a stray entry in a real
    ``~/.claude/agents``-style directory on the machine running the tests cannot change which assertions
    pass.
    """
    monkeypatch.setattr(settings_service, "usable_agent_dirs", lambda: {})
    monkeypatch.setattr(settings_service, "get_extra_agent_dirs", lambda: [])
    monkeypatch.setattr(settings_service, "get_disabled_agent_dirs", lambda: [])
    # The installed-artifact sinks default to real paths under the user's home (PR #1000, P2-2).
    from cli_agent_orchestrator.services import install_service

    for attr in (
        "KIRO_AGENTS_DIR",
        "COPILOT_AGENTS_DIR",
        "OPENCODE_AGENTS_DIR",
        "AGENT_CONTEXT_DIR",
    ):
        monkeypatch.setattr(install_service, attr, tmp_path / "install" / attr.lower())
    # The shared OpenCode config is an installed artifact too (PR #1000 round 2, P2-1); unpinned, the
    # digest would read, and the artifact tests would WRITE, ~/.aws/opencode/opencode.json.
    from cli_agent_orchestrator.utils import opencode_config

    monkeypatch.setattr(
        opencode_config, "OPENCODE_CONFIG_FILE", tmp_path / "install" / "opencode.json"
    )
    store = tmp_path / "agent-store"
    store.mkdir(exist_ok=True)
    for stale in store.glob("*.md"):
        stale.unlink()
    for name, text in profiles.items():
        (store / f"{name}.md").write_text(text, encoding="utf-8")
    monkeypatch.setattr(agent_profiles, "LOCAL_AGENT_STORE_DIR", store)


def test_rewriting_an_agent_profile_changes_the_plan_id(tmp_path, monkeypatch):
    """ISSUE #827's FIRST ACCEPTANCE CRITERION, and the reason this unit needed a scheme bump.

    A script step's behaviour and tool policy come from whichever profile it names, resolved from a
    file OUTSIDE the script at launch time. Under ``plan-v1`` that file was in no component of the
    identifier, so rewriting a profile left the ``plan_id`` byte-identical and a brand-new run matched
    the approval granted for the old profile. Nothing refused it.

    If this test fails, an edited agent profile is again executing under a stale approval.
    """
    _store(tmp_path, monkeypatch, reviewer="---\nname: reviewer\n---\nBe careful.")
    before = _frozen()["plan_id"]

    _store(tmp_path, monkeypatch, reviewer="---\nname: reviewer\n---\nrm -rf everything.")
    after = _frozen()["plan_id"]

    assert before != after


def test_installing_or_removing_a_profile_changes_the_plan_id(tmp_path, monkeypatch):
    """The FULL set, not just the profiles a plan happens to name.

    Script-tier steps are discovered by executing the Python, so at approval time there is no
    inventory of which profiles a run will launch. A digest scoped to "the profiles this plan uses"
    would need a list that cannot be produced when it is needed, so adding or removing ANY profile
    must move the identifier.
    """
    _store(tmp_path, monkeypatch, reviewer="---\nname: reviewer\n---\nOne.")
    one = _frozen()["plan_id"]

    _store(
        tmp_path,
        monkeypatch,
        reviewer="---\nname: reviewer\n---\nOne.",
        builder="---\nname: builder\n---\nTwo.",
    )
    two = _frozen()["plan_id"]

    assert one != two


def test_renaming_a_profile_without_changing_its_text_changes_the_plan_id(tmp_path, monkeypatch):
    """The name is digested alongside the content, because a step resolves its profile BY NAME."""
    body = "---\nname: whoever\n---\nSame words."
    _store(tmp_path, monkeypatch, reviewer=body)
    as_reviewer = _frozen()["plan_id"]
    _store(tmp_path, monkeypatch, builder=body)
    as_builder = _frozen()["plan_id"]
    assert as_reviewer != as_builder


def test_an_unchanged_profile_set_leaves_the_plan_id_stable(tmp_path, monkeypatch):
    """The other half of the contract: no spurious re-approval.

    A digest that moved on its own would demand re-approval on every run, which trains an operator to
    approve without reading — the same false positive ``plan_identifier``'s key sorting exists to
    prevent.
    """
    _store(tmp_path, monkeypatch, reviewer="---\nname: reviewer\n---\nStable.")
    assert _frozen()["plan_id"] == _frozen()["plan_id"]


def test_an_unreadable_profile_set_fails_the_freeze_closed_as_profiles_unreadable(
    monkeypatch, caplog
):
    """A store CAO cannot READ is refused under its own closed reason, not ``internal``.

    ``internal`` tells the operator "this is a CAO-side failure, retry"; a ``PermissionError`` on a profile
    entry does not go away on retry and is not CAO's fault. ``profiles_unreadable`` already exists in the
    closed vocabulary and its message names the profile set (PR #1000, P2-1).
    """

    def _locked(_name):
        raise PermissionError("mode 000")

    monkeypatch.setattr(agent_profiles, "_read_agent_profile_source", _locked)
    with caplog.at_level("WARNING"):
        frozen = manifest_freeze.freeze_manifest(source_hash="h", inputs={})
    assert frozen.manifest_json is None
    assert frozen.unavailable_reason == "profiles_unreadable"
    assert any("profiles_unreadable" in r.message for r in caplog.records)
    assert not any("mode 000" in r.message for r in caplog.records)


def test_a_profile_that_vanishes_mid_scan_still_freezes_to_an_identity_matching_neither_state(
    tmp_path, monkeypatch
):
    """The race case is fail-closed WITHOUT a NULL manifest: the vanished entry digests as a sentinel,
    so the identifier matches neither the approval for "present" nor the one for "absent"."""
    body = "---\nname: reviewer\n---\nA."
    _store(tmp_path, monkeypatch, reviewer=body)
    present = _frozen()["plan_id"]
    _store(tmp_path, monkeypatch)
    absent = _frozen()["plan_id"]
    _store(tmp_path, monkeypatch, reviewer=body)

    def _vanished(_name):
        raise FileNotFoundError("profile removed mid-scan")

    monkeypatch.setattr(agent_profiles, "_read_agent_profile_source", _vanished)
    raced = _frozen()["plan_id"]
    assert raced not in {present, absent}


def test_the_freeze_returns_the_profile_set_read_its_plan_id_was_computed_from(
    tmp_path, monkeypatch
):
    """P3-3: one read, shared. ``profile_set`` digests to the value folded into this plan_id, so the
    launch guard built from it describes the admitted state and not a later one."""
    _store(tmp_path, monkeypatch, reviewer="---\nname: reviewer\n---\nA.")
    frozen = manifest_freeze.freeze_manifest(source_hash="h", inputs={}, cwd=str(tmp_path))
    assert frozen.profile_set is not None
    assert "reviewer" in frozen.profile_set
    assert frozen.profile_set["reviewer"].source == "---\nname: reviewer\n---\nA."
    plan_from_snapshot = manifest_freeze.freeze_manifest(
        source_hash="h", inputs={}, cwd=str(tmp_path)
    )
    assert (
        json.loads(plan_from_snapshot.manifest_json)["plan_id"]
        == json.loads(frozen.manifest_json)["plan_id"]
    )
    # The snapshot is what the digest hashed: it reproduces the live digest while nothing changed, and
    # keeps reproducing the FROZEN one after an edit the live store no longer matches.
    live_before = agent_profiles.profile_set_digest()
    assert agent_profiles.profile_set_digest(frozen.profile_set) == live_before
    (tmp_path / "agent-store" / "reviewer.md").write_text("---\nname: reviewer\n---\nB.")
    assert agent_profiles.profile_set_digest() != live_before
    assert agent_profiles.profile_set_digest(frozen.profile_set) == live_before


def test_a_freeze_that_fails_before_the_profile_set_carries_no_snapshot(monkeypatch):
    monkeypatch.setattr(
        manifest_freeze,
        "derive_baseline",
        lambda root, **_kw: {"available": False, "reason": "git_timeout"},
    )
    frozen = manifest_freeze.freeze_manifest(source_hash="h", inputs={})
    assert frozen.manifest_json is None
    assert frozen.profile_set is None
