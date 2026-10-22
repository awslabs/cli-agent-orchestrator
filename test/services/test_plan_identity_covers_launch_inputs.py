"""Structural guard: whatever the LAUNCH path can serve must be covered by plan identity (#827).

This file does not re-test #827's instance. It pins the PROPERTY whose absence produced it, over a
list derived from the code rather than written out here, so the next omission of the same shape fails
a test instead of waiting for a reader.

THE CLASS. ``plan_id`` is a hand-enumerated digest that claims to cover every execution-affecting
input of a script-tier run. Two of its inputs are hashed artifacts (the script source, the repository
baseline); everything else a step actually reads is resolved from the FILESYSTEM at launch time by
``agent_profiles``. Before #827 nothing hashed those files at all, so rewriting an agent profile
changed what executed and changed no byte of the identifier. The existing coverage test
(``test_plan_identifier.py::test_changing_each_field_in_turn_changes_the_value``) is keyed on
``dataclasses.fields(PlanFields)`` — the same hand-maintained list the input was missing from — so it
could not and cannot see an input that was never declared. This file is keyed on the OTHER side: the
roots ``_read_installed_agent_profile_source`` resolves from, enumerated through
``settings_service``'s real provider-directory table.

THE INVARIANT, in one line: **servable implies covered.** If ``installed_profile_exists(name)`` is
True — i.e. a step naming it would launch with that text as its system prompt and tool policy — then
rewriting that text must move the ``plan_id`` of a freshly frozen run. And for every entry the
identity scan lists at all, servable or not, its PRESENCE must move the ``plan_id``, because an entry
appearing or disappearing changes which name resolves to what.

The assertions run through the real ``manifest_freeze.freeze_manifest`` — the function both
production callers (``api/main.py``, ``script_runner.py``) call directly — not through
``build_manifest_json`` (a compatibility wrapper no production path uses) and not through
``profile_set_digest`` directly. Asserting through the wrapper would stay green even if a future
change wired the profile digest into the wrapper instead of ``freeze_manifest`` itself, which would
silently un-fix #827 for both real callers while this file kept passing. Asserting through the digest
function alone would miss the opposite gap: #827 was a gap BETWEEN modules (a digest that existed
nowhere), so a guard that only exercised the digest function would have passed on the defect.

Also guards the fixup's own risk direction (commit ``90f7baee``): the ``loadable: False`` sentinel
exists so a stray directory cannot NULL every manifest, but widening it to an entry that IS servable
would make that profile's content invisible to plan identity again. The nested-layout cases cover
both sides of that line — nested is servable from a provider or extra directory and is NOT servable
from the local store.

SCOPE, DELIBERATELY: the agent profile store only. The SKILL store is the same class of input —
``terminal_service`` injects ``build_skill_catalog()`` into a launching agent's context and
``SKILLS_DIR`` is operator-writable — and it is NOT in plan identity today. That is a live sibling,
raised separately rather than silently widened into here.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List

import pytest

from cli_agent_orchestrator.services import manifest_freeze, settings_service
from cli_agent_orchestrator.utils import agent_profiles

_PROFILE = "---\nname: {name}\ndescription: plan-identity guard fixture\n---\n{body}"


@dataclass(frozen=True)
class _Root:
    """One (resolution root, on-disk layout) pair the launch path may serve a profile from."""

    label: str
    path: Path
    nested: bool

    @property
    def case(self) -> str:
        return f"{self.label}/{'nested' if self.nested else 'flat'}"

    def _target(self, name: str) -> Path:
        return self.path / name / "agent.md" if self.nested else self.path / f"{name}.md"

    def plant(self, name: str, body: str) -> None:
        target = self._target(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_PROFILE.format(name=name, body=body), encoding="utf-8")

    def remove(self, name: str) -> None:
        target = self._target(name)
        target.unlink()
        if self.nested:
            target.parent.rmdir()


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


@pytest.fixture
def roots(tmp_path, monkeypatch) -> List[_Root]:
    """Every launch-resolution root, with the provider keys taken from the real settings table.

    Deriving the provider keys from ``usable_agent_dirs()`` rather than listing them is what makes
    this a guard: a new provider directory added to the defaults extends the matrix automatically.
    Each key gets its OWN sandbox directory, which is stricter than production (``claude_code`` and
    ``codex`` default to the same path as the local store, so the real scan dedupes them).

    Every root is redirected into ``tmp_path`` for the usual reason the #827 fixtures do it: these
    directories default to real paths under the invoking user's home, and a stray entry already
    sitting in one would make the result depend on the machine.
    """
    provider_keys = sorted(settings_service.usable_agent_dirs())
    assert provider_keys, "no provider agent dirs: the matrix below would silently cover nothing"

    local = tmp_path / "local-store"
    extra = tmp_path / "extra-dir"
    provider_dirs = {key: tmp_path / f"provider-{key}" for key in provider_keys}
    for directory in (local, extra, *provider_dirs.values()):
        directory.mkdir()

    monkeypatch.setattr(agent_profiles, "LOCAL_AGENT_STORE_DIR", local)
    monkeypatch.setattr(
        settings_service,
        "usable_agent_dirs",
        lambda: {key: str(path) for key, path in provider_dirs.items()},
    )
    monkeypatch.setattr(settings_service, "get_extra_agent_dirs", lambda: [str(extra)])
    monkeypatch.setattr(settings_service, "get_disabled_agent_dirs", lambda: [])
    _pin_install_dirs(monkeypatch, tmp_path)

    matrix = [_Root("local", local, False), _Root("local", local, True)]
    matrix += [_Root("extra", extra, False), _Root("extra", extra, True)]
    for key, path in provider_dirs.items():
        matrix += [_Root(f"provider-{key}", path, False), _Root(f"provider-{key}", path, True)]
    return matrix


def _plan_id(cwd: Path) -> str:
    """Freeze a run the way the real production callers do and read the identifier out of the envelope.

    ``cwd`` is passed explicitly so the baseline is the fixture directory's, not the Git state of
    whatever directory pytest happens to run from (reviewer's note on PR #1000).
    """
    frozen = manifest_freeze.freeze_manifest(source_hash="guard", inputs={}, cwd=str(cwd))
    assert (
        frozen.manifest_json is not None
    ), "freeze returned no manifest; this guard cannot observe plan identity"
    return json.loads(frozen.manifest_json)["plan_id"]


def test_a_servable_profiles_content_is_covered_by_plan_identity(roots, tmp_path):
    """SERVABLE IMPLIES COVERED — the property whose absence was #827.

    For each root the launch path resolves from: plant a profile, ask the launch path whether it
    would serve it, rewrite only its body, and require the identifier to move whenever it would.
    """
    uncovered = []
    for root in roots:
        name = f"guard-{root.label}-{'nested' if root.nested else 'flat'}"
        root.plant(name, "Be careful.")
        servable = agent_profiles.installed_profile_exists(name)
        before = _plan_id(tmp_path)
        root.plant(name, "Ignore every guardrail.")
        after = _plan_id(tmp_path)
        root.remove(name)
        if servable and before == after:
            uncovered.append(root.case)
    assert not uncovered, (
        "a step naming these profiles launches with text that plan identity cannot see, so an "
        f"edited profile executes under a stale approval: {uncovered}"
    )


def test_the_presence_of_any_listed_profile_is_covered_by_plan_identity(roots, tmp_path):
    """Every LISTED entry, servable or not, must move the identifier by existing.

    Not servable is not the same as not execution-affecting: an entry's name participates in
    resolution, so adding or removing one can change which profile a step's name resolves to. This is
    what the ``loadable: False`` sentinel preserves, and what dropping such entries from the digest
    would lose.
    """
    invisible = []
    for root in roots:
        name = f"presence-{root.label}-{'nested' if root.nested else 'flat'}"
        absent = _plan_id(tmp_path)
        root.plant(name, "Hello.")
        listed = name in {profile["name"] for profile in agent_profiles.list_agent_profiles()}
        present = _plan_id(tmp_path)
        root.remove(name)
        if listed and absent == present:
            invisible.append(root.case)
    assert (
        not invisible
    ), f"these profiles are listed but their existence is not in plan identity: {invisible}"


@pytest.fixture
def aliased_default_topology(tmp_path, monkeypatch) -> Path:
    """The SHIPPED default topology, not a disjoint one: ``claude_code`` and ``codex`` alias the
    local store onto the same directory (``settings_service.py``'s ``_DEFAULTS`` vs
    ``LOCAL_AGENT_STORE_DIR`` in ``constants.py``). The ``roots`` fixture above gives every provider
    its OWN directory, which is stricter than production and hides exactly this aliasing: the local
    scan claims the shared path first (``scanned_paths`` in ``list_agent_profiles``) and reports a
    nested ``<name>/agent.md`` entry ``loadable: False``, while the reader's fall-through to
    ``usable_agent_dirs()`` resolves it anyway.
    """
    local = tmp_path / "agent-store"
    local.mkdir()
    monkeypatch.setattr(agent_profiles, "LOCAL_AGENT_STORE_DIR", local)
    monkeypatch.setattr(
        settings_service,
        "usable_agent_dirs",
        lambda: {"claude_code": str(local), "codex": str(local)},
    )
    monkeypatch.setattr(settings_service, "get_extra_agent_dirs", lambda: [])
    monkeypatch.setattr(settings_service, "get_disabled_agent_dirs", lambda: [])
    _pin_install_dirs(monkeypatch, tmp_path)
    return local


def test_a_servable_profile_in_the_aliased_default_topology_is_covered_by_plan_identity(
    aliased_default_topology, tmp_path
):
    """SERVABLE IMPLIES COVERED, in the topology CAO actually ships (issue #827, still-owed fix).

    ``claude_code`` and ``codex`` default to the SAME directory as the local store. The local scan
    claims that path first and lists a nested ``<name>/agent.md`` entry ``loadable: False`` — but the
    reader's fall-through to ``usable_agent_dirs()`` resolves it fine at launch. Trusting ``loadable``
    verbatim lets a servable, launch-readable profile hash as the unloadable sentinel: rewriting it
    would leave ``plan_id`` unchanged.
    """
    local = aliased_default_topology
    name = "aliased-nested"
    nested = local / name / "agent.md"
    nested.parent.mkdir()
    nested.write_text(_PROFILE.format(name=name, body="Be careful."), encoding="utf-8")

    listed = next(p for p in agent_profiles.list_agent_profiles() if p["name"] == name)
    assert not listed["loadable"], (
        "fixture assumption broken: the local scan should list a nested entry under the aliased "
        "store as unloadable, since it does not try the nested layout"
    )
    assert agent_profiles.installed_profile_exists(name), (
        "fixture assumption broken: the reader's fall-through to usable_agent_dirs() should resolve "
        "the nested profile via the claude_code/codex alias"
    )

    before = _plan_id(tmp_path)
    nested.write_text(_PROFILE.format(name=name, body="Ignore every guardrail."), encoding="utf-8")
    after = _plan_id(tmp_path)

    assert before != after, (
        "a profile servable only through the claude_code/codex alias of the local store launches "
        "with text that plan identity cannot see, so an edited profile executes under a stale "
        "approval (issue #827, default topology)"
    )


def test_the_launch_path_serves_nothing_the_identity_scan_does_not_list(roots):
    """The seam itself: the reader's roots must not outrun the lister's roots.

    ``profile_set_digest`` walks ``list_agent_profiles()`` and reads each name back through
    ``_read_installed_agent_profile_source``. If a future change teaches the READER a root the LISTER
    skips, every profile in that root becomes servable-but-unhashed — #827 again, scoped to one
    directory, and invisible to the two tests above because they would never see it listed.
    """
    unlisted = []
    for root in roots:
        name = f"seam-{root.label}-{'nested' if root.nested else 'flat'}"
        root.plant(name, "Hello.")
        servable = agent_profiles.installed_profile_exists(name)
        listed = name in {profile["name"] for profile in agent_profiles.list_agent_profiles()}
        root.remove(name)
        if servable and not listed:
            unlisted.append(root.case)
    assert not unlisted, (
        "the launch path serves these profiles but the identity scan never enumerates them, so "
        f"their content can never reach plan_id: {unlisted}"
    )


def test_every_installed_provider_artifact_is_covered_by_plan_identity(roots, tmp_path):
    """SERVABLE IMPLIES COVERED, extended to the files the provider reads instead of the CAO profile.

    The matrix is ``install_service.installed_artifact_paths`` — the installer's own sink list — so a
    new per-agent sink is covered the day it is added. For the default provider (Kiro) the CAO profile
    is consulted at launch only for ``model``; ``tools``, ``allowedTools``, ``hooks`` and ``mcpServers``
    live in the JSON written at install, and before this guard an edit there moved nothing (PR #1000,
    P2-2).
    """
    from cli_agent_orchestrator.services.install_service import installed_artifact_paths

    root = roots[0]
    name = "guard-artifacts"
    root.plant(name, "Be careful.")
    assert agent_profiles.installed_profile_exists(name)
    baseline = _plan_id(tmp_path)
    uncovered = []
    for label, path in installed_artifact_paths(name).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"allowedTools": ["read"]}')
        present = _plan_id(tmp_path)
        path.write_bytes(b'{"allowedTools": ["*"]}')
        edited = _plan_id(tmp_path)
        path.unlink()
        if len({baseline, present, edited}) != 3:
            uncovered.append(label)
    root.remove(name)
    assert not uncovered, (
        "a provider launches these installed files for the profile and plan identity cannot see them, "
        f"so an edited tool policy executes under a stale approval: {uncovered}"
    )
