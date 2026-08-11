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
``projected_skill_names``, which is also what makes ``provenance.py`` a
pure record lookup and what lets a rebuild notice that a collision's winner
changed.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Set, Tuple

from cli_agent_orchestrator.agent_plugins.models import Finding, PluginRecord, Severity
from cli_agent_orchestrator.agent_plugins.store import InstalledPluginStore, PluginStoreError
from cli_agent_orchestrator.constants import SKILLS_DIR

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

    records = store.list_installed()
    prior = dict(previous) if previous is not None else _previous_projection(records)

    winners, collision_findings = _elect_winners(store, records, target_dir, prior)
    findings.extend(collision_findings)

    resolved_mode = _resolve_mode(mode)
    materialized, mode_used, material_findings = _materialize(
        store, winners, target_dir, resolved_mode, previous=prior
    )
    findings.extend(material_findings)

    swept, sweep_findings = _sweep(store, target_dir, prior, materialized, mode=mode_used)
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
    source of truth is what guarantees ``provenance.owning_plugin`` and the
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
    managed: Set[str],
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

    "CAO-managed" is decided two ways, and both are needed. A symlink resolving
    inside ``AGENT_PLUGINS_DIR`` is *structurally* ours, which is the reliable
    test. Copy-mode projections carry no such marker, so the ``managed`` names
    carried over from the previous projection cover them.
    """
    names: Set[str] = set()
    plugins_real = os.path.realpath(store.plugins_dir)

    if target_dir.is_dir():
        try:
            for item in target_dir.iterdir():
                if item.name.startswith(".") or item.name in managed:
                    continue
                if item.is_symlink():
                    try:
                        resolved = os.path.realpath(item)
                    except OSError:  # pragma: no cover - exotic FS failure
                        continue
                    if resolved == plugins_real or resolved.startswith(plugins_real + os.sep):
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

    preexisting = _preexisting_skill_names(store, target_dir, managed=set(previous))

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
    previous: Optional[Mapping[str, str]] = None,
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
        # not: found by an independent audit of the F2 fix, which guarded the
        # sweep's delete-by-name path but left this one — a real directory here is
        # deleted by `_remove_quiet` just the same. Refuse when we have no record
        # of having placed this name: a real directory (not a symlink into the
        # store) at a name the previous projection did not own is somebody else's
        # data. Names we did own stay replaceable, which is what keeps a plugin
        # upgrade and a copy-to-symlink mode migration working.
        claimed_before = (previous or {}).get(skill_name) is not None
        if (
            link_path.exists()
            and not link_path.is_symlink()
            and link_path.is_dir()
            and not claimed_before
        ):
            findings.append(
                Finding(
                    severity=Severity.SKIPPED,
                    code="projection.target_not_ours",
                    spec_ref="CAO policy",
                    message=(
                        f"Skill '{skill_name}' from plugin '{plugin_name}' was not "
                        f"projected: {link_path} is an existing directory that no "
                        f"previous projection placed, so it was left untouched."
                    ),
                    path=skill_name,
                )
            )
            continue

        ok, used_fallback, error = _place(link_path, source, effective_mode)

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


def _place(link_path: Path, source: Path, mode: str) -> Tuple[bool, bool, Optional[str]]:
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
            ok, error = _copy_into(link_path, source)
            return ok, True, error

    ok, error = _copy_into(link_path, source)
    return ok, False, error


def _copy_into(link_path: Path, source: Path) -> Tuple[bool, Optional[str]]:
    """Replace ``link_path`` with a fresh copy of ``source``."""
    try:
        _remove_quiet(link_path)
        shutil.copytree(source, link_path, symlinks=False)
        return True, None
    except OSError as exc:
        return False, str(exc)


def _is_managed_projection(path: Path, store: InstalledPluginStore, mode: str) -> bool:
    """Whether ``path`` is still an entry the projection engine placed.

    The defence-in-depth half of review finding F2. Phase one of the sweep used
    to remove ``previous - current`` **by name only** — no structural check at
    all, while phase two right below it already had the realpath-containment test
    it needed. So a name that a stale record still claimed was enough to
    ``shutil.rmtree`` whatever sat at that name, including a real directory the
    user had just installed there. A name match must never be sufficient to
    delete a real directory.

    A symlink resolving inside the plugin store is *structurally* ours, which is
    the reliable proof and the same test ``_preexisting_skill_names`` trusts. A
    real directory carries no such marker, so it is treated as ours only when the
    projection is running in copy mode, where a real directory is the shape we
    place. In symlink mode — the default, and the mode the reported failure
    happens in — a real directory is by definition not ours and survives.

    Residual, deliberately conservative: a copy-mode projection left over from an
    earlier copy-mode rebuild is not swept by a later symlink-mode rebuild. It is
    then classified as pre-existing, so the user keeps the directory and the
    plugin loses the collision — over-preservation rather than data loss, and
    reported as a finding rather than silent.
    """
    if path.is_symlink():
        try:
            resolved = os.path.realpath(path)
        except OSError:  # pragma: no cover - exotic FS failure
            return False
        plugins_real = os.path.realpath(store.plugins_dir)
        return resolved == plugins_real or resolved.startswith(plugins_real + os.sep)
    if path.is_file():
        # Never a projection we placed; a stray file may be removed by name.
        return True
    return mode == PROJECTION_MODE_COPY


def _sweep(
    store: InstalledPluginStore,
    target_dir: Path,
    previous: Mapping[str, str],
    current: Mapping[str, str],
    *,
    mode: str = PROJECTION_MODE_SYMLINK,
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
        if not _is_managed_projection(path, store, mode):
            logger.warning(
                "Not sweeping '%s': the previous projection claimed it, but what is "
                "on disk is not a CAO-managed projection. Leaving it in place.",
                path,
            )
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    code="projection.sweep_skipped_unmanaged",
                    spec_ref="CAO policy",
                    message=(
                        f"Skill '{skill_name}' was claimed by a previous projection but "
                        f"what is on disk is not a CAO-managed projection, so it was left "
                        f"in place. It is now treated as a user-owned skill."
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

    Keeping the records truthful is what makes ``provenance.owning_plugin`` a
    plain record lookup and what gives the next rebuild its "previous winner"
    for the transition warning.

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


def sweep_dangling_projections(
    store: Optional[InstalledPluginStore] = None,
    *,
    skills_dir: Optional[Path] = None,
) -> List[str]:
    """Sweep dangling projected links without rebuilding. Never raises.

    Called by ``cao plugin list`` so simply looking at the installed set tidies
    up after an out-of-band store mutation.
    """
    store = store or InstalledPluginStore()
    target_dir = _skills_dir(skills_dir)
    try:
        swept, _findings = _sweep(store, target_dir, previous={}, current={})
        return swept
    except Exception as exc:  # pragma: no cover - the never-raise backstop
        logger.warning("Dangling-projection sweep failed: %s", exc)
        return []
