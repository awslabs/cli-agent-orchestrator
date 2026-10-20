"""Skill delivery: project plugin skills into CAO's existing global skill store.

This is the highest-consequence design decision in the feature, so the reasoning
is recorded here rather than only in the spec.

CAO delivers skills through **three** independent mechanisms and only one goes
through :func:`~cli_agent_orchestrator.utils.skills.list_skills`:

===============================  =========================================  ====================
Mechanism                        Reads from                                 Providers
===============================  =========================================  ====================
Runtime catalog                  ``list_skills()`` → ``_skill_search_dirs``  Claude Code, Codex,
                                                                            Kimi, Antigravity
Baked catalog (install time)     ``list_skills()``                          Copilot
Native, filesystem-direct        ``SKILLS_DIR`` **path, literally**         Kiro CLI, OpenCode
===============================  =========================================  ====================

The obvious approach — appending plugin roots to ``_skill_search_dirs()``
alongside ``get_extra_skill_dirs()`` — covers the first two rows with a
one-function change and **cannot** cover the third. Kiro receives only
``skill://`` globs rooted at ``SKILLS_DIR`` and OpenCode's ``skills`` entry is a
single symlink to ``SKILLS_DIR``; plugin skills stored anywhere else are
invisible to both, including CAO's own default provider.

Because these skills are materialized as *symlinks*, ``install_service`` emits a
single-level ``*/SKILL.md`` glob alongside the recursive one — ``**`` does not
have one agreed meaning for directory symlinks, and the stricter reading would
hide every projected skill from Kiro. See the comment at that emission site.

So each valid plugin skill is materialized **inside** ``SKILLS_DIR``::

    SKILLS_DIR/<skill-name>  ->  AGENT_PLUGINS_DIR/<plugin-name>/skills/<skill-name>

Zero provider changes. Every already-tested delivery path is inherited unmodified,
and the terminal-launch path gains no new filesystem scan — projected entries
land in a directory ``build_skill_catalog()`` already scans.

Projection is **derived state**, never a source of truth: it is rebuilt from
scratch from the installed set on every add/remove/update, and swept for
dangling links. The previous projection is read back from the install records'
``projected_skill_names``, which is what lets a rebuild notice that a
collision's winner changed, and what makes "which plugin provided this skill?"
answerable by reading records alone.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import shutil
import stat as stat_module
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Set, Tuple

from cli_agent_orchestrator.agent_plugins.models import Finding, PluginRecord, Severity
from cli_agent_orchestrator.agent_plugins.store import (
    InstalledPluginStore,
    PluginBusyError,
    PluginStoreError,
)
from cli_agent_orchestrator.constants import SKILLS_DIR

#: Provenance marker written inside every *copied* projection.
#:
#: A dot-file, which is what makes it invisible to every reader that matters:
#: ``utils/skills.py`` gates on ``SKILL.md``, Kiro's installer globs
#: ``*/SKILL.md``, and the plugin validator's ``_discover_skills`` skips
#: dot-entries. So it proves ownership without becoming part of the skill.
MARKER_FILENAME = ".cao-projection.json"
MARKER_FORMAT = 1
DIGEST_PREFIX = "sha256-tree-v1:"

logger = logging.getLogger(__name__)

PROJECTION_MODE_SYMLINK = "symlink"
PROJECTION_MODE_COPY = "copy"


@dataclass(frozen=True)
class ProjectionResult:
    """Outcome of one projection rebuild."""

    projected: Mapping[str, str] = field(default_factory=dict)
    """Mapping of projected skill name → owning plugin name."""

    findings: Tuple[Finding, ...] = ()
    mode: str = PROJECTION_MODE_SYMLINK
    swept: Tuple[str, ...] = ()
    """Skill names whose stale projected entries were removed."""


def _skills_dir(override: Optional[Path] = None) -> Path:
    return Path(override) if override is not None else SKILLS_DIR


class ProjectionClaimError(RuntimeError):
    """A plugin held the skill name but its claim could not be released.

    The third state review of revision 2 asked for. ``release_projection_claim``
    used to return ``None`` both for "no plugin held this name" and for "a plugin
    held it and the record write failed", the failure swallowed with a warning.
    The caller — ``cao skills add --force`` — could not tell the two apart, so on
    a failure it went on to unlink the projection and copy the user's directory
    into place while the record still claimed the name. The next rebuild's sweep
    then deleted that directory by name.

    The failure premise is real rather than theoretical: records live under the
    store's ``state_dir`` while projections live in ``SKILLS_DIR`` — two
    independent trees, so a full or read-only state volume fails the record write
    while the copy into the skill store succeeds.
    """


def release_projection_claim(
    skill_name: str,
    store: Optional[InstalledPluginStore] = None,
) -> Optional[str]:
    """Drop ``skill_name`` from whichever plugin record still claims it.

    Called when a user installs a skill of the same name over a projected one
    (``cao skills add <folder> --force``). Reproduced by review on #584: the
    previous projection was reconstructed from the records, so the record kept
    claiming a name the user now owned, and a later ``cao plugin remove``
    recursively deleted the *user's* directory while an intervening rebuild
    overwrote it with the plugin's copy.

    Transferring the claim fixes both halves with the machinery that already
    exists rather than a second ownership mechanism:

    * ``_sweep`` only removes names the *previous* projection owned, so a
      released name is never swept.
    * ``_preexisting_skill_names`` treats any unmanaged name in the skill store
      as pre-existing, so the next rebuild sees a user-owned skill and the plugin
      loses the collision with a ``SKIPPED`` finding — which is precisely the
      documented "a user-added skill always wins" rule, now true regardless of
      the order the two installs happened in.

    Three outcomes, distinguishable — this function is tri-state on purpose:

    * returns the plugin name — the claim was released and committed;
    * returns ``None`` — no installed plugin claimed the name, nothing to do;
    * raises :class:`ProjectionClaimError` — a plugin claimed it and the release
      could **not** be committed. The caller must abort whatever it was going to
      do next; proceeding is what deletes a user's skill directory later.

    The find-owner → write-remaining sequence runs inside the store lock (see
    :meth:`InstalledPluginStore.release_projected_name`) so it cannot revert a
    concurrent publish.
    """
    store = store or InstalledPluginStore()
    try:
        released = store.release_projected_name(skill_name)
    except PluginStoreError as exc:
        raise ProjectionClaimError(str(exc)) from exc
    except Exception as exc:  # pragma: no cover - unreadable state dir
        raise ProjectionClaimError(
            f"Could not release the projection claim on skill '{skill_name}': {exc}"
        ) from exc

    if released is not None:
        logger.info(
            "Skill '%s' is now user-owned; plugin '%s' released its projection claim",
            skill_name,
            released,
        )
    return released


def current_projection(store: Optional[InstalledPluginStore] = None) -> Dict[str, str]:
    """Snapshot the projection currently recorded in the install records.

    Callers that are about to *change* the installed set take this snapshot
    **first** and hand it back to :func:`rebuild_projection` as ``previous``.
    Without that, publishing a replacement record (or deleting one) erases the
    prior ``projected_skill_names`` before the rebuild can read them — which
    both loses the transition warning and, worse, makes the rebuild mistake
    CAO's own still-present projected entry for a pre-existing user skill and
    refuse to re-project it.
    """
    store = store or InstalledPluginStore()
    return _previous_projection(store.list_installed())


def projection_owner(
    skill_name: str,
    store: Optional[InstalledPluginStore] = None,
    *,
    skills_dir: Optional[Path] = None,
) -> Optional[str]:
    """The plugin that owns the projection at ``skill_name``, or ``None``.

    Answers the question ``cao skills remove`` has to ask before it deletes
    anything: is this entry plugin-owned content that belongs to
    ``cao plugin remove``, or the user's own skill?

    Two independent routes to "owned", and the asymmetry between them is the
    point:

    1. **A readable claim plus structural proof.** An install record still claims
       the name, *and* what is on disk is provably something the projection engine
       placed — :func:`_is_managed_projection`: a symlink resolving into the plugin
       store, a copy whose marker digest still verifies, or a copy byte-identical
       to the plugin's own source.
    2. **Conclusive structure alone**, when no readable record claims the name —
       :func:`_structural_owner`. A symlink resolving into the plugin store, or a
       verified marker, cannot be content the user authored, and either one also
       *names* the owning plugin from its store-relative path, so a refusal is
       still actionable without a record to read.

    A name claim alone deliberately does not qualify, in either route. The poisoned
    state ``test_claim_transfer`` describes — the record still claiming a name whose
    directory is now the user's, after a release that failed — would otherwise
    leave the user with a skill they could neither remove nor explain. That is the
    same rule the sweep applies before deleting; stating it once means the CLI and
    the sweep cannot drift into disagreeing about who owns a path.

    That citation **describes the state and is not a claim of coverage.**
    ``test_claim_transfer`` constructs the poisoned state genuinely, but drives
    ``_sweep`` with the claim supplied as an explicit ``previous=`` argument and
    never calls this function, so it guards the sweep's decision rather than this
    one. What covers **this** function is ``test/agent_plugins/
    test_projection_owner.py``, one test per branch, each seen to fail against a
    mutation that breaks the branch it covers; ``test_skills_cli_guards.py`` adds
    the end-to-end ``cao skills remove`` cases. The distinction matters because the
    two are one line apart in prose and a reader would otherwise credit the sweep's
    tests as this function's.

    **Route 2 exists because record readability used to be load-bearing, and was
    not allowed to be.** :meth:`InstalledPluginStore.list_installed` logs and skips
    a record whose JSON will not parse — deliberate, and *unchanged*, because
    ``cao plugin list`` and every rebuild must not fail over one corrupt record. But
    while ownership was derived from that call alone, an unparseable record hid its
    claim, this returned ``None``, and ``cao skills remove`` deleted a genuine
    projection and exited 0 reporting success (issue #797). The fix is confined to
    this predicate: it asks the filesystem a second, independent question rather
    than making the store stricter for consumers that have nothing to do with
    removal.
    """
    store = store or InstalledPluginStore()
    path = _skills_dir(skills_dir) / skill_name
    claimed = _previous_projection(store.list_installed()).get(skill_name)
    if not claimed:
        # No *readable* record claims the name, which is not the same fact as no
        # plugin owning it. Ask the disk directly.
        return _structural_owner(path, store)

    source = _recorded_source(store, {skill_name: claimed}, skill_name)
    if not _is_managed_projection(path, store, source=source):
        return None
    return claimed


def _structural_owner(path: Path, store: InstalledPluginStore) -> Optional[str]:
    """Name the plugin that owns ``path`` from the on-disk evidence alone.

    Used when no readable install record claims the name. Only evidence that is
    **conclusive by itself** counts, because there is no claim here to corroborate
    it and the answer authorises a refusal rather than a delete:

    * a symlink whose target resolves inside the plugin store *and still exists* —
      the projection engine is the only thing that creates those, so it cannot be
      the user's own skill. Requiring the target to exist keeps a dangling
      projection (a store mutated out of band) removable by the operator, which is
      what the dangling sweep would do anyway;
    * a directory carrying a marker that :func:`_verified_marker` accepts — bound
      to this path's name, sourced inside the plugin store, and with a content
      digest that still matches, so an edited copy is not covered.

    The adoption rule from :func:`_is_managed_projection` is deliberately **not**
    reachable here: it needs a ``source``, which needs a plugin name, which is
    exactly what a missing record denies us. An unmarked copy therefore stays
    removable, as it was before.

    The owning plugin name is the first path segment under the plugin store root,
    which is the store's own layout (``<plugins_dir>/<plugin>/skills/<skill>``) and
    needs no record to read. Returns ``None`` when the evidence is absent or the
    segment is not a plugin directory.
    """
    if path.is_symlink():
        try:
            resolved = os.path.realpath(path)
        except OSError:  # pragma: no cover - exotic FS failure
            return None
        if not _within(resolved, store.plugins_dir) or not os.path.exists(resolved):
            return None
        return _plugin_name_for_store_path(resolved, store)

    if not path.is_dir():
        # A regular file, socket or device. CAO never projects one of these.
        return None

    marker = _verified_marker(path, store)
    if marker is None:
        return None
    source = marker.get("source")
    if not isinstance(source, str):  # pragma: no cover - _verified_marker already checked
        return None
    return _plugin_name_for_store_path(os.path.realpath(source), store)


def _plugin_name_for_store_path(candidate: str, store: InstalledPluginStore) -> Optional[str]:
    """The installed plugin directory ``candidate`` lies under, if any.

    ``candidate`` must already be a realpath known to be inside the store (see
    :func:`_within`); this only splits off the owning directory name and confirms
    it really is a plugin root. Dot-prefixed segments are rejected because the
    store keeps its own bookkeeping inside ``plugins_dir`` — ``.state`` holds the
    records, and a failed force-update can leave a ``.<name>.replaced.<pid>``
    tree — and neither is a plugin an operator could pass to ``cao plugin remove``.
    """
    root_real = os.path.realpath(store.plugins_dir)
    try:
        relative = os.path.relpath(candidate, root_real)
    except ValueError:  # pragma: no cover - different drives, Windows only
        return None
    first = relative.split(os.sep)[0]
    if not first or first.startswith(".") or first == os.pardir:
        return None
    try:
        root = store.plugin_root(first)
    except ValueError:  # pragma: no cover - guarded by the checks above
        return None
    return first if root.is_dir() else None


def rebuild_projection(
    store: Optional[InstalledPluginStore] = None,
    *,
    skills_dir: Optional[Path] = None,
    mode: Optional[str] = None,
    previous: Optional[Mapping[str, str]] = None,
) -> ProjectionResult:
    """Rebuild the whole projection as a pure function of the installed set.

    Rebuilt from scratch rather than incrementally patched (Requirement 13.3):
    an incremental patch would make the result depend on the sequence of
    operations that produced it, which is exactly what the deterministic
    collision rule exists to prevent.

    Never raises. Every filesystem step is best-effort with a finding, because
    this runs inside ``cao plugin add/remove``, ``cao plugin list``, and the API
    — none of which may fail an operator's whole command over one unwritable
    link.
    """
    store = store or InstalledPluginStore()
    target_dir = _skills_dir(skills_dir)
    findings: List[Finding] = []

    # Clear staged trees a killed process left behind, before this rebuild stages
    # its own (review 4 item 2 on #584). Safe here specifically because every
    # caller of `rebuild_projection` holds the lifecycle lock (item 1), so this
    # cannot delete a staging directory another operation is actively filling --
    # which is why item 1 was fixed first.
    clear_staging_leftovers(target_dir)

    records = store.list_installed()
    prior = dict(previous) if previous is not None else _previous_projection(records)

    # One digest cache for the whole rebuild: election, materialization and sweep
    # each ask about the same directories, and hashing a skill tree three times
    # per rebuild would be a real cost on a large skill store. Scoped to this call
    # so it can never serve a stale answer across rebuilds.
    digest_cache: Dict[str, str] = {}

    winners, collision_findings = _elect_winners(store, records, target_dir, prior, digest_cache)
    findings.extend(collision_findings)

    resolved_mode = _resolve_mode(mode)
    materialized, mode_used, material_findings = _materialize(
        store, winners, target_dir, resolved_mode, cache=digest_cache
    )
    findings.extend(material_findings)

    # No cache for the sweep, deliberately: materialization just rewrote some of
    # these trees, so a digest computed before that write would be a stale answer
    # to "are these bytes still ours". `_sweep` therefore takes no cache at all
    # rather than a parameter every caller has to remember to pass as None.
    swept, sweep_findings = _sweep(store, target_dir, prior, materialized)
    findings.extend(sweep_findings)

    findings.extend(_transition_findings(prior, materialized))
    _write_back(store, records, materialized)

    return ProjectionResult(
        projected=materialized,
        findings=tuple(findings),
        mode=mode_used,
        swept=tuple(swept),
    )


def _resolve_mode(explicit: Optional[str]) -> str:
    if explicit in (PROJECTION_MODE_SYMLINK, PROJECTION_MODE_COPY):
        return explicit
    try:
        from cli_agent_orchestrator.services.settings_service import get_skill_projection_mode

        return get_skill_projection_mode()
    except Exception:  # pragma: no cover - unreadable settings must not block
        return PROJECTION_MODE_SYMLINK


def _previous_projection(records: List[PluginRecord]) -> Dict[str, str]:
    """Reconstruct the previous projection from the install records.

    The records are the persisted truth about what each plugin actually
    projected, so no separate projection state file is needed — and keeping one
    source of truth is what guarantees a record-based ownership answer and the
    on-disk projection cannot disagree.
    """
    previous: Dict[str, str] = {}
    for record in sorted(records, key=lambda r: r.name):
        for skill_name in record.projected_skill_names:
            previous.setdefault(skill_name, record.name)
    return previous


def _preexisting_skill_names(
    store: InstalledPluginStore,
    target_dir: Path,
    previous: Mapping[str, str],
    cache: Optional[Dict[str, str]] = None,
) -> Set[str]:
    """Names already owned by a built-in or user-added skill.

    Two sources, both genuinely "pre-existing" from a plugin's point of view:

    1. Entries in ``SKILLS_DIR`` that are **not** CAO-managed projections —
       seeded built-ins and anything installed by ``cao skills add``.
    2. Skills reachable through ``skills.extra_dirs``. These are user-added too,
       and ``_skill_search_dirs()`` searches ``SKILLS_DIR`` *first* — so
       projecting over one of their names would silently shadow it, which is
       precisely what Requirement 14.1 forbids and what makes the reachable-set
       union in Requirement 13.2 a true union.

    "CAO-managed" is decided by the one structural predicate
    (:func:`_is_managed_projection`), not by a name carried over from the previous
    projection. Review 3 on #584: a prior claim on a name says nothing about what
    is at that name *now*, so a copied projection the user has since edited is
    correctly reported here as pre-existing (they own it) while an untouched or
    marker-verified one is still recognised as CAO's. ``previous`` is passed rather
    than a set of names because the adoption rule needs the *source* the previous
    owner would have copied from.
    """
    names: Set[str] = set()

    if target_dir.is_dir():
        try:
            for item in target_dir.iterdir():
                if item.name.startswith("."):
                    continue
                if _is_managed_projection(
                    item,
                    store,
                    source=_recorded_source(store, previous, item.name),
                    cache=cache,
                ):
                    continue  # our own projection, not a pre-existing skill
                if item.is_dir() and (item / "SKILL.md").is_file():
                    names.add(item.name)
        except OSError as exc:  # pragma: no cover - unreadable skill store
            logger.warning("Could not enumerate the skill store at %s: %s", target_dir, exc)

    try:
        from cli_agent_orchestrator.services.settings_service import get_extra_skill_dirs

        for extra in get_extra_skill_dirs():
            extra_path = Path(extra)
            if not extra_path.is_dir():
                continue
            for item in extra_path.iterdir():
                if item.is_dir() and (item / "SKILL.md").is_file():
                    names.add(item.name)
    except Exception as exc:  # pragma: no cover - settings/FS best effort
        logger.warning("Could not enumerate extra skill dirs: %s", exc)

    return names


def _elect_winners(
    store: InstalledPluginStore,
    records: List[PluginRecord],
    target_dir: Path,
    previous: Mapping[str, str],
    cache: Optional[Dict[str, str]] = None,
) -> Tuple[Dict[str, str], List[Finding]]:
    """Decide which plugin owns each contested skill name.

    Two rules, in order:

    1. **A pre-existing built-in or user-added skill always wins.** The plugin's
       skill is skipped with a finding and the pre-existing skill keeps
       resolving exactly as before.
    2. **Among plugins, the lexicographically smallest manifest name wins.**
       Plugin names are unique across the installed set and persisted in the
       install record, so this is a total order over *persisted state* — never
       over ``os.scandir`` results, ``mtime``, or install order.

    ``installed_at`` is deliberately **not** the key even though it is also
    persisted: it encodes install *order*, so installing A-then-B and B-then-A
    would elect different winners from the same final installed set, and
    same-second installs would tie and fall back to iteration order anyway.
    Ordering on ``name`` makes the projection a pure function of *which* plugins
    are installed, independent of *how* they got there (property P8).
    """
    findings: List[Finding] = []

    claims: Dict[str, List[str]] = {}
    for record in sorted(records, key=lambda r: r.name):
        for skill_name in sorted(set(record.skill_names)):
            claims.setdefault(skill_name, []).append(record.name)

    preexisting = _preexisting_skill_names(store, target_dir, previous, cache)

    winners: Dict[str, str] = {}
    for skill_name in sorted(claims):
        claimants = sorted(claims[skill_name])

        if skill_name in preexisting:
            for claimant in claimants:
                findings.append(
                    Finding(
                        severity=Severity.SKIPPED,
                        code="projection.preexisting_collision",
                        spec_ref="CAO policy",
                        message=(
                            f"Skill '{skill_name}' from plugin '{claimant}' was not projected: "
                            f"a built-in or user-added skill of that name already exists and "
                            f"keeps resolving"
                        ),
                        path=skill_name,
                    )
                )
            continue

        winner = claimants[0]
        winners[skill_name] = winner
        for loser in claimants[1:]:
            findings.append(
                Finding(
                    severity=Severity.SKIPPED,
                    code="projection.plugin_collision",
                    spec_ref="CAO policy",
                    message=(
                        f"Skill '{skill_name}' from plugin '{loser}' was not projected: "
                        f"plugin '{winner}' provides the same skill name and wins "
                        f"(lexicographically smallest plugin name)"
                    ),
                    path=skill_name,
                )
            )

    return winners, findings


def _transition_findings(
    previous: Mapping[str, str],
    current: Mapping[str, str],
) -> List[Finding]:
    """Warn when a rebuild reassigns an already-projected skill's winner.

    Requirement 13.3 (projection is a pure function of the installed set) plus
    the lexicographic rule mean installing a new, lexicographically-earlier
    plugin can reassign an *existing* projection on the very next rebuild — a
    previously-working skill silently changes source. A ``SKIPPED`` finding on
    the loser is not sufficient signal for a transition that changes which
    plugin's content an agent now receives, so the change itself is reported.
    """
    findings: List[Finding] = []
    for skill_name in sorted(current):
        before = previous.get(skill_name)
        after = current[skill_name]
        if before is not None and before != after:
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    code="projection.winner_changed",
                    spec_ref="CAO policy",
                    message=(
                        f"Skill '{skill_name}' is now provided by plugin '{after}'; "
                        f"it was previously provided by plugin '{before}'"
                    ),
                    path=skill_name,
                )
            )
    return findings


def _materialize(
    store: InstalledPluginStore,
    winners: Mapping[str, str],
    target_dir: Path,
    mode: str,
    cache: Optional[Dict[str, str]] = None,
) -> Tuple[Dict[str, str], str, List[Finding]]:
    """Create the projected entries, falling back to copy mode when needed."""
    findings: List[Finding] = []
    materialized: Dict[str, str] = {}
    effective_mode = mode
    fallback_reported = False

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        findings.append(
            Finding(
                severity=Severity.SKIPPED,
                code="projection.store_unavailable",
                spec_ref="CAO policy",
                message=f"Skill store {target_dir} is not writable: {exc}",
            )
        )
        return materialized, effective_mode, findings

    for skill_name in sorted(winners):
        plugin_name = winners[skill_name]
        try:
            source = store.plugin_root(plugin_name) / "skills" / skill_name
        except ValueError as exc:
            findings.append(
                Finding(
                    severity=Severity.SKIPPED,
                    code="projection.source_missing",
                    spec_ref="CAO policy",
                    message=f"Skill '{skill_name}' has an unusable owning plugin name: {exc}",
                    path=skill_name,
                )
            )
            continue

        if not source.is_dir():
            findings.append(
                Finding(
                    severity=Severity.SKIPPED,
                    code="projection.source_missing",
                    spec_ref="CAO policy",
                    message=(
                        f"Skill '{skill_name}' is recorded for plugin '{plugin_name}' but "
                        f"{source} is not a directory; nothing was projected"
                    ),
                    path=skill_name,
                )
            )
            continue

        link_path = target_dir / skill_name
        # `_place` removes whatever occupies the target before writing. That is
        # correct for an entry the engine placed and catastrophic for one it did
        # not. The guard used to be "did a previous projection claim this name",
        # which review 3 on #584 rejected for the same reason it rejected the
        # sweep's version: a name claim cannot prove the current *bytes* are CAO's,
        # so an in-place edit of a copied skill was silently overwritten. It is now
        # the same structural predicate the sweep uses, so a marker-verified copy
        # and a store symlink stay replaceable — which is what keeps a plugin
        # upgrade and a copy-to-symlink migration working — while a regular file,
        # a foreign symlink, an unmarked directory and an edited one are refused.
        if (link_path.is_symlink() or link_path.exists()) and not _is_managed_projection(
            link_path, store, source=source, cache=cache
        ):
            findings.append(
                Finding(
                    severity=Severity.SKIPPED,
                    code="projection.target_not_ours",
                    spec_ref="CAO policy",
                    message=(
                        f"Skill '{skill_name}' from plugin '{plugin_name}' was not "
                        f"projected: {link_path} is {_describe_unmanaged(link_path)}, "
                        f"so it was left untouched."
                    ),
                    path=skill_name,
                )
            )
            continue

        ok, used_fallback, error = _place(
            link_path,
            source,
            effective_mode,
            plugin_name=plugin_name,
            skill_name=skill_name,
        )

        if used_fallback:
            effective_mode = PROJECTION_MODE_COPY
            if not fallback_reported:
                fallback_reported = True
                findings.append(
                    Finding(
                        severity=Severity.WARNING,
                        code="projection.copy_fallback",
                        spec_ref="CAO policy",
                        message=(
                            "Symlink creation is unsupported in this environment; plugin "
                            "skills were copied into the skill store instead. Set "
                            "skills.projection_mode to 'copy' in settings.json to make "
                            "this explicit."
                        ),
                    )
                )

        if ok:
            materialized[skill_name] = plugin_name
        else:
            findings.append(
                Finding(
                    severity=Severity.SKIPPED,
                    code="projection.write_failed",
                    spec_ref="CAO policy",
                    message=(
                        f"Skill '{skill_name}' from plugin '{plugin_name}' could not be "
                        f"projected into the skill store: {error}"
                    ),
                    path=skill_name,
                )
            )

    return materialized, effective_mode, findings


def _place(
    link_path: Path,
    source: Path,
    mode: str,
    *,
    plugin_name: Optional[str] = None,
    skill_name: Optional[str] = None,
) -> Tuple[bool, bool, Optional[str]]:
    """Put one projected skill at ``link_path``.

    Returns ``(ok, used_copy_fallback, error)``.
    """
    if mode == PROJECTION_MODE_SYMLINK:
        try:
            if link_path.is_symlink():
                if os.path.realpath(link_path) == os.path.realpath(source):
                    return True, False, None  # already correct — idempotent
                link_path.unlink()
            elif link_path.exists():
                # A managed copy left over from copy mode, or a stale directory.
                _remove_quiet(link_path)
            link_path.symlink_to(source, target_is_directory=True)
            return True, False, None
        except (OSError, NotImplementedError) as exc:
            # Windows without Developer Mode or elevation raises here. Fall
            # back rather than failing the install (Requirement 13.4).
            logger.warning("Symlink projection failed for %s, copying instead: %s", link_path, exc)
            ok, error = _copy_into(
                link_path, source, plugin_name=plugin_name, skill_name=skill_name
            )
            return ok, True, error

    ok, error = _copy_into(link_path, source, plugin_name=plugin_name, skill_name=skill_name)
    return ok, False, error


#: Where a copy is assembled before it is published. Dot-prefixed so it is
#: obviously not a skill, and deliberately NOT under ``SKILLS_DIR``.
STAGING_DIRNAME = ".cao-staging"


def _staging_root(target_dir: Optional[Path] = None) -> Path:
    """The staging directory: a SIBLING of the skills directory being projected.

    Not *under* the skills directory, on purpose (review 4 item 2, spec §7.1). Kiro
    CLI is handed ``skill://{SKILLS_DIR}/**/SKILL.md`` and expands it itself; the
    shipped binary links ``globset`` (which has no leading-dot option at all) and
    ``walkdir`` (which does no hidden-entry filtering — that lives in the ``ignore``
    crate, which is not linked). So a dot-prefixed staging directory under the
    skills directory WOULD be matched, and a half-copied skill could be loaded
    mid-projection. Both emitted globs are rooted at the skills directory, so a
    sibling cannot be matched by either, under any dot policy, on any Kiro version.

    Derived from the TARGET rather than from ``CAO_HOME_DIR``, which matters twice.
    ``os.rename`` requires the same *filesystem*, not the same directory — a
    sibling of the destination is on the destination's filesystem by construction,
    whereas a fixed ``CAO_HOME_DIR`` would raise ``EXDEV`` for any projection
    target on another mount. And it means redirecting the projection target
    redirects staging with it, so nothing has to remember to patch a second global.
    """
    base = (target_dir or SKILLS_DIR).parent
    return base / STAGING_DIRNAME


def clear_staging_leftovers(target_dir: Optional[Path] = None) -> None:
    """Remove staged trees a killed process left behind. Never raises.

    Called at the start of ``rebuild_projection``, which holds the lifecycle lock,
    so this cannot race a staging directory another operation is actively using.
    """
    root = _staging_root(target_dir)
    try:
        if not root.is_dir():
            return
        for entry in root.iterdir():
            shutil.rmtree(entry, ignore_errors=True)
    except OSError as exc:  # pragma: no cover - best-effort tidy-up
        logger.warning("Could not clear projection staging area: %s", exc)


def _copy_into(
    link_path: Path,
    source: Path,
    *,
    plugin_name: Optional[str] = None,
    skill_name: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """Publish a fresh copy of ``source`` at ``link_path``, atomically.

    The marker is what makes a copy-mode projection *provably* CAO's later; see
    :func:`_write_marker`. Written after ``copytree`` and unconditionally, so a
    plugin that packaged a stale marker inside its own skill directory cannot
    smuggle one in — CAO always overwrites with the digest it just computed.

    STAGED, then renamed into place (review 4 item 2 on #584). This previously
    removed ``link_path`` and copied *directly* over it, so any failure partway --
    a dangling nested link makes ``copytree`` raise mid-tree -- left a live
    ``SKILL.md`` at the projected path with no marker. Unmarked content is
    indistinguishable from a skill the operator created, so every retry then
    refused to touch it and the plugin silently never projected. The reviewer's
    "retry misclassifies the debris as a user-owned collision" is exactly that.

    With staging, ``link_path`` is only ever written by ``os.rename`` of a
    *complete* tree, and a failure discards the staging directory and leaves the
    previous state untouched.
    """
    staging_root = _staging_root(link_path.parent)
    staged: Optional[Path] = None
    try:
        staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        staged = staging_root / f"{link_path.name}.{os.getpid()}.{secrets.token_hex(4)}"
        shutil.rmtree(staged, ignore_errors=True)
        shutil.copytree(source, staged, symlinks=False)
        if plugin_name is not None and skill_name is not None:
            _write_marker(staged, plugin_name, skill_name, source)
        # Only now is the projected path touched. The remove/rename pair is not
        # itself atomic, but the window contains no *partial* tree: either the old
        # projection or the new one, never half of either.
        _remove_quiet(link_path)
        os.rename(staged, link_path)
        staged = None
    except OSError as exc:
        return False, str(exc)
    finally:
        if staged is not None:
            shutil.rmtree(staged, ignore_errors=True)
    return True, None


def _no_follow_opener(path: str, flags: int) -> int:
    """``open()`` opener that refuses a symlink at the final component.

    ``_tree_digest`` has already classified the entry with ``os.lstat``, so this
    closes the TOCTOU window between that check and the open.
    """
    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0))


def _tree_digest(root: Path, _cache: Optional[Dict[str, str]] = None) -> Optional[str]:
    """A content digest over a projected directory tree, excluding the marker.

    Sorted relative paths plus entry kind, size and file bytes — so a rename, a
    truncation, an added file and an edited byte all change it.

    **Nothing is followed and nothing but regular files is opened.** ``os.walk``
    runs with ``followlinks=False``, which only covers *directory* symlinks; every
    entry is then classified with ``os.lstat``. A symlink is hashed by the text of
    its target, and anything that is neither a regular file nor a symlink (a FIFO,
    socket or device) contributes its kind and nothing more. That matters because
    this also digests plugin **source** trees for the adoption rule: a plugin is
    free to ship a symlink or a FIFO, and ``open()`` on a FIFO would block the
    projection forever while a symlink would silently hash a file outside the
    plugin.

    The marker file is excluded because it contains the digest; including it would
    make the value unverifiable by construction.

    Returns ``None`` when the tree cannot be read, which callers must treat as
    "not proven ours" rather than as a match.
    """
    key = str(root)
    if _cache is not None and key in _cache:
        return _cache[key]
    digest = hashlib.sha256()
    try:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames.sort()
            rel_dir = os.path.relpath(dirpath, root)
            for name in sorted(dirnames):
                digest.update(f"d\0{os.path.join(rel_dir, name)}\0".encode())
            for name in sorted(filenames):
                if rel_dir == "." and name == MARKER_FILENAME:
                    continue
                full = Path(dirpath) / name
                rel = os.path.join(rel_dir, name)
                info = os.lstat(full)
                if stat_module.S_ISLNK(info.st_mode):
                    # Hashed by target text, never dereferenced.
                    digest.update(f"l\0{rel}\0".encode())
                    digest.update(os.readlink(full).encode())
                    continue
                if not stat_module.S_ISREG(info.st_mode):
                    # FIFO, socket, device: record that something of this kind is
                    # here and move on. Opening it could block indefinitely.
                    digest.update(f"s\0{rel}\0{stat_module.S_IFMT(info.st_mode)}\0".encode())
                    continue
                digest.update(f"f\0{rel}\0{info.st_size}\0".encode())
                with open(full, "rb", opener=_no_follow_opener) as handle:
                    for chunk in iter(lambda: handle.read(65536), b""):
                        digest.update(chunk)
    except OSError as exc:
        logger.warning("Could not digest the projected tree at %s: %s", root, exc)
        return None
    value = f"{DIGEST_PREFIX}{digest.hexdigest()}"
    if _cache is not None:
        _cache[key] = value
    return value


def _write_marker(link_path: Path, plugin_name: str, skill_name: str, source: Path) -> None:
    """Record that CAO placed this copied tree, and what its bytes were.

    Best effort: a marker CAO cannot write leaves an *unmarked* copy, which the
    adoption rule in :func:`_is_managed_projection` recovers as long as the copy
    is still byte-identical to its source. Failing the projection over a marker
    would be worse than a projection CAO has to re-prove later.
    """
    digest = _tree_digest(link_path)
    if digest is None:  # pragma: no cover - unreadable tree we just wrote
        return
    payload = {
        "format": MARKER_FORMAT,
        "plugin": plugin_name,
        "skill": skill_name,
        # The LOGICAL projected path: the resolved *parent* plus the projected
        # skill's own name. Not `realpath(source)`.
        #
        # §4.1 permits `skills/<name>` to itself be a symlink resolving inside the
        # plugin root, so `skills/inspection -> ../shared/implementation` is a valid
        # package. Resolving the whole path recorded basename `implementation` while
        # the projected skill is `inspection`, and `_verified_marker` binds a marker
        # to its directory by requiring that basename to match -- so CAO rejected
        # its own unchanged marker, and uninstall stranded the copy in the shared
        # skill store with `projection.sweep_skipped_unmanaged`.
        #
        # The parent is still resolved, which is the whole reason resolution was
        # introduced: `_verified_marker` containment-checks this against a realpath'd
        # plugin store, and recording an unresolved path made every marker fail to
        # verify on any host whose CAO home has a symlink component -- macOS `/tmp`
        # -> `/private/tmp`, or a symlinked `$HOME`. Resolving the parent answers
        # containment exactly as well, because a final-component symlink cannot move
        # a path out of its own directory.
        "source": os.path.join(os.path.realpath(source.parent), skill_name),
        "digest": digest,
    }
    try:
        (link_path / MARKER_FILENAME).write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("Could not write the projection marker at %s: %s", link_path, exc)


def _verified_marker(
    path: Path, store: InstalledPluginStore, cache: Optional[Dict[str, str]] = None
) -> Optional[Dict[str, object]]:
    """The marker at ``path`` if it is well-formed, *bound to this path*, and current.

    Four checks, all required:

    1. ``format`` is one CAO wrote.
    2. ``source``'s **parent directory** lies inside the plugin store — a marker
       naming somewhere else was not written by a projection. Compared after
       ``realpath`` on both sides, or a symlink component anywhere in the CAO home
       breaks every marker.
    3. **The marker is bound to the directory holding it**: both the recorded
       ``skill`` and the last segment of ``source`` must equal ``path.name``.
       Without this a marker is a bearer token — copying a marked projection to
       another name carries ownership with it, and a later plugin claiming that
       name would replace the user's directory with no finding at all. That is
       *worse* than the pre-marker behaviour, which refused such a directory.
    4. The recomputed digest equals the recorded one, which is what makes an
       in-place edit visible: without it a marker would be a name claim again,
       exactly the thing this review rejected.

    Checks 2 and 3 are split across the parent and the last segment on purpose, and
    that split is load-bearing. Realpath'ing the whole recorded path re-follows a
    permitted renaming ``skills/<name>`` symlink (§4.1) and yields the *target's*
    basename, so ``skills/inspection -> ../shared/implementation`` made check 3
    reject CAO's own unchanged marker. Resolving only the parent answers containment
    just as well — a final-component symlink cannot move a path out of its own
    directory — while leaving the binding to compare the logical names.

    ``plugin`` is read but deliberately **not** matched against the caller's
    expectation: a legitimate winner transition hands a name from one plugin to
    another, and requiring it to match would refuse that. The transition never
    changes the *skill* name, which is why binding on that costs nothing.
    """
    marker_path = path / MARKER_FILENAME
    if not marker_path.is_file():
        return None
    try:
        loaded = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(loaded, dict) or loaded.get("format") != MARKER_FORMAT:
        return None
    source = loaded.get("source")
    if not isinstance(source, str):
        return None
    # Containment is asked of the PARENT directory, and the basename is compared
    # LEXICALLY. Realpath'ing the whole recorded path would re-follow a permitted
    # renaming `skills/<name>` symlink and hand back the target's basename, which is
    # the defect this pair of checks used to have. A final-component symlink cannot
    # move a path out of its own directory, so resolving the parent answers
    # containment exactly as well -- and still defeats a `..` escape, whose parent
    # resolves outside the store.
    if not _within(os.path.realpath(os.path.dirname(source)), store.plugins_dir):
        return None
    if loaded.get("skill") != path.name or os.path.basename(source) != path.name:
        return None
    recorded = loaded.get("digest")
    if not isinstance(recorded, str) or recorded != _tree_digest(path, cache):
        return None
    return loaded


def _within(candidate: str, root: Path) -> bool:
    """Whether ``candidate`` is lexically inside ``root``, after realpath on root."""
    root_real = os.path.realpath(root)
    return candidate == root_real or candidate.startswith(root_real + os.sep)


def _is_managed_projection(
    path: Path,
    store: InstalledPluginStore,
    *,
    source: Optional[Path] = None,
    cache: Optional[Dict[str, str]] = None,
) -> bool:
    """Whether ``path`` is still an entry the projection engine placed.

    A name match must never be sufficient to delete or replace what is on disk.
    Three ways a path is provably CAO's, and nothing else counts:

    1. **A symlink resolving inside the plugin store.** Structurally ours, and the
       same test ``_preexisting_skill_names`` trusts.
    2. **A directory carrying a verified marker** — well-formed, sourced inside
       the plugin store, and with a content digest that still matches
       (:func:`_verified_marker`).
    3. **A directory byte-identical to the ``source`` the caller names.** The
       adoption rule: identity with the plugin's own bytes is exact proof, and it
       is the upgrade path for copy-mode projections written before markers
       existed. Without it, every pre-marker copy would become permanently
       unmanaged.

    Reported by review 3 on #584: this used to answer ``True`` for **any** regular
    file (a stray file "may be removed by name") and for **any** directory in copy
    mode. Both are name claims wearing a structural disguise. A regular file is
    now *never* ours — CAO projects symlinks and directories, never files, so a
    file at a projected name is by construction something else, and the user keeps
    it. Anything that is not a directory is treated the same way, which also
    covers sockets and devices.

    The remaining residual is over-preservation, never data loss: an unmarked,
    non-identical copy is refused and reported, so the user keeps the directory
    and the plugin loses the name.
    """
    if path.is_symlink():
        try:
            resolved = os.path.realpath(path)
        except OSError:  # pragma: no cover - exotic FS failure
            return False
        return _within(resolved, store.plugins_dir)
    if not path.is_dir():
        # A regular file, socket or device. CAO never places one of these at a
        # projected name, so it cannot be ours no matter who claimed the name.
        return False
    if _verified_marker(path, store, cache) is not None:
        return True
    if source is not None and source.is_dir():
        theirs = _tree_digest(path, cache)
        return theirs is not None and theirs == _tree_digest(source, cache)
    return False


def _sweep(
    store: InstalledPluginStore,
    target_dir: Path,
    previous: Mapping[str, str],
    current: Mapping[str, str],
) -> Tuple[List[str], List[Finding]]:
    """Remove stale and dangling projected entries. Never raises.

    Two cases are swept:

    * an entry the previous projection owned that the current one does not
      (the plugin was removed, or lost a collision) **and that is still a
      CAO-managed projection** — see :func:`_is_managed_projection`; a stale
      claim alone is not licence to delete, and
    * any symlink in the skill store that points into ``AGENT_PLUGINS_DIR`` but
      whose target no longer exists — a projection left behind by a store
      mutated out of band, or by a removal that raced a launch.

    Returns the swept names and any findings raised for entries it declined to
    remove, so a skip is visible to the operator instead of silent.

    This runs on every rebuild and on ``cao plugin list``, both of which can be
    concurrent with ``terminal_service.create_terminal``. It therefore uses the
    same never-raise discipline the delivery paths already do: a link it cannot
    remove is logged at warning level and the sweep continues. The read paths
    tolerate a broken link on their own — ``list_skills()`` gates on
    ``is_dir()`` and ``SKILL.md is_file()``, and both are ``False`` (not an
    exception) for a symlink whose target is gone — so a link that survives the
    sweep is simply not enumerated.
    """
    swept: List[str] = []
    findings: List[Finding] = []

    for skill_name in sorted(set(previous) - set(current)):
        path = target_dir / skill_name
        if not path.is_symlink() and not path.exists():
            continue
        # The plugin that owned the name last time still names the bytes CAO
        # would have copied, which is what lets an unmarked pre-fix copy be
        # adopted instead of stranded. Absent (uninstalled plugin) is fine — the
        # marker path does not need it.
        source = _recorded_source(store, previous, skill_name)
        # No digest cache here on purpose -- see the call site in
        # `rebuild_projection`: materialization may have just rewritten this tree.
        if not _is_managed_projection(path, store, source=source):
            found = _describe_unmanaged(path)
            logger.warning(
                "Not sweeping '%s': the previous projection claimed it, but what is "
                "on disk is %s. Leaving it in place.",
                path,
                found,
            )
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    code="projection.sweep_skipped_unmanaged",
                    spec_ref="CAO policy",
                    message=(
                        f"Skill '{skill_name}' was claimed by a previous projection but "
                        f"what is on disk is {found}, so it was left in place. It is now "
                        f"treated as a user-owned skill."
                    ),
                    path=str(path),
                )
            )
            continue
        if _remove_quiet(path):
            swept.append(skill_name)

    try:
        plugins_real = os.path.realpath(store.plugins_dir)
        if target_dir.is_dir():
            for item in sorted(target_dir.iterdir(), key=lambda p: p.name):
                if not item.is_symlink():
                    continue
                try:
                    raw_target = os.path.realpath(item)
                except OSError:  # pragma: no cover - exotic FS failure
                    continue
                inside_store = raw_target == plugins_real or raw_target.startswith(
                    plugins_real + os.sep
                )
                if inside_store and not item.exists():
                    if _remove_quiet(item):
                        swept.append(item.name)
    except OSError as exc:  # pragma: no cover - unreadable skill store
        logger.warning("Dangling-projection sweep could not scan %s: %s", target_dir, exc)

    return swept, findings


def _recorded_source(
    store: InstalledPluginStore, previous: Mapping[str, str], skill_name: str
) -> Optional[Path]:
    """Where the plugin that last owned ``skill_name`` keeps that skill, if still installed."""
    plugin_name = (previous or {}).get(skill_name)
    if not plugin_name:
        return None
    try:
        source = store.plugin_root(plugin_name) / "skills" / skill_name
    except (ValueError, OSError):
        # `plugin_root` validates the directory name and raises `ValueError` on an
        # unsafe one, which a hand-edited or corrupt record can carry. No source
        # means "cannot prove ownership by adoption", which is the safe answer.
        return None
    return source if source.is_dir() else None


def _describe_unmanaged(path: Path) -> str:
    """What was found at a projected name, for the operator-facing finding.

    "Not a CAO-managed projection" told an operator nothing actionable. The two
    cases have different remedies: a regular file was never CAO's at all, whereas
    an unverified directory usually means someone edited a copied skill in place
    and now owns it.
    """
    if path.is_symlink():
        return "a symlink pointing outside CAO's plugin store"
    if not path.is_dir():
        return "a regular file"
    return "a directory CAO did not place, or whose contents have changed since it did"


def _remove_quiet(path: Path) -> bool:
    """Delete a projected entry best-effort. Logs and continues on failure."""
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
            return True
        if path.is_dir():
            shutil.rmtree(path)
            return True
    except OSError as exc:
        # Permissions, a busy handle, a copy-mode directory on Windows. Logged
        # and skipped — the sweep must not halt, and must never raise into
        # terminal creation.
        logger.warning("Could not remove projected skill entry '%s': %s", path, exc)
    return False


def _write_back(
    store: InstalledPluginStore,
    records: List[PluginRecord],
    materialized: Mapping[str, str],
) -> None:
    """Update each record's ``projected_skill_names`` to match reality.

    Keeping the records truthful is what makes ownership answerable by a plain
    record lookup and what gives the next rebuild its "previous winner" for the
    transition warning.

    Delegated to ``store.update_projected_names`` rather than rebuilding a whole
    record here: ``records`` is the snapshot taken at the *start* of the rebuild,
    and writing a full record from it reverted whatever a concurrent publish or
    removal committed during the slow materialization in between. The primitive
    re-reads under the store lock and patches only this one field, skipping a
    plugin that has since been uninstalled instead of resurrecting its record.
    """
    for record in records:
        owned = tuple(
            sorted(name for name, plugin in materialized.items() if plugin == record.name)
        )
        try:
            store.update_projected_names(record.name, owned)
        except Exception as exc:  # pragma: no cover - unwritable state dir
            logger.warning("Could not update install record for '%s': %s", record.name, exc)


@dataclass(frozen=True)
class DanglingSweep:
    """Outcome of an opportunistic dangling-projection sweep.

    A structure rather than a bare list because "swept nothing" and "did not
    look" are different facts the caller must distinguish. Returning ``[]`` for
    both would have ``cao plugin list`` silently imply a tidy store while an
    install was mid-flight.
    """

    swept: Tuple[str, ...] = ()
    skipped_busy: bool = False


def sweep_dangling_projections(
    store: Optional[InstalledPluginStore] = None,
    *,
    skills_dir: Optional[Path] = None,
) -> DanglingSweep:
    """Sweep dangling projected links without rebuilding. Never raises.

    Called by ``cao plugin list`` so simply looking at the installed set tidies
    up after an out-of-band store mutation.

    The lifecycle lock is taken NON-BLOCKING (review 4 item 1 on #584). Sweeping
    reads the installed set and then deletes links that set does not claim --
    exactly the stale-snapshot hazard the lock exists for -- but this is the read
    path, and a listing must never queue behind an install. So it declines,
    reports that it declined, and the next ``list`` tries again.
    ``rebuild_projection`` under the lock is what guarantees convergence.
    """
    store = store or InstalledPluginStore()
    target_dir = _skills_dir(skills_dir)
    try:
        with store.lifecycle_lock(0.0, blocking=False):
            swept, _findings = _sweep(store, target_dir, previous={}, current={})
            return DanglingSweep(swept=tuple(swept))
    except PluginBusyError:
        return DanglingSweep(skipped_busy=True)
    except Exception as exc:  # pragma: no cover - the never-raise backstop
        logger.warning("Dangling-projection sweep failed: %s", exc)
        return DanglingSweep()
