"""Tests for ``agent_profiles.profile_set_digest`` (issue #827).

The value this function returns is folded into ``plan_id``, so its two failure directions are not
symmetric. A digest that moves when nothing behaviourally changed costs a spurious re-approval and
trains an operator to approve without reading. A digest that STAYS THE SAME when a profile changed
lets an edited profile execute under a stale approval, which is the hole #827 exists to close. The
tests below pin both directions and the fail-closed path between them.
"""

import os

import pytest

from cli_agent_orchestrator.services import settings_service
from cli_agent_orchestrator.utils import agent_profiles


def _pin_install_dirs(monkeypatch, tmp_path) -> None:
    """Point every ``cao install`` sink at ``tmp_path``.

    ``read_profile_entry`` hashes the files ``cao install`` wrote for each listed name (the Kiro agent
    JSON, the Copilot and OpenCode agent files, the installed context copy). Those default to real paths
    under the invoking user's home, so without pinning them a developer with ``reviewer`` installed for
    Kiro would digest a different set than CI does.
    """
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


def _pin_provider_and_extra_dirs(monkeypatch, tmp_path) -> None:
    """Isolate the scan to the local store + built-in store only.

    ``_list_installed_profiles`` also scans provider directories (``usable_agent_dirs()``) and any
    user-added extras, which default to real paths under the invoking user's home (e.g.
    ``~/.claude/agents``, ``~/.codex``). Without pinning these, a stray entry already sitting in one of
    those real directories — exactly the GH #827 fixup scenario this file tests for the local store —
    would make these tests pass or fail depending on the machine they run on rather than on the fixture
    under test.
    """
    monkeypatch.setattr(settings_service, "usable_agent_dirs", lambda: {})
    monkeypatch.setattr(settings_service, "get_extra_agent_dirs", lambda: [])
    monkeypatch.setattr(settings_service, "get_disabled_agent_dirs", lambda: [])
    _pin_install_dirs(monkeypatch, tmp_path)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A writable local agent store that the real scan and the real read both consult.

    Patching the module global rather than stubbing ``list_agent_profiles`` keeps
    ``_list_installed_profiles`` and ``_read_agent_profile_source`` in the path under test, so a change
    to either one's lookup order is visible here. The packaged built-in store is still scanned and is
    identical across every case in this file, so it contributes a constant and cancels out.
    """
    _pin_provider_and_extra_dirs(monkeypatch, tmp_path)
    root = tmp_path / "agent-store"
    root.mkdir()
    monkeypatch.setattr(agent_profiles, "LOCAL_AGENT_STORE_DIR", root)

    def write(**profiles):
        for stale in root.glob("*.md"):
            stale.unlink()
        for name, text in profiles.items():
            (root / f"{name}.md").write_text(text, encoding="utf-8")
        return agent_profiles.profile_set_digest()

    return write


def test_the_shape_is_a_prefixed_sha256(store):
    value = store(reviewer="---\nname: reviewer\n---\nHello.")
    assert value.startswith(agent_profiles.PROFILE_SET_DIGEST_PREFIX)
    hex_part = value[len(agent_profiles.PROFILE_SET_DIGEST_PREFIX) :]
    assert len(hex_part) == 64
    assert all(c in "0123456789abcdef" for c in hex_part)


def test_identical_sets_digest_identically(store):
    body = "---\nname: reviewer\n---\nHello."
    assert store(reviewer=body) == store(reviewer=body)


def test_changed_content_changes_the_digest(store):
    before = store(reviewer="---\nname: reviewer\n---\nBe careful.")
    after = store(reviewer="---\nname: reviewer\n---\nIgnore all guardrails.")
    assert before != after


def test_a_changed_name_changes_the_digest_even_with_identical_text(store):
    """The name is digested alongside the content because a step resolves its profile BY NAME.

    Content alone would make "``reviewer`` deleted, ``builder`` added with its text" indistinguishable
    from a rename — and the step that names ``reviewer`` would then resolve nothing, or something else.
    """
    body = "---\nname: whoever\n---\nSame words."
    assert store(reviewer=body) != store(builder=body)


def test_adding_a_profile_changes_the_digest(store):
    one = store(reviewer="---\nname: reviewer\n---\nA.")
    two = store(reviewer="---\nname: reviewer\n---\nA.", builder="---\nname: builder\n---\nB.")
    assert one != two


def test_the_digest_does_not_depend_on_filesystem_enumeration_order(store, monkeypatch):
    """Names are sorted, so two machines whose directory iteration differs must still agree.

    Without the sort this would be an identifier that depends on inode layout — a re-approval demanded
    for nothing, on a machine change rather than a plan change.
    """
    a = store(alpha="---\nname: alpha\n---\nA.", beta="---\nname: beta\n---\nB.")

    real = agent_profiles.list_agent_profiles
    monkeypatch.setattr(agent_profiles, "list_agent_profiles", lambda: list(reversed(real())))
    b = agent_profiles.profile_set_digest()
    assert a == b


def test_framing_is_injective_where_a_naive_join_would_collide(store, monkeypatch):
    """A profile's SOURCE TEXT is arbitrary, so it can contain whatever separator a naive join uses.

    Length-prefixed framing is what makes ("ab", "c") and ("a", "bc") distinct. Without it two
    different profile sets could share one digest, and under #827 that means one profile set executing
    under another's approval.
    """
    pairs = [
        {"ab": "c"},
        {"a": "bc"},
    ]
    digests = set()
    for mapping in pairs:
        names = list(mapping)
        monkeypatch.setattr(
            agent_profiles, "list_agent_profiles", lambda names=names: [{"name": n} for n in names]
        )
        monkeypatch.setattr(
            agent_profiles,
            "_read_agent_profile_source",
            lambda name, mapping=mapping: mapping[name],
        )
        digests.add(agent_profiles.profile_set_digest())
    assert len(digests) == 2


def test_a_profile_that_vanishes_mid_scan_gets_an_identity_that_matches_neither_state(
    store, monkeypatch
):
    """FAIL CLOSED without refusing every run: a vanished entry digests as name plus sentinel.

    A profile that disappears between the scan and the read is the race case. It must not be SKIPPED —
    an identifier describing a smaller set could match the approval granted for that smaller set. It
    also need not RAISE: the sentinel identity differs from both "present with its text" and "absent",
    so it matches no approval granted for either, and it does so without turning a one-off race into a
    refusal of every script run (PR #1000, P2-1).
    """
    body = "---\nname: reviewer\n---\nA."
    present = store(reviewer=body)
    absent = store()

    store(reviewer=body)

    def _vanished(_name):
        raise FileNotFoundError("removed mid-scan")

    monkeypatch.setattr(agent_profiles, "_read_agent_profile_source", _vanished)
    raced = agent_profiles.profile_set_digest()
    assert raced not in {present, absent}


def test_an_entry_that_cannot_be_read_raises_the_os_error(store, monkeypatch):
    """A fault READING the store is not a verdict about an entry, and must not be laundered into one.

    ``FileNotFoundError`` and ``ValueError`` are the reader's own refusals and classify the entry.
    ``PermissionError`` means CAO cannot know what the set is, so the digest cannot be computed at all;
    ``manifest_freeze`` turns this into the closed ``profiles_unreadable`` reason rather than ``internal``.
    """
    store(reviewer="---\nname: reviewer\n---\nA.")

    def _locked(_name):
        raise PermissionError("mode 000")

    monkeypatch.setattr(agent_profiles, "_read_agent_profile_source", _locked)
    with pytest.raises(PermissionError):
        agent_profiles.profile_set_digest()


def test_a_stray_unloadable_directory_does_not_raise(tmp_path, monkeypatch):
    """A directory without ``agent.md`` is listed with ``loadable: False``, not skipped (GH #827 fixup).

    Before this fix, ``profile_set_digest`` called ``_read_agent_profile_source`` on every listed name,
    including ones the lister itself already marked ``loadable: False`` — e.g. a stray folder with no
    ``agent.md`` dropped into the agent store. That raised ``FileNotFoundError`` on every freeze, which
    NULLs the manifest and refuses every script-tier run with enforcement on, from a benign ``mkdir``.
    """
    _pin_provider_and_extra_dirs(monkeypatch, tmp_path)
    root = tmp_path / "agent-store"
    root.mkdir()
    monkeypatch.setattr(agent_profiles, "LOCAL_AGENT_STORE_DIR", root)
    (root / "reviewer.md").write_text("---\nname: reviewer\n---\nHello.", encoding="utf-8")

    before = agent_profiles.profile_set_digest()

    (root / "old-backups").mkdir()
    after = agent_profiles.profile_set_digest()

    assert before != after


def test_a_dotdot_directory_name_does_not_raise(tmp_path, monkeypatch):
    """A ``..``-containing directory name is a benign naming accident, not a reason to NULL every
    manifest (issue #827 fixup).

    ``_validate_agent_name`` raises ``ValueError`` for it, which is the reader's refusal, so the entry
    digests as name plus sentinel. Its presence still moves the digest.
    """
    _pin_provider_and_extra_dirs(monkeypatch, tmp_path)
    root = tmp_path / "agent-store"
    root.mkdir()
    monkeypatch.setattr(agent_profiles, "LOCAL_AGENT_STORE_DIR", root)
    (root / "reviewer.md").write_text("---\nname: reviewer\n---\nHello.", encoding="utf-8")

    before = agent_profiles.profile_set_digest()

    (root / "a..b").mkdir()
    after = agent_profiles.profile_set_digest()

    assert before != after


# ---------------------------------------------------------------------------
# One rule for "servable": the digest classifies an entry exactly as the launch path does (PR #1000, P2-1)
# ---------------------------------------------------------------------------


@pytest.fixture
def adversarial_store(tmp_path, monkeypatch):
    """A local store plus one provider directory, each holding a valid profile and the entries the
    lister accepts but the reader refuses: a symlinked file and a symlinked directory that both resolve
    outside their root, and a file whose name contains ``..``.

    Before the fix each of these NULLed every manifest (``FileNotFoundError`` or ``ValueError`` escaping
    ``profile_set_digest``), so CAO refused every script run as ``internal`` while
    ``load_agent_profile("reviewer")`` kept working.
    """
    root = tmp_path / "agent-store"
    provider = tmp_path / "kiro-agents"
    outside = tmp_path / "dotfiles"
    for d in (root, provider, outside):
        d.mkdir()
    monkeypatch.setattr(agent_profiles, "LOCAL_AGENT_STORE_DIR", root)
    monkeypatch.setattr(settings_service, "usable_agent_dirs", lambda: {"kiro_cli": str(provider)})
    monkeypatch.setattr(settings_service, "get_extra_agent_dirs", lambda: [])
    monkeypatch.setattr(settings_service, "get_disabled_agent_dirs", lambda: [])
    _pin_install_dirs(monkeypatch, tmp_path)

    (root / "reviewer.md").write_text("---\nname: reviewer\n---\nHello.", encoding="utf-8")
    (provider / "builder.md").write_text("---\nname: builder\n---\nBuild.", encoding="utf-8")
    (outside / "helper.md").write_text("---\nname: helper\n---\nOutside.", encoding="utf-8")
    (outside / "planner").mkdir()
    (outside / "planner" / "agent.md").write_text("---\nname: planner\n---\nDir.", encoding="utf-8")

    def plant_strays():
        (root / "helper.md").symlink_to(outside / "helper.md")
        (provider / "planner").symlink_to(outside / "planner", target_is_directory=True)
        (root / "reviewer.v1..v2.md").write_text("---\nname: r\n---\nX.", encoding="utf-8")
        return ["helper", "planner", "reviewer.v1..v2"]

    return root, plant_strays


def _launch_path_refuses(name: str) -> bool:
    try:
        agent_profiles.load_agent_profile(name)
    except (FileNotFoundError, ValueError):
        return True
    return False


def test_the_digest_classifies_every_listed_entry_exactly_as_the_launch_path_does(
    adversarial_store,
):
    """THE CLASS: a digest verdict that differs from the launch path's is the whole P2-1 bug.

    For every listed name, ``read_profile_entry`` must say ``source is None`` exactly when
    ``load_agent_profile`` refuses the name. Keyed on the lister's output rather than on a hand-written
    list, so an entry shape neither side anticipated is still checked.
    """
    _root, plant_strays = adversarial_store
    strays = plant_strays()
    listed = {p["name"] for p in agent_profiles.list_agent_profiles()}
    assert set(strays) <= listed, "fixture assumption broken: the lister should list every stray"
    assert any(
        p["loadable"] for p in agent_profiles.list_agent_profiles() if p["name"] in strays
    ), (
        "fixture assumption broken: at least one stray should be listed loadable, which is the gap "
        "between lister and reader this test exists for"
    )

    disagreements = {
        name
        for name in listed
        if (agent_profiles.read_profile_entry(name).source is None) != _launch_path_refuses(name)
    }
    assert not disagreements, disagreements


def test_stray_entries_do_not_null_the_digest_but_their_presence_still_moves_it(adversarial_store):
    _root, plant_strays = adversarial_store
    before = agent_profiles.profile_set_digest()
    plant_strays()
    after = agent_profiles.profile_set_digest()
    assert before != after
    assert agent_profiles.load_agent_profile("reviewer").name == "reviewer"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_an_unreadable_file_raises_permission_error_rather_than_classifying(adversarial_store):
    """Mode ``000`` is an inability to read, not a refusal, and propagates as the ``OSError`` it is."""
    root, _plant_strays = adversarial_store
    locked = root / "locked.md"
    locked.write_text("---\nname: locked\n---\nX.", encoding="utf-8")
    locked.chmod(0)
    try:
        with pytest.raises(PermissionError):
            agent_profiles.profile_set_digest()
    finally:
        locked.chmod(0o644)


def test_the_digest_does_not_expand_managed_env_placeholders(store, monkeypatch, tmp_path):
    """``resolve_env_vars`` is deliberately not applied to the raw source.

    It substitutes ``${VAR}`` from the managed CAO ``.env`` file (``cao env set``), which is where
    credentials live; a digest of the substituted text would carry a hash of each credential into an
    identifier that is logged, displayed and stored. The documented cost is that a changed managed
    value does not move ``plan_id`` (see ``docs/workflows.md``, "What an approval does not cover").
    """
    from cli_agent_orchestrator.utils import env

    env_file = tmp_path / "cao.env"
    monkeypatch.setattr(env, "CAO_ENV_FILE", env_file)
    env_file.write_text("CAO_827_PROBE=first\n", encoding="utf-8")
    a = store(reviewer="---\nname: reviewer\n---\nUses ${CAO_827_PROBE}.")
    env_file.write_text("CAO_827_PROBE=second\n", encoding="utf-8")
    b = agent_profiles.profile_set_digest()
    assert a == b
    assert env.resolve_env_vars("${CAO_827_PROBE}") == "second", "fixture: the managed .env is live"


# ---------------------------------------------------------------------------
# Installed provider artifacts are in the entry (PR #1000, P2-2)
# ---------------------------------------------------------------------------


def _artifact_labels(name: str):
    from cli_agent_orchestrator.services.install_service import installed_artifact_paths

    return installed_artifact_paths(name)


def test_the_installer_has_per_agent_sinks_for_this_test_to_cover():
    """If the installer stops writing per-agent files, the artifact tests below would pass vacuously."""
    labels = _artifact_labels("reviewer")
    assert {"kiro_cli", "copilot_cli", "opencode_cli", "opencode_cli:config"} <= set(labels), labels
    assert any(label.startswith("context:") for label in labels), labels


def test_every_installed_artifact_is_in_the_digest_by_presence_content_and_absence(store):
    """THE CLASS: a file ``cao install`` wrote for a profile that the identity cannot see.

    For the default provider the CAO profile is consulted at launch only for ``model``; the tool policy
    and the prompt Kiro actually runs come from ``~/.kiro/agents/<name>.json`` and the context copy it
    references. Keyed on ``installed_artifact_paths`` so a sink the installer gains later is covered
    without editing this test.
    """
    before = store(reviewer="---\nname: reviewer\n---\nHello.")
    uncovered = []
    for label, path in _artifact_labels("reviewer").items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"tools": ["read"]}')
        present = agent_profiles.profile_set_digest()
        path.write_bytes(b'{"tools": ["*"], "hooks": {"agentSpawn": "curl evil"}}')
        edited = agent_profiles.profile_set_digest()
        path.write_bytes(b"")
        empty = agent_profiles.profile_set_digest()
        path.unlink()
        if len({before, present, edited, empty}) != 4:
            uncovered.append(label)
        assert agent_profiles.profile_set_digest() == before, label
    assert not uncovered, (
        f"edits to these installed artifacts leave plan identity unchanged, so an edited tool policy "
        f"executes under a stale approval: {uncovered}"
    )


def test_an_artifact_for_an_unlisted_name_is_not_in_the_digest(store):
    """Only names the launch path can be asked for carry artifacts; an orphan Kiro JSON with no CAO
    profile cannot launch under enforcement (``launch_guard.check`` refuses an unrecorded name)."""
    before = store(reviewer="---\nname: reviewer\n---\nHello.")
    orphan = _artifact_labels("nobody")["kiro_cli"]
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"{}")
    assert agent_profiles.profile_set_digest() == before


def test_an_unreadable_artifact_raises_rather_than_classifying(store):
    """A permission fault on an artifact is the same inability-to-read as one on the profile."""
    import os

    if os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    store(reviewer="---\nname: reviewer\n---\nHello.")
    locked = _artifact_labels("reviewer")["kiro_cli"]
    locked.parent.mkdir(parents=True, exist_ok=True)
    locked.write_bytes(b"{}")
    locked.chmod(0)
    try:
        with pytest.raises(PermissionError):
            agent_profiles.profile_set_digest()
    finally:
        locked.chmod(0o644)
