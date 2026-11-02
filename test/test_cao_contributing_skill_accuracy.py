"""The ``cao-contributing`` skill documents CI. This pins it to the real CI.

A skill that describes the CI gate map in prose drifts the moment a job is
renamed, added, or has its command changed -- and a stale skill is worse than no
skill, because an agent will act on it confidently. That is not hypothetical:
review on #448 caught three factual drifts (a wrong ``--cov`` target, a moved
recorder path, and a gate map that named only six of the thirteen ``ci.yml`` jobs
it is required to cover) that accumulated in the 46 days the PR sat open.

The size of that last gap was reported three different ways -- "six", "five", and
"four newly documented jobs plus one non-job step row" -- and all three are real
measurements of different things, which is why the derivation is written down
rather than the number alone. Re-derive it from the gate map at the commit that
introduced it (``ec433f38``) against ``_ci_job_names()``, using this test's own
"documented" test, namely that the job name appears anywhere in the skill:

* **Seven** of the thirteen jobs were absent. The six named were Unit Tests, Code
  Quality, AG-UI demo, CAO MCP Apps, Web UI Build -- all five as table rows --
  plus Security Scan, which appeared only in prose.
* ``95a08178`` closed **five** of the seven. It added six job rows, but one of
  them, Security Scan, was already named, so the newly-documented count is five
  where the new-row count is six (seven counting the ``step:`` row, which
  describes a step inside an already-listed job rather than a job of its own).
  Discounting Security Scan and treating the split ``CAO MCP Apps E2E`` row as a
  restatement of an existing one gives the **four** reported elsewhere.
* ``1d35d872`` closed the sixth, Agent Plugins dog-food.
* The seventh was ``Dependency Review``, which this file had wrongly *exempted*
  rather than documented (see ``INTENTIONALLY_UNDOCUMENTED``). With the exemption
  removed and the row added, every ``ci.yml`` job is documented and no exemption
  remains -- so the count that matters now is zero, and it is enforced rather
  than asserted in a docstring.

These tests read ``.github/workflows/ci.yml`` and fail if the skill no longer
matches it, so the next rename is caught by CI rather than by a reviewer. Three
properties are asserted here that prose review kept missing:

* Every gate-map row's **Blocking?** verdict, against that job's real job-level
  ``continue-on-error``. Before this, the column was decorative -- only that a
  job *name* appeared somewhere in the file was checked, so a job flipping
  blocking/tolerated passed silently.
* A non-vacuity floor under every matcher whose result feeds a ``parametrize``.
  Those run at collection time, so a matcher that stops matching collects zero
  tests and reports success instead of failing.
* That the isolated-``HOME`` recipe actually isolates, and actually reports the
  test's exit status. It is the one snippet whose output a reader treats as
  evidence, so a recipe that lies is worse than none.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SKILL = REPO_ROOT / "skills" / "cao-contributing" / "SKILL.md"

# Jobs deliberately left out of the skill's gate map, with the reason. Anything
# not listed here MUST appear in the map -- that is what makes the test a gate
# rather than a suggestion.
#
# Deliberately EMPTY, and that is the point: the one entry this ever held --
# ``Dependency Review``, exempted as "advisory-only, PR-scoped; no local
# equivalent to run" -- was simply false about ci.yml. ``jobs.dependency-review``
# carries no job-level or step-level ``continue-on-error`` and is configured with
# ``fail-on-severity: high`` plus ``deny-licenses``, so it fails the PR on a
# qualifying finding. An exemption is a hole in the gate by construction, so the
# bar for adding one is "ci.yml genuinely does not gate on it", not "there is
# nothing to run locally". Having no local equivalent is a reason to DOCUMENT a
# job, not to hide it.
INTENTIONALLY_UNDOCUMENTED: dict[str, str] = {}

# Shipped skills whose names do not start with ``cao-``. The reference matcher
# below is anchored to the ``cao-`` prefix plus this allowlist rather than to
# ``**bold**`` generally, because the gate-map table is full of bold prose
# (``**Yes**``) that would otherwise be read as skill names.
NON_CAO_SKILL_NAMES = frozenset({"agui-author", "mcp-apps-builder"})

# Floor for the reference guard below. A pattern that stops matching would make
# the existence check vacuous instead of failing, so the count is asserted
# separately. Review on #448 flagged exactly this hazard in the ``examples/``
# parametrize, which collects zero tests if its findall returns nothing.
#
# Why a literal rather than a value derived from ci.yml, as the verdict floor is:
# there is no ground truth to derive from here. The number of skills this one
# should route to is an editorial decision, not a fact about the repo, so a
# derived floor would either be circular (count what the file happens to say) or
# wrong (every shipped skill is not a valid route). The slack is therefore stated
# instead of hidden: the routing section currently names FOUR real skills
# (cao-mcp-apps, cao-plugin, cao-provider, cao-session-management), so this floor
# tolerates losing exactly one before it fires.
MINIMUM_SKILL_REFERENCES = 3

# The ``examples/`` path matcher is a SECOND, independent parametrize site with
# the same collection-time hazard, and ``MINIMUM_SKILL_REFERENCES`` does not
# cover it -- that floor guards the skill-name matcher. This one is its own.
EXAMPLE_PATH_PATTERN = r"`(examples/[^`]+?)`"
MINIMUM_EXAMPLE_PATH_REFERENCES = 3

# The Unit Tests job narrows its own selection with these BEFORE ``-m`` is
# applied, so ``-m "not e2e"`` alone does not describe what CI runs: the Kiro
# provider integration test is excluded by path and never executes in CI. The
# skill has to say so, and this pins that correction against silent reversion.
CI_REQUIRED_IGNORES = (
    "--ignore=test/providers/test_kiro_cli_integration.py",
    "--ignore=test/e2e",
)


def _ci_spec() -> dict:
    spec = yaml.safe_load(CI_WORKFLOW.read_text())
    assert isinstance(spec, dict), f"{CI_WORKFLOW} did not parse as a YAML mapping."
    return spec


def _canonical_job_name(name: str) -> str:
    """Strip matrix interpolation only: ``Rust TUI (${{ matrix.label }})`` -> ``Rust TUI``.

    Deliberately does NOT strip ordinary parentheticals. ``(Playwright)`` and
    ``(AC3)`` are part of the real check name a contributor reads on the PR, so
    dropping them would make ``CAO MCP Apps E2E`` compare equal to
    ``CAO MCP Apps E2E (Playwright)`` -- one of the two truncations the exact
    membership test below exists to reject.
    """
    return re.sub(r"\s*\(\$\{\{.*?\}\}\)", "", name).strip()


def _ci_jobs_by_name() -> dict[str, dict]:
    """Canonical job name -> the job's parsed mapping."""
    jobs: dict[str, dict] = {}
    for job_id, job in (_ci_spec().get("jobs") or {}).items():
        job = job or {}
        jobs[_canonical_job_name(job.get("name") or job_id)] = job
    return jobs


def _ci_job_names() -> set[str]:
    return set(_ci_jobs_by_name())


def _truthy_continue_on_error(value: object) -> bool:
    """Whether a ``continue-on-error`` value tolerates failure.

    Accepts the quoted string form as well as the bool, because both are legal
    YAML for this key and a reader scanning for ``true`` should not be fooled by
    quoting.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().strip("'\"").lower() == "true"
    return False


def _job_is_blocking(job: dict) -> bool:
    """Whether this job's failure fails the workflow.

    ``continue-on-error`` is absent on every job in ``ci.yml`` today, and absent
    means BLOCKING -- GitHub defaults the key to false. That default is the whole
    reason this has to be derived rather than read: the tolerated branch is
    currently unreachable from ``ci.yml``, so ``TestTheBlockingDerivationItself``
    pins it directly instead of trusting that it works.

    STEP-level ``continue-on-error`` is deliberately not consulted. It tolerates
    one step, not the job, and ``ci.yml`` carries three of them -- artifact uploads
    in three jobs -- while all three of those jobs remain hard gates. Folding steps
    in here would mis-report three blocking jobs as tolerated.
    """
    return not _truthy_continue_on_error(job.get("continue-on-error"))


def _skill_text() -> str:
    return SKILL.read_text()


# The ``references/`` pages the skill's long sections moved into under
# progressive disclosure (#1121 follow-up 3 / R8). Named rather than globbed
# because two guards below depend on these specific files: a rename would
# otherwise leave the glob matching something else and the guards passing
# against the wrong page.
REQUIRED_REFERENCE_PAGES = frozenset({"pr-gating-workflows.md", "test-isolation.md"})


def _reference_files() -> list[Path]:
    """Every ``references/`` page of this skill, in a stable order."""
    return sorted((SKILL.parent / "references").glob("*.md"))


def _skill_text_with_references() -> str:
    """``SKILL.md`` plus every ``references/`` page.

    Follow-up 3 moved two sections out of ``SKILL.md``: the four-cell "Other
    workflows that gate a PR" table now lives in
    ``references/pr-gating-workflows.md``, and the isolated-``HOME`` recipe in
    ``references/test-isolation.md``. The guards that read those two assert
    facts about THE SKILL rather than about one of its files, so they read the
    whole skill.

    This does not weaken them. The same bytes are parsed -- the move was
    verified byte-identical -- and only the corpus supplying them grew. What it
    does mean is that a guard reading this cannot tell WHICH file a fact came
    from, so anything that must live in ``SKILL.md`` itself (the ``ci.yml`` gate
    map, the dependency section, the checklist anchor) deliberately keeps
    reading ``_skill_text()``.
    """
    parts = [_skill_text()]
    parts.extend(path.read_text(encoding="utf-8") for path in _reference_files())
    return "\n".join(parts)


def _gate_map_rows() -> list[tuple[str, str, str]]:
    """Three-cell Markdown table rows from the skill, cells stripped."""
    rows: list[tuple[str, str, str]] = []
    for line in _skill_text().splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 3:
            continue
        rows.append((cells[0], cells[1], cells[2]))
    return rows


def _gate_map_job_rows() -> dict[str, str]:
    """Canonical job name -> the raw text of that row's ``Blocking?`` cell.

    Header, separator, and ``step:`` continuation rows are skipped: a step row
    describes a step inside an already-listed job, not a job of its own.
    """
    rows: dict[str, str] = {}
    for first, _runs, verdict in _gate_map_rows():
        if first == "Job" or set(first) <= set("-: "):
            continue
        bold = re.match(r"\*\*(.+?)\*\*", first)
        if not bold:  # e.g. the "step:" continuation row, whose bold is not leading
            continue
        rows[_canonical_job_name(bold.group(1))] = verdict
    return rows


def _claimed_blocking(verdict_cell: str) -> bool | None:
    """The JOB-level verdict a ``Blocking?`` cell claims, or None if unreadable.

    An explicit bolded ``**Yes**``/``**No**`` is the job verdict wherever it
    appears in the cell; the bare phrase "non-blocking" only decides the verdict
    when no bolded token is present. That makes this ORDER-INDEPENDENT, which the
    previous first-token-wins version was not: review on #448 pointed out that
    rewording Code Quality's cell from ``black/isort **yes**; **mypy is
    non-blocking**`` to "mypy non-blocking; otherwise **yes**" would have flipped
    a blocking job to tolerated with no test noticing. Both orderings now read as
    blocking, and ``TestTheBlockingDerivationItself`` pins that.

    Two contradictory bolded verdicts in one cell return None rather than picking
    one, so an ambiguous cell is a loud failure instead of a coin flip.
    """
    verdicts = {m.lower() for m in re.findall(r"\*\*(yes|no)\*\*", verdict_cell, re.IGNORECASE)}
    if len(verdicts) > 1:
        return None
    if verdicts:
        return verdicts.pop() == "yes"
    if re.search(r"non-blocking", verdict_cell, re.IGNORECASE):
        return False
    return None


def _real_skill_names() -> set[str]:
    """Directories under ``skills/`` that actually contain a ``SKILL.md``.

    Mirrors the Agent Plugins discovery rule: one skill per immediate child
    directory holding a ``SKILL.md``, no deeper recursion.
    """
    skills_dir = REPO_ROOT / "skills"
    return {p.name for p in skills_dir.iterdir() if (p / "SKILL.md").is_file()}


def _referenced_skill_names() -> set[str]:
    """Skill names the skill text points at, in bold or backticks.

    Anchored to the ``cao-`` prefix plus ``NON_CAO_SKILL_NAMES``. A looser
    matcher over ``**...**`` would capture gate-map prose and fail spuriously;
    the cost of this precision is that a future non-``cao-`` skill must be added
    to the allowlist, which is a visible maintenance point rather than a silent
    miss.
    """
    text = _skill_text()
    found = set(re.findall(r"\*\*(cao-[a-z0-9-]+)\*\*|`(cao-[a-z0-9-]+)`", text))
    names = {m for pair in found for m in pair if m}
    for extra in NON_CAO_SKILL_NAMES:
        if re.search(rf"\*\*{re.escape(extra)}\*\*|`{re.escape(extra)}`", text):
            names.add(extra)
    # The skill documents itself; that is not a route to verify.
    return names - {"cao-contributing"}


def _referenced_example_paths() -> list[str]:
    return re.findall(EXAMPLE_PATH_PATTERN, _skill_text())


def _isolated_home_recipe() -> str:
    """The fenced ``bash`` block that runs a test under a throwaway ``HOME``.

    Located by its ``mktemp -d``, and asserted unique: if a second isolation
    recipe is ever added, the guards below would silently check only one of
    them, which is the same vacuity hazard the parametrize floors exist for.

    Reads the whole skill (``SKILL.md`` plus ``references/``) because follow-up
    3 moved this recipe into ``references/test-isolation.md``. The uniqueness
    assertion is what makes the wider corpus safe: if the recipe is ever left
    in two places at once -- the likely outcome of a half-finished move -- this
    fails loudly instead of executing whichever copy came first.
    """
    blocks = re.findall(r"```bash\n(.*?)```", _skill_text_with_references(), re.DOTALL)
    matching = [b for b in blocks if "mktemp -d" in b]
    assert len(matching) == 1, (
        f"Expected exactly one mktemp-based isolation recipe, found {len(matching)}. "
        "Update this helper deliberately rather than checking an arbitrary one."
    )
    return matching[0]


def _unit_tests_pytest_command() -> str:
    """The Unit Tests job's pytest invocation, and only that one.

    Anchored to ``jobs.test`` rather than regexed out of the whole file because
    ``ci.yml`` runs a SECOND pytest, with its own ``--cov`` and ``-m``, in the
    ``cao-mcp-apps`` job (the coverage-ratchet floor). A whole-file regex can
    therefore validate the wrong job: verified by deleting ``--cov`` and ``-m``
    from the Unit Tests step, which left the previous unanchored assertions green
    while the documented job produced no coverage and deselected nothing.
    """
    steps = _ci_spec()["jobs"]["test"]["steps"]
    commands = [str(s.get("run", "")) for s in steps if "pytest" in str(s.get("run", ""))]
    assert len(commands) == 1, (
        "Expected exactly one pytest step in the Unit Tests job, found "
        f"{len(commands)}. Update this helper deliberately -- picking one of "
        "several silently would reintroduce the ambiguity it exists to remove."
    )
    return commands[0]


class TestTheGateMapMatchesCi:
    def test_every_ci_job_is_documented(self):
        documented = _skill_text()
        missing = sorted(
            name
            for name in _ci_job_names()
            if name not in INTENTIONALLY_UNDOCUMENTED and name not in documented
        )
        assert not missing, (
            "These CI jobs are not mentioned in the cao-contributing gate map: "
            f"{missing}. Add them to the table in {SKILL.relative_to(REPO_ROOT)}, or "
            "record why they are omitted in INTENTIONALLY_UNDOCUMENTED."
        )

    def test_no_phantom_jobs_are_documented(self):
        """The skill must not promise a job that CI does not run.

        Exact set membership, not the bidirectional ``startswith`` this used to
        do. That comparison accepted any truncation or extension of a real name:
        ``Code`` passed for ``Code Quality``, and ``CAO MCP Apps E2E`` passed for
        ``CAO MCP Apps E2E (Playwright)`` -- both verified green before this
        change. A contributor searching the PR's checks for the documented name
        finds nothing in either case.
        """
        real = _ci_job_names()
        # Only the FIRST column of a ci.yml gate-map row names a job -- later
        # columns hold the blocking verdict, which is also bolded. Scoped to the
        # three-cell ci.yml table: the four-cell "Other workflows" table names
        # workflows, not ci.yml jobs, and TestOtherPrGatingWorkflowsAreDocumented
        # holds it to the same exact-name standard against its own files.
        claimed = set(_gate_map_job_rows())
        phantom = sorted(claimed - real)
        assert not phantom, (
            f"The skill documents jobs that no longer exist in ci.yml: {phantom}. "
            f"Real job names: {sorted(real)}. Names must match exactly (matrix "
            "interpolation aside) -- a near-miss is not findable in the PR's checks."
        )


class TestTheGateMapBlockingVerdicts:
    """The ``Blocking?`` column, which was previously unverified prose.

    ``test_every_ci_job_is_documented`` only checks that a job NAME appears
    somewhere in the file, so a job flipping blocking/tolerated -- or a row
    simply claiming the wrong verdict -- used to pass silently. That is the
    drift class this file exists to catch, and the column is the part a
    contributor acts on when deciding whether a red check blocks the merge.
    """

    def test_the_verdict_guard_has_something_to_check(self):
        """Non-vacuity floor, tied to ci.yml rather than to a magic number."""
        rows = _gate_map_job_rows()
        must_document = _ci_job_names() - set(INTENTIONALLY_UNDOCUMENTED)
        assert len(rows) >= len(must_document), (
            f"Parsed only {len(rows)} gate-map job rows ({sorted(rows)}) but ci.yml has "
            f"{len(must_document)} jobs requiring documentation ({sorted(must_document)}). "
            "If the table was restructured, update the row parser -- otherwise the "
            "verdict check below silently verifies fewer rows than it appears to."
        )

    def test_every_row_verdict_is_readable(self):
        unreadable = sorted(
            name for name, cell in _gate_map_job_rows().items() if _claimed_blocking(cell) is None
        )
        assert not unreadable, (
            f"These gate-map rows have no readable Blocking? verdict: {unreadable}. "
            "A cell must say **Yes**, **No**, or 'non-blocking' so the claim can be "
            "checked against ci.yml rather than taken on trust."
        )

    def test_every_row_verdict_matches_job_level_continue_on_error(self):
        jobs = _ci_jobs_by_name()
        wrong: list[str] = []
        for name, cell in sorted(_gate_map_job_rows().items()):
            job = jobs.get(name)
            if job is None:
                continue  # a phantom name is test_no_phantom_jobs_are_documented's finding
            claimed = _claimed_blocking(cell)
            real = _job_is_blocking(job)
            if claimed is not real:
                raw = job.get("continue-on-error", "<unset>")
                wrong.append(
                    f"{name!r}: the skill says "
                    f"{'blocking' if claimed else 'tolerated'}, but ci.yml says "
                    f"{'blocking' if real else 'tolerated'} "
                    f"(job-level continue-on-error={raw!r})"
                )
        assert not wrong, (
            "The gate map's Blocking? column disagrees with ci.yml:\n  "
            + "\n  ".join(wrong)
            + f"\nFix the table in {SKILL.relative_to(REPO_ROOT)} or the job in ci.yml. "
            "A wrong verdict tells a contributor to ignore a gate that will block "
            "their merge, or to chase one that will not."
        )


class TestTheVerdictCellParser:
    """Pins ``_claimed_blocking`` against rewordings, not just today's wording.

    The cell text is prose a human edits. Review on #448 showed the previous
    first-token-wins parser would read "mypy non-blocking; otherwise **yes**" as
    a TOLERATED job, silently inverting the verdict for Code Quality while every
    test stayed green. Order-independence is the property; these are the cases.
    """

    @pytest.mark.parametrize(
        "cell,expected",
        [
            ("**Yes**", True),
            ("**No**", False),
            # Both orderings of the real Code Quality cell must read as blocking.
            ("black/isort **yes**; **mypy is non-blocking**", True),
            ("mypy non-blocking; otherwise **yes**", True),
            # The Dependency Review row's shape: a verdict plus a scope caveat.
            ("**Yes** — CI-only; skipped on forks", True),
            # No bolded token at all: the phrase decides.
            ("non-blocking", False),
            # Unreadable rather than guessed.
            ("", None),
            ("probably?", None),
            ("**Yes** and also **No**", None),
        ],
    )
    def test_the_cell_is_read_the_same_regardless_of_token_order(self, cell, expected):
        assert _claimed_blocking(cell) is expected


class TestTheBlockingDerivationItself:
    """Pins ``_job_is_blocking`` directly, because ci.yml cannot exercise it.

    Every job in ci.yml is blocking today, so the gate-map test above would pass
    even if the derivation returned True unconditionally. These assert the branch
    that ci.yml never reaches, so the guard is not merely vacuously green.
    """

    @pytest.mark.parametrize(
        "yaml_fragment,expected_blocking",
        [
            ("", True),  # unset -- GitHub's default is false, i.e. blocking
            ("continue-on-error: false", True),
            ("continue-on-error: true", False),
            ('continue-on-error: "true"', False),  # quoted is still truthy YAML
        ],
    )
    def test_unset_and_false_block_while_true_tolerates(self, yaml_fragment, expected_blocking):
        job = yaml.safe_load("name: Example\n" + (f"{yaml_fragment}\n" if yaml_fragment else ""))
        assert _job_is_blocking(job) is expected_blocking

    def test_step_level_tolerance_does_not_make_the_job_tolerated(self):
        job = yaml.safe_load(
            "name: Example\n"
            "steps:\n"
            "  - run: the real gate\n"
            "  - run: upload an artifact\n"
            "    continue-on-error: true\n"
        )
        assert _job_is_blocking(job) is True

    def test_ci_really_contains_the_step_level_tolerance_just_asserted(self):
        """Keeps the test above honest: the case it models is real, not invented.

        If ci.yml ever stops carrying step-level tolerances, the modelling above
        is no longer describing this repo and should be revisited rather than
        left as decoration.
        """
        tolerated = [
            (job_name, step.get("name"))
            for job_name, job in _ci_jobs_by_name().items()
            for step in (job.get("steps") or [])
            if _truthy_continue_on_error(step.get("continue-on-error"))
        ]
        assert len(tolerated) >= 3, (
            "Expected ci.yml to carry at least the three step-level "
            "continue-on-error steps verified here -- the AG-UI demo, AG-UI "
            f"construct demos, and Agent Plugins dog-food artifact uploads -- but "
            f"found {len(tolerated)}: {tolerated}. A DECREASE is the direction that "
            "matters: it means the case "
            "test_step_level_tolerance_does_not_make_the_job_tolerated models may "
            "no longer exist in this repo. An increase is fine; raise this floor."
        )
        assert all(
            _job_is_blocking(_ci_jobs_by_name()[job_name]) for job_name, _ in tolerated
        ), f"A job with a tolerated STEP must still be a blocking JOB: {tolerated}"


class TestReferencedSkillsExist:
    """A skill that routes agents elsewhere must not name a skill that is absent.

    #448 shipped a route to ``cao-skill-creator``, which never existed in
    ``skills/``. A dangling route is worse in a packaged skill than in ordinary
    prose: the frontmatter ``description`` is the text an agent matches on when
    deciding whether to load the skill, so a phantom name both fails to route
    and widens the activation surface.
    """

    def test_the_reference_guard_has_something_to_check(self):
        found = _referenced_skill_names()
        assert len(found) >= MINIMUM_SKILL_REFERENCES, (
            f"Expected at least {MINIMUM_SKILL_REFERENCES} skill references in "
            f"{SKILL.relative_to(REPO_ROOT)}, found {sorted(found)}. If the routing "
            "section was reworded, lower this floor deliberately -- do not let the "
            "existence check below silently verify nothing."
        )

    def test_every_referenced_skill_exists(self):
        missing = sorted(_referenced_skill_names() - _real_skill_names())
        assert not missing, (
            f"{SKILL.relative_to(REPO_ROOT)} references skills that do not exist: "
            f"{missing}. Real skills: {sorted(_real_skill_names())}. Either point the "
            "reference at a skill that exists, drop it, or add the skill."
        )


class TestQuotedCommandsAreReal:
    def test_the_coverage_target_matches_ci(self):
        match = re.search(r"--cov=(\S+)", _unit_tests_pytest_command())
        assert match, "The Unit Tests job no longer passes --cov; update this test."
        target = match.group(1)
        # Compare exact tokens. A substring check would pass "--cov=src" against a
        # skill saying "--cov=src/cli_agent_orchestrator", which is the very drift
        # this test exists to catch.
        quoted = set(re.findall(r"--cov=([^\s`|)]+)", _skill_text()))
        assert target in quoted, (
            f"The Unit Tests job runs coverage as --cov={target}, but the skill quotes "
            f"{quoted or '{}'}. A wrong coverage target sends contributors looking at "
            "the wrong report."
        )

    def test_the_marker_expression_matches_ci(self):
        match = re.search(r'-m\s+"([^"]+)"', _unit_tests_pytest_command())
        assert match, "The Unit Tests job no longer passes -m; update this test."
        assert match.group(1) in _skill_text(), (
            f'The Unit Tests job deselects with -m "{match.group(1)}"; the skill must '
            "quote it verbatim, because it replaces any local addopts rather than "
            "composing with them."
        )

    def test_the_path_exclusions_are_documented_as_well_as_the_marker(self):
        """``-m "not e2e"`` is not the whole selection, and the difference misleads.

        The Unit Tests job also excludes two paths outright, so the Kiro provider
        integration test does NOT run in CI. Quoting only the marker supports the
        inference that every integration test runs in CI, which is what the skill
        used to say. Pinned in both directions: the flags must still be in ci.yml,
        and the skill must still name them.
        """
        command = _unit_tests_pytest_command()
        missing_from_ci = [flag for flag in CI_REQUIRED_IGNORES if flag not in command]
        assert not missing_from_ci, (
            f"The Unit Tests job no longer passes {missing_from_ci}. The skill's "
            "carve-out for the Kiro integration test is now wrong -- reword it rather "
            "than deleting this assertion."
        )
        text = _skill_text()
        missing_from_skill = [flag for flag in CI_REQUIRED_IGNORES if flag not in text]
        assert not missing_from_skill, (
            f"ci.yml narrows the Unit Tests selection with {missing_from_skill}, which "
            f"{SKILL.relative_to(REPO_ROOT)} does not mention. Without them the skill "
            'implies -m "not e2e" is the whole story and that every integration test '
            "runs in CI; the Kiro provider integration test does not."
        )


def _run_recipe_shape(recipe_body: str, stub_exit: int, shell: str = "bash") -> dict[str, object]:
    """Execute a recipe shape and report what a contributor would observe.

    The ``uv run pytest ...`` call -- and only that -- is swapped for a recording
    stub with a chosen exit status, so what runs is the documented text's own
    shape (subshell, trap, ordering, cleanup) rather than a paraphrase of it.

    Returns the status the recipe *reported*, whether the throwaway directory
    still existed immediately after the recipe finished, and the ``HOME`` /
    ``CAO_HOME_DIR`` the child process really saw.
    """
    interpreter = shutil.which(shell)
    if interpreter is None:
        pytest.skip(f"{shell} is required to execute the documented recipe")
    with tempfile.TemporaryDirectory() as scratch:
        probe = Path(scratch) / "probe"
        stub = Path(scratch) / "stub"
        stub.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n%s\\n" "$HOME" "${{CAO_HOME_DIR-<unset>}}" > "{probe}"\n'
            f"exit {stub_exit}\n"
        )
        stub.chmod(0o755)
        # Stop at shell punctuation: a greedy \S+ swallows a trailing ';' or ')',
        # which silently turns `pytest x.py; rm -rf $TMPH` into `stub rm -rf
        # $TMPH` -- the stub receiving rm as ARGUMENTS and no cleanup running.
        # That made a negative control pass for the wrong reason, which is the
        # exact defect class this file exists to prevent.
        executable = re.sub(r"uv run pytest\s+[\w./\-]+", f'"{stub}"', recipe_body)
        assert str(stub) in executable, (
            "The pytest invocation was not substituted, so this would execute the "
            f"real suite instead of the stub. Recipe:\n{recipe_body}"
        )
        harness = (
            # An ordinary shell with no errexit -- the condition under which the
            # status bug appears at all.
            "set +e\n"
            f"{executable}\n"
            # The status the recipe BLOCK returns to whatever runs it -- a script,
            # an agent, `&&`. Captured on the very next line, before anything else
            # overwrites $?. This is distinct from the number the recipe PRINTS:
            # a trailing `echo "exit=$?"` prints the right value and still returns 0.
            "RECIPE_STATUS=$?\n"
            # Still inside the harness process, so a trap registered OUTSIDE the
            # subshell has not fired yet. This distinguishes "cleaned up" from
            # "will be cleaned up whenever the shell happens to exit", which is
            # the difference between a recipe that is safe interactively and one
            # that only looks safe inside a script.
            'RECORDED_HOME=$(head -1 "' + str(probe) + '" 2>/dev/null)\n'
            'if [ -n "$RECORDED_HOME" ] && [ -d "$RECORDED_HOME" ]; '
            'then echo "LEAK=1"; else echo "LEAK=0"; fi\n'
            # Exit with the recipe's status so proc.returncode reports it. Without
            # this the harness's own last command (the LEAK echo) sets the
            # returncode, which is always 0 -- the same masking bug being tested for.
            'exit "$RECIPE_STATUS"\n'
        )
        proc = subprocess.run([interpreter, "-c", harness], capture_output=True, text=True)
        recorded = probe.read_text().splitlines() if probe.exists() else []
        reported = re.search(r"exit=(-?\d+)", proc.stdout)
        leak = re.search(r"LEAK=(\d)", proc.stdout)
        return {
            "reported_status": int(reported.group(1)) if reported else None,
            "returned_status": proc.returncode,
            "leaked": leak.group(1) == "1" if leak else None,
            "child_home": recorded[0] if recorded else None,
            "child_cao_home": recorded[1] if len(recorded) > 1 else None,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }


def _recipe_is_trustworthy(result: dict[str, object], expected_status: int) -> bool:
    """All properties at once: right printed AND returned status, cleaned up, isolated."""
    home, cao = result["child_home"], result["child_cao_home"]
    return bool(
        result["reported_status"] == expected_status
        and result["returned_status"] == expected_status
        and result["leaked"] is False
        and home
        and cao
        and cao != "<unset>"
        and str(cao).startswith(str(home))
    )


# Recipe shapes that are WRONG, each in a way that a string check over the
# Markdown fence cannot see. These are the negative controls: review of #448
# showed the previous guards still passed after moving the ``CAO_HOME_DIR``
# assignment below the invocation, or hoisting the trap out of the subshell, so
# "the guard exists" had to be replaced with "the guard distinguishes these".
BROKEN_RECIPE_SHAPES: dict[str, str] = {
    # The shape this skill itself documented at 88bc5722. It prints the right
    # number, so a guard that only parses stdout passes it -- which is exactly
    # what happened until review ran the block and checked its return code.
    "a diagnostic after the subshell prints the status but returns 0": (
        "TMPH=$(mktemp -d)\n"
        "( trap 'rm -rf \"$TMPH\"' EXIT\n"
        '  HOME="$TMPH" CAO_HOME_DIR="$TMPH/cao" uv run pytest test/path/to/test_x.py )\n'
        'echo "exit=$?"\n'
    ),
    "cleanup with ';' reports rm's status instead of the test's": (
        'TMPH=$(mktemp -d); HOME="$TMPH" CAO_HOME_DIR="$TMPH/cao" '
        'uv run pytest test/path/to/test_x.py; rm -rf "$TMPH"\n'
        'echo "exit=$?"\n'
    ),
    "cleanup with '&&' leaks the directory on the failing run": (
        'TMPH=$(mktemp -d); HOME="$TMPH" CAO_HOME_DIR="$TMPH/cao" '
        'uv run pytest test/path/to/test_x.py && rm -rf "$TMPH"\n'
        'echo "exit=$?"\n'
    ),
    "the override below the invocation is not in its environment": (
        "TMPH=$(mktemp -d)\n"
        "( trap 'rm -rf \"$TMPH\"' EXIT\n"
        '  HOME="$TMPH" uv run pytest test/path/to/test_x.py\n'
        '  CAO_HOME_DIR="$TMPH/cao" )\n'
        'echo "exit=$?"\n'
    ),
    "a trap outside the subshell defers cleanup to shell exit": (
        "TMPH=$(mktemp -d)\n"
        "trap 'rm -rf \"$TMPH\"' EXIT\n"
        '( HOME="$TMPH" CAO_HOME_DIR="$TMPH/cao" uv run pytest test/path/to/test_x.py )\n'
        'echo "exit=$?"\n'
    ),
    "overriding HOME alone leaves CAO's state where it was": (
        "TMPH=$(mktemp -d)\n"
        "( trap 'rm -rf \"$TMPH\"' EXIT\n"
        '  HOME="$TMPH" uv run pytest test/path/to/test_x.py )\n'
        'echo "exit=$?"\n'
    ),
}


class TestTheIsolatedHomeRecipeReallyIsolates:
    """The recipe tells contributors to prove a test is clean-runner-safe.

    It is the one snippet in the skill whose whole purpose is to produce a
    trustworthy pass/fail signal, so a recipe that reports the wrong answer is
    worse than no recipe: it manufactures the confidence it was supposed to test
    for. Three ways it can, all three now EXECUTED rather than pattern-matched:

    * Overriding ``HOME`` alone does not relocate CAO's state. ``constants.py``
      prefers an exported ``CAO_HOME_DIR`` and derives ``DB_DIR`` and every other
      state path from it, so a contributor who already exports an absolute (or
      cwd-relative) value keeps using the initialised store the recipe is meant
      to exclude -- concealing exactly the missing-table failure it exists to
      surface. A tilde-relative value does follow ``HOME``, which is why this
      reads as working when spot-checked.
    * Cleaning up with ``;`` discards the test's status and yields ``rm``'s, so a
      failing run reports success. ``&&`` fixes the status but leaks the
      directory on exactly the failures you wanted isolated.
    * A trap registered outside the subshell defers cleanup to shell exit, which
      looks identical inside a script and leaks for the rest of the session
      interactively.

    Why this class runs a subprocess when the rest of the file is strictly
    read-only: review demonstrated that the string-matching version passed after
    edits that broke the recipe -- moving the assignment below the invocation,
    hoisting the trap. A guard whose subject is a token cannot see either. The
    harness is still hermetic (bash plus two temp files; no network, DB, server,
    or real ``HOME``), and ``BROKEN_RECIPE_SHAPES`` pins that it actually
    distinguishes the broken shapes from the documented one.
    """

    def test_the_env_var_the_recipe_overrides_is_the_one_constants_reads(self):
        """Anchor the guard to the code, not to a remembered variable name."""
        source = (REPO_ROOT / "src" / "cli_agent_orchestrator" / "constants.py").read_text()
        assert 'os.environ.get("CAO_HOME_DIR"' in source, (
            "constants.py no longer reads CAO_HOME_DIR from the environment. The "
            "isolated-HOME recipe overrides that variable; if the override moved, "
            "update the recipe and this guard together."
        )

    def test_it_overrides_cao_home_dir_under_the_throwaway_directory(self):
        """Fast localiser for the common regression; behaviour is checked below."""
        recipe = _isolated_home_recipe()
        assert re.search(r"CAO_HOME_DIR=\"?\$\{?TMPH\}?", recipe), (
            "The recipe must set CAO_HOME_DIR to a path under the mktemp directory, "
            "or CAO's state stays outside the throwaway home. Recipe:\n" + recipe
        )

    @pytest.mark.parametrize("stub_exit", [0, 1, 2])
    def test_the_recipe_reports_the_real_test_status(self, stub_exit: int):
        result = _run_recipe_shape(_isolated_home_recipe(), stub_exit)
        assert result["reported_status"] == stub_exit, (
            f"A test exiting {stub_exit} was reported as "
            f"{result['reported_status']!r} by the documented recipe. A caller "
            f"checking the status would misread this run.\n{result['stdout']}"
        )

    @pytest.mark.parametrize("stub_exit", [0, 1, 2])
    def test_the_recipe_block_returns_the_real_test_status(self, stub_exit: int):
        """What an automated caller actually receives, not what gets printed."""
        result = _run_recipe_shape(_isolated_home_recipe(), stub_exit)
        assert result["returned_status"] == stub_exit, (
            f"A test exiting {stub_exit} made the documented recipe block return "
            f"{result['returned_status']!r}. A script or agent checking $? would "
            f"misread this run even though the printed diagnostic is right.\n"
            f"{result['stdout']}"
        )

    @pytest.mark.parametrize("stub_exit", [0, 1, 2])
    def test_the_recipe_also_works_when_pasted_into_zsh(self, stub_exit: int):
        """macOS's default shell. The fence says bash; contributors paste into zsh.

        zsh reserves ``status`` as a read-only alias of ``$?``, so a recipe that
        saves the exit code as ``status=$?`` errors there and returns the error's
        code instead of the test's. That shipped briefly in this PR and was caught
        only by running the recipe interactively. Skipped where zsh is absent.
        """
        result = _run_recipe_shape(_isolated_home_recipe(), stub_exit, shell="zsh")
        assert _recipe_is_trustworthy(result, stub_exit), (
            f"Under zsh the documented recipe is not trustworthy for exit {stub_exit}."
            f"\nobserved: {result}"
        )

    def test_the_recipe_cleans_up_before_it_returns(self):
        result = _run_recipe_shape(_isolated_home_recipe(), 1)
        assert result["leaked"] is False, (
            "The throwaway directory still existed after the recipe finished, so "
            f"cleanup is deferred to shell exit.\n{result['stdout']}"
        )

    def test_the_child_process_really_ran_isolated(self):
        result = _run_recipe_shape(_isolated_home_recipe(), 0)
        home, cao = result["child_home"], result["child_cao_home"]
        assert home and home != str(
            Path.home()
        ), f"The child ran with HOME={home!r}, which is not a throwaway directory."
        assert cao and cao != "<unset>", (
            "The child saw no CAO_HOME_DIR, so CAO would resolve its state from "
            f"the ambient environment.\n{result['stdout']}"
        )
        assert str(cao).startswith(str(home)), (
            f"CAO_HOME_DIR={cao!r} is not under HOME={home!r}, so state lives "
            "outside the directory that gets cleaned up."
        )

    def test_the_documented_shape_is_trustworthy(self):
        assert _recipe_is_trustworthy(_run_recipe_shape(_isolated_home_recipe(), 1), 1)

    @pytest.mark.parametrize("label", sorted(BROKEN_RECIPE_SHAPES), ids=lambda label: label[:40])
    def test_each_broken_shape_is_detected(self, label: str):
        """Non-vacuity: prove the checks above can actually fail."""
        result = _run_recipe_shape(BROKEN_RECIPE_SHAPES[label], 1)
        assert not _recipe_is_trustworthy(result, 1), (
            f"A recipe that is broken -- {label} -- was judged trustworthy. The "
            "guards above cannot detect this regression, so they do not protect "
            f"the property they claim to.\nobserved: {result}"
        )


class TestReferencedPathsExist:
    def test_the_example_path_guard_has_something_to_check(self):
        """Floor for the parametrize below, which is evaluated at COLLECTION time.

        If those backticked ``examples/`` paths are reworded away, ``findall``
        returns ``[]``, the parametrized guard collects ZERO tests, and the suite
        reports success -- the failure mode is silence, not red.
        ``test_the_reference_guard_has_something_to_check`` is the floor for a
        DIFFERENT matcher (skill names) and does not cover this site.
        """
        found = _referenced_example_paths()
        assert len(found) >= MINIMUM_EXAMPLE_PATH_REFERENCES, (
            f"Expected at least {MINIMUM_EXAMPLE_PATH_REFERENCES} backtick-quoted "
            f"examples/ paths in {SKILL.relative_to(REPO_ROOT)}, found {found}. If the "
            "recorder sections were reworded, lower this floor deliberately -- the "
            "parametrized existence check below collects nothing without it."
        )

    @pytest.mark.parametrize("quoted", re.findall(EXAMPLE_PATH_PATTERN, SKILL.read_text()))
    def test_every_referenced_example_path_exists(self, quoted: str):
        # A workflow path filter such as `examples/x/images/**` names its folder.
        path = REPO_ROOT / quoted.removesuffix("**").rstrip("/")
        assert path.exists(), (
            f"The skill references {quoted}, which does not exist. "
            "Paths in a skill are instructions an agent will follow literally."
        )


class TestMypyToleranceClaim:
    def test_mypy_is_blocking_via_the_ratchet(self):
        """The skill tells contributors mypy is blocking via a ratchet. Verify that holds."""
        spec = _ci_spec()
        steps = spec["jobs"]["lint"]["steps"]
        mypy_steps = [s for s in steps if "mypy" in str(s.get("run", ""))]
        assert mypy_steps, "The lint job no longer runs mypy; update the skill."
        assert not any(_truthy_continue_on_error(s.get("continue-on-error")) for s in mypy_steps), (
            "mypy is no longer BLOCKING in CI (continue-on-error reappeared). The "
            "skill's guidance that mypy is a blocking ratchet is now wrong."
        )


# --- Workflows other than ci.yml that gate a pull request ----------------------
#
# Review of #448 found the gate map silently incomplete: ``Secret Scan`` had been
# split out of ci.yml into its own workflow (#457), so a table pinned to ci.yml
# alone could never notice it was missing, and neither could cargo-deny or the
# path-filtered provider workflows. These guards derive the set from every
# workflow file that triggers on ``pull_request`` instead of from a list, so a
# new PR-gating workflow fails here until the skill documents it.

WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
# Never a PR check: ``if: github.event_name == 'push'`` jobs (Docs site deploy).
_PUSH_ONLY_JOB = re.compile(r"github\.event_name\s*==\s*'push'")
# Sentinel for "no pull_request trigger": None already means a trigger with no body.
_NO_PR = object()


def _pull_request_trigger(spec: dict) -> object:
    """The ``pull_request`` trigger config, or ``_NO_PR`` when there is none.

    PyYAML reads the bare key ``on`` as boolean True, so both spellings are
    checked; a trigger written as ``pull_request:`` with no body is ``None``,
    which is still a trigger and must not be confused with "absent".
    """
    on = spec.get(True, spec.get("on"))
    if isinstance(on, str):
        return None if on == "pull_request" else _NO_PR
    if isinstance(on, list):
        return None if "pull_request" in on else _NO_PR
    if isinstance(on, dict) and "pull_request" in on:
        return on["pull_request"]
    return _NO_PR


def _pr_gating_workflows() -> dict[str, dict]:
    """Workflow display name -> spec, for every non-ci.yml workflow run on PRs."""
    found: dict[str, dict] = {}
    for path in sorted(WORKFLOWS_DIR.glob("*.y*ml")):
        if path.resolve() == CI_WORKFLOW.resolve():
            continue
        spec = yaml.safe_load(path.read_text()) or {}
        trigger = _pull_request_trigger(spec)
        if trigger is _NO_PR:
            continue
        found[spec.get("name") or path.stem] = {"spec": spec, "trigger": trigger, "file": path.name}
    return found


def _pr_check_names(spec: dict) -> set[str]:
    """Job display names that can actually appear as checks on a PR."""
    names = set()
    for job_id, job in (spec.get("jobs") or {}).items():
        job = job or {}
        if _PUSH_ONLY_JOB.search(str(job.get("if") or "")):
            continue
        names.add(_canonical_job_name(job.get("name") or job_id))
    return names


def _other_workflow_rows() -> dict[str, tuple[str, str, str, str]]:
    """Workflow name -> (file cell, checks cell, runs-on cell, verdict cell).

    Four-cell rows only, which is what keeps this table out of the three-cell
    ci.yml job parser above -- and keeps that parser's phantom-job guard strict.

    Reads the whole skill (``SKILL.md`` plus ``references/``) because follow-up
    3 moved this table into ``references/pr-gating-workflows.md``. The cell
    shape still does the separating: the ci.yml gate map is three-cell and its
    parser still reads ``SKILL.md`` alone, so widening the corpus here cannot
    feed a reference page's rows to it.
    """
    rows: dict[str, tuple[str, str, str, str]] = {}
    for line in _skill_text_with_references().splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 4 or cells[0] == "Workflow" or set(cells[0]) <= set("-: "):
            continue
        bold = re.match(r"\*\*(.+?)\*\*\s*(?:\(`([^`]+)`\))?", cells[0])
        if bold:
            rows[bold.group(1)] = (bold.group(2) or "", cells[1], cells[2], cells[3])
    return rows


class TestOtherPrGatingWorkflowsAreDocumented:
    def test_the_guard_has_something_to_check(self):
        """Non-vacuity: at least Secret Scan, cargo-deny and one path-filtered one."""
        found = _pr_gating_workflows()
        assert {"Secret Scan", "cargo-deny"} <= set(found), sorted(found)
        assert any(
            isinstance(w["trigger"], dict) and w["trigger"].get("paths") for w in found.values()
        )

    def test_every_pr_gating_workflow_has_a_row(self):
        missing = sorted(set(_pr_gating_workflows()) - set(_other_workflow_rows()))
        assert not missing, (
            f"These workflows run on pull requests but are absent from the skill's "
            f"'Other workflows that gate a PR' table: {missing}. A contributor using "
            "the skill to read a red PR would not know they exist."
        )

    def test_no_phantom_workflows_are_documented(self):
        phantom = sorted(set(_other_workflow_rows()) - set(_pr_gating_workflows()))
        assert not phantom, f"The skill documents PR-gating workflows that do not exist: {phantom}"

    @pytest.mark.parametrize("workflow", sorted(_pr_gating_workflows()))
    def test_the_file_named_is_the_real_file(self, workflow: str):
        file_cell = _other_workflow_rows()[workflow][0]
        assert file_cell == _pr_gating_workflows()[workflow]["file"], (
            f"{workflow!r} is defined in {_pr_gating_workflows()[workflow]['file']}, "
            f"but the skill names {file_cell!r}."
        )

    @pytest.mark.parametrize("workflow", sorted(_pr_gating_workflows()))
    def test_the_listed_checks_are_exactly_the_pr_checks(self, workflow: str):
        """Exact set: a push-only job listed, or a real check omitted, both fail."""
        claimed = set(re.findall(r"`([^`]+)`", _other_workflow_rows()[workflow][1]))
        actual = _pr_check_names(_pr_gating_workflows()[workflow]["spec"])
        assert claimed == actual, (
            f"{workflow!r}: the skill lists checks {sorted(claimed)}, but on a PR the "
            f"workflow runs {sorted(actual)}."
        )

    @pytest.mark.parametrize("workflow", sorted(_pr_gating_workflows()))
    def test_the_path_filter_claim_matches_the_trigger(self, workflow: str):
        trigger = _pr_gating_workflows()[workflow]["trigger"]
        filtered = isinstance(trigger, dict) and bool(trigger.get("paths"))
        runs_on = _other_workflow_rows()[workflow][2].lower()
        claims_filtered = runs_on.startswith("only")
        assert claims_filtered == filtered, (
            f"{workflow!r} is {'path-filtered' if filtered else 'run on every PR'}, "
            f"but the skill says it runs on: {runs_on!r}."
        )

    def test_docs_trigger_summary_lists_every_input_path(self):
        spec = yaml.safe_load((WORKFLOWS_DIR / "gh-pages.yml").read_text())
        file_cell, _, runs_on, _ = _other_workflow_rows()[spec["name"]]
        documented = set(re.findall(r"`([^`]+)`", runs_on))
        documented.add(f".github/workflows/{file_cell}")
        trigger = _pull_request_trigger(spec)
        assert isinstance(trigger, dict)
        assert documented == set(trigger["paths"])

    @pytest.mark.parametrize("workflow", sorted(_pr_gating_workflows()))
    def test_the_verdict_matches_job_level_continue_on_error(self, workflow: str):
        spec = _pr_gating_workflows()[workflow]["spec"]
        pr_jobs = [
            job or {}
            for job_id, job in (spec.get("jobs") or {}).items()
            if _canonical_job_name((job or {}).get("name") or job_id) in _pr_check_names(spec)
        ]
        actual = all(_job_is_blocking(job) for job in pr_jobs)
        claimed = _claimed_blocking(_other_workflow_rows()[workflow][3])
        assert claimed is not None, f"{workflow!r}: the Blocking? cell is unreadable."
        assert claimed == actual, (
            f"{workflow!r}: the skill says blocking={claimed}, but the workflow's "
            f"PR jobs give blocking={actual}."
        )


class TestReferencePagesAreReachable:
    """A ``references/`` page nothing links to is dead weight in the package.

    Progressive disclosure only works if the top-level ``SKILL.md`` says a
    deeper page exists: an agent loads ``SKILL.md`` and follows links from it,
    so an unlinked reference page ships bytes in every installed copy that no
    reader will ever open. It is also the failure mode a move like follow-up 3
    produces -- the section leaves ``SKILL.md`` and the link is forgotten -- and
    neither the packaging parity test nor the link validator notices, because
    both only check links that EXIST.
    """

    def test_the_pages_the_other_guards_depend_on_are_present(self):
        """Named, not globbed: a rename must break here rather than silently.

        ``_other_workflow_rows`` and ``_isolated_home_recipe`` read whichever
        pages the glob finds. If one were renamed, those guards would keep
        passing against the renamed file and this test is the only place that
        records which names the suite was written against.
        """
        present = {path.name for path in _reference_files()}
        missing = sorted(REQUIRED_REFERENCE_PAGES - present)
        assert not missing, (
            f"These reference pages are missing from {SKILL.parent.relative_to(REPO_ROOT)}"
            f"/references/: {missing}. Present: {sorted(present)}. If a page was renamed, "
            "update REQUIRED_REFERENCE_PAGES deliberately -- the guards that parse the "
            "'Other workflows' table and execute the isolated-HOME recipe read these files."
        )

    @pytest.mark.parametrize("reference", _reference_files(), ids=lambda path: path.name)
    def test_every_reference_page_is_linked_from_the_skill(self, reference: Path):
        link = re.compile(r"\]\(\s*(?:\./)?references/" + re.escape(reference.name) + r"\s*\)")
        assert link.search(_skill_text()), (
            f"references/{reference.name} is not linked from "
            f"{SKILL.relative_to(REPO_ROOT)}. A reference page is only reachable if the "
            "skill body points at it; an agent reads SKILL.md and follows its links, so an "
            "unlinked page ships in every copy and is never opened."
        )


DEPENDENCY_SECTION_HEADING = "## Dependency and lockfile changes"

# Every path this section names sits inside a backtick span, so the spans are
# what gets scanned -- but a span is not necessarily a path, which is why there
# are two matchers rather than one. They differ in how much of the span has to
# be the path, because the two kinds of token carry different false-positive
# risk:
#
# * A ``scripts/`` or ``examples/`` token is matched ANYWHERE INSIDE a span.
#   The directory prefix disambiguates it, and matching inside the span is the
#   entire point: the only script this section names appears as an argument to a
#   command, ``uv run python scripts/dependency_security.py``, so the span is
#   the command and the path is a substring of it. Requiring the path to BE the
#   whole span -- which this pattern used to do -- dropped that path silently
#   and left #1120 AC6 ("every script path the section names exists")
#   unenforced. Review on #1121 (P3-4) demonstrated the gap by renaming the
#   script to ``scripts/no_such_gate.py``: this file stayed green at 103 passed.
# * A bare filename is matched only as a WHOLE span. ``uv.lock``,
#   ``pyproject.toml`` and ``SECURITY.md`` have no directory prefix to
#   disambiguate them, and the section also quotes them inside prose commands
#   (``git diff uv.lock``) where the token is part of a command rather than a
#   reference to the root file. Keeping those whole-span preserves the previous
#   behaviour exactly, so this change only ever ADDS collected paths.
DEPENDENCY_SECTION_PATH_PATTERN = r"""(?:scripts|examples)/[^\s`'",;)]+"""
DEPENDENCY_SECTION_BARE_FILE_NAMES = frozenset({"pyproject.toml", "SECURITY.md", "uv.lock"})
_BACKTICK_SPAN_PATTERN = r"`([^`]+)`"

# Floor for the parametrize below, which is evaluated at COLLECTION time: a
# matcher that stops matching collects zero tests and reports success instead of
# failing. Named as the one path whose absence was the actual defect rather than
# as a count, because a count floor is satisfied by the four whole-span paths
# that were already collected while the script path stays missing -- which is
# precisely the state the review found.
DEPENDENCY_SECTION_REQUIRED_PATH = "scripts/dependency_security.py"

DEPENDENCY_SECURITY_ACTION = (
    REPO_ROOT / ".github" / "actions" / "dependency-security" / "action.yml"
)


def _dependency_section() -> str:
    text = _skill_text()
    start = text.index(DEPENDENCY_SECTION_HEADING)
    end = text.index("\n## ", start + len(DEPENDENCY_SECTION_HEADING))
    return text[start:end]


def _dependency_section_paths() -> set[str]:
    """Repo paths the dependency section names, read out of its backtick spans."""
    paths: set[str] = set()
    for span in re.findall(_BACKTICK_SPAN_PATTERN, _dependency_section()):
        span = span.strip()
        if span in DEPENDENCY_SECTION_BARE_FILE_NAMES:
            paths.add(span)
        paths.update(re.findall(DEPENDENCY_SECTION_PATH_PATTERN, span))
    return paths


class TestDependencySectionMatchesTheRepo:
    """#1120: the lockfile and CVE-floor rules must point at real files and versions."""

    def test_the_section_exists(self):
        assert DEPENDENCY_SECTION_HEADING in _skill_text()

    def test_the_pre_pr_checklist_links_to_it(self):
        assert "(#dependency-and-lockfile-changes)" in _skill_text()

    def test_the_named_path_guard_collects_the_script_the_section_runs(self):
        """Non-vacuity floor for the parametrize below, and the P3-4 regression.

        The gate is #1120 AC6: every script path the section names must exist.
        The section names exactly one, inside the command span ``uv run python
        scripts/dependency_security.py``, so a matcher that only accepts a path
        occupying a whole span collects the four whole-span paths, passes, and
        checks the script not at all. Asserting the path is COLLECTED rather
        than counting collected paths is what distinguishes those two states.
        """
        collected = _dependency_section_paths()
        assert DEPENDENCY_SECTION_REQUIRED_PATH in collected, (
            f"{DEPENDENCY_SECTION_REQUIRED_PATH!r} is not among the paths collected from "
            f"the dependency section: {sorted(collected)}. Either the section no longer "
            "names the dependency gate -- in which case rule 4 has changed and this floor "
            "should move deliberately -- or DEPENDENCY_SECTION_PATH_PATTERN stopped "
            "matching inside backtick spans, which makes the existence check below "
            "silently skip the only script path the section has."
        )

    @pytest.mark.parametrize("quoted", sorted(_dependency_section_paths()))
    def test_every_named_path_exists(self, quoted: str):
        path = REPO_ROOT / quoted.removesuffix("**").rstrip("/")
        assert path.exists(), f"{quoted!r} is named but does not exist"

    def test_security_md_names_the_trivy_version_ci_pins(self):
        pinned = re.search(r"version:\s*v?(\d+\.\d+\.\d+)", DEPENDENCY_SECURITY_ACTION.read_text())
        assert pinned, "the dependency-security action no longer pins a Trivy version"
        security = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
        assert f"Trivy {pinned.group(1)}" in security, (
            f"the skill sends readers to SECURITY.md for the Trivy version, but SECURITY.md "
            f"does not name the pinned {pinned.group(1)}"
        )


# --- Absolute self-links, which CI's link checker does not see ----------------
#
# N6 rewrote this skill's relative links as absolute
# ``https://github.com/awslabs/cli-agent-orchestrator/blob/main/...`` URLs, so
# they resolve from the packaged mirror and the plugin copy, where ``../../``
# does not. That fix silently removed them from the repo's link validation:
# ``utils/markdown_links.py`` returns ``None`` for any destination carrying a
# scheme or a host, so ``scripts/validate_markdown_links.py`` checks RELATIVE
# links only. Review on #1121 filed this as R8 -- six links that can now rot
# with nothing noticing.
#
# These guards restore the coverage for the links that point back into this
# repository, which are exactly the ones a repo test can check: the path must
# exist in the tree, and an anchor must name a heading GitHub would actually
# generate. Links to anywhere else (agentskills.io, agent-plugins.org) are out
# of scope -- an external URL cannot be verified without network access, and a
# test that needs the network is not a test this suite can run.

CONTRIBUTING_SKILL_DIR = REPO_ROOT / "skills" / "cao-contributing"
BLOB_MAIN_PREFIX = "https://github.com/awslabs/cli-agent-orchestrator/blob/main/"
_ABSOLUTE_SELF_LINK_PATTERN = re.compile(
    re.escape(BLOB_MAIN_PREFIX) + r"(?P<path>[^)\s#]+)(?:#(?P<anchor>[^)\s]+))?"
)
# On a file GitHub does not render as Markdown, the only anchor that means
# anything is a line reference (``#L42``, ``#L10-L20``).
_LINE_ANCHOR_PATTERN = re.compile(r"^L\d+(?:-L\d+)?$")


class AbsoluteSelfLink(NamedTuple):
    """One ``blob/main`` link: the repo path, its anchor if any, and where it was found."""

    path: str
    anchor: str | None
    source: str


# Floors for the parametrize below, which is evaluated at COLLECTION time: a
# collector that stops matching yields zero cases and reports success. Two
# floors rather than one, because the test has two arms and a count alone
# leaves the second vacuous -- if every anchored link were reworded away, the
# heading-slug half would check nothing while the total still looked healthy.
# Both are stated low deliberately: text moving between SKILL.md and
# references/ must not redden this.
MINIMUM_ABSOLUTE_SELF_LINKS = 3
MINIMUM_ANCHORED_SELF_LINKS = 1


def _skill_markdown_files(skill_dir: Path) -> list[Path]:
    """``SKILL.md`` plus any ``references/`` page, which is where long sections go.

    Both are scanned because progressive disclosure MOVES prose rather than
    rewriting it: a link that lived in ``SKILL.md`` yesterday can be in
    ``references/`` today, and a guard anchored to ``SKILL.md`` alone would
    quietly stop checking it at the moment it moved.
    """
    files = [skill_dir / "SKILL.md"]
    files.extend(sorted((skill_dir / "references").glob("*.md")))
    return [path for path in files if path.is_file()]


def _absolute_self_links(skill_dir: Path = CONTRIBUTING_SKILL_DIR) -> list[AbsoluteSelfLink]:
    links: list[AbsoluteSelfLink] = []
    for path in _skill_markdown_files(skill_dir):
        text = path.read_text(encoding="utf-8")
        for match in _ABSOLUTE_SELF_LINK_PATTERN.finditer(text):
            links.append(
                AbsoluteSelfLink(
                    path=match.group("path"),
                    anchor=match.group("anchor"),
                    source=path.name,
                )
            )
    return links


def _github_heading_slugs(markdown: str) -> set[str]:
    """The anchors GitHub generates for a Markdown file's ATX headings.

    Mirrors ``github-slugger``, which is what GitHub runs: take the heading's
    rendered text, lowercase it, drop every character that is not a letter,
    digit, space, hyphen or underscore, then replace spaces with hyphens. A
    repeated slug gets ``-1``, ``-2``, ... appended, so the second
    ``## Notes`` is reachable as ``#notes-1``.

    Fenced blocks are skipped. A ``# comment`` inside a ``bash`` fence is not a
    heading, and admitting it would invent slugs -- which would make a WRONG
    anchor pass, the one direction that matters here.
    """
    slugs: set[str] = set()
    seen: dict[str, int] = {}
    in_fence = False
    for line in markdown.splitlines():
        if re.match(r"^\s*(```|~~~)", line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        heading = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if not heading:
            continue
        text = heading.group(2)
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # a link contributes its text
        text = text.replace("`", "")
        slug = re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")
        if not slug:
            continue
        occurrence = seen.get(slug, 0)
        seen[slug] = occurrence + 1
        slugs.add(slug if occurrence == 0 else f"{slug}-{occurrence}")
    return slugs


def _absolute_link_problem(link: AbsoluteSelfLink, repo_root: Path = REPO_ROOT) -> str | None:
    """Why this link is broken, or ``None``.

    Separated from the assertion so the negative controls below can exercise it
    on synthetic links. A guard whose only input is the current tree cannot
    demonstrate that it would fail on a bad one.
    """
    target = repo_root / link.path
    if not target.exists():
        return (
            f"{link.source} links to {BLOB_MAIN_PREFIX}{link.path}, but "
            f"{link.path!r} does not exist in the repo"
        )
    if link.anchor is None:
        return None
    if target.suffix.lower() != ".md":
        if _LINE_ANCHOR_PATTERN.match(link.anchor):
            return None
        return (
            f"{link.source} puts the anchor #{link.anchor} on {link.path!r}, which GitHub "
            "does not render as Markdown; only a line anchor such as #L42 resolves there"
        )
    slugs = _github_heading_slugs(target.read_text(encoding="utf-8"))
    if link.anchor not in slugs:
        return (
            f"{link.source} links to {link.path}#{link.anchor}, but that file has no heading "
            f"whose GitHub anchor is {link.anchor!r}. Available: {sorted(slugs)}"
        )
    return None


class TestAbsoluteSelfLinksResolve:
    """R8: the absolute links CI's validator skips, checked here instead."""

    def test_the_validator_really_does_skip_these_links(self):
        """Anchor the premise to the validator's BEHAVIOUR, not to its source text.

        This used to assert an exact indented source string, so reformatting
        ``markdown_links.py`` would have broken it for no real reason (round-2
        review). Calling the function is both sturdier and a stronger claim: it
        shows the validator returns no error for an absolute URL that could not
        possibly resolve, which is precisely why the links below need checking
        here.

        If the validator ever starts checking absolute URLs, this fails and
        this class becomes duplicate coverage -- remove it deliberately then,
        rather than leaving two checkers with different rules.
        """
        from markdown_it import MarkdownIt

        from cli_agent_orchestrator.utils import markdown_links

        unresolvable = f"{BLOB_MAIN_PREFIX}NO_SUCH_FILE_ANYWHERE.md#no-such-anchor"
        skipped = markdown_links._validate_destination(
            repo_root=REPO_ROOT,
            source=SKILL,
            destination=unresolvable,
            line=1,
            headings={},
            parser=MarkdownIt(),
        )
        assert skipped is None, (
            "scripts/validate_markdown_links.py now reports absolute URLs "
            f"({skipped}). This class exists only because it does not -- if that "
            "changed, delete these guards on purpose instead of keeping two "
            "validators with different rules."
        )
        # Control: the same function DOES report a relative path that is missing,
        # so the None above is the scheme short-circuit rather than a validator
        # that reports nothing at all.
        reported = markdown_links._validate_destination(
            repo_root=REPO_ROOT,
            source=SKILL,
            destination="NO_SUCH_FILE_ANYWHERE.md",
            line=1,
            headings={},
            parser=MarkdownIt(),
        )
        assert reported is not None, (
            "The validator reported nothing for a MISSING RELATIVE path either, so "
            "the assertion above proves nothing about absolute URLs specifically. "
            "Check how _validate_destination is being called."
        )

    def test_the_guard_has_links_to_check(self):
        links = _absolute_self_links()
        assert len(links) >= MINIMUM_ABSOLUTE_SELF_LINKS, (
            f"Expected at least {MINIMUM_ABSOLUTE_SELF_LINKS} absolute {BLOB_MAIN_PREFIX}... "
            f"links across {CONTRIBUTING_SKILL_DIR.relative_to(REPO_ROOT)}, found {links}. "
            "If N6's absolute links were reverted to relative ones, CI's validator covers "
            "them again and this floor should be lowered deliberately -- otherwise the "
            "parametrized check below collects nothing and passes silently."
        )

    def test_the_guard_has_an_anchored_link_to_check(self):
        """The heading-slug arm needs its own floor; a total count does not cover it."""
        anchored = [link for link in _absolute_self_links() if link.anchor]
        assert len(anchored) >= MINIMUM_ANCHORED_SELF_LINKS, (
            f"Expected at least {MINIMUM_ANCHORED_SELF_LINKS} absolute self-link with an "
            f"anchor, found {anchored}. Without one, the heading-slug half of this guard "
            "verifies nothing while the link count still looks healthy."
        )

    @pytest.mark.parametrize(
        "link", _absolute_self_links(), ids=lambda link: f"{link.source}:{link.path}"
    )
    def test_every_absolute_self_link_resolves(self, link: AbsoluteSelfLink):
        problem = _absolute_link_problem(link)
        assert problem is None, (
            f"{problem}. These links are invisible to scripts/validate_markdown_links.py, "
            "so nothing else in CI will catch this."
        )

    @pytest.mark.parametrize(
        "link,expected_fragment",
        [
            # A path that does not exist -- the R8 failure mode itself.
            (AbsoluteSelfLink("SECURITY.md", None, "SKILL.md"), None),
            (AbsoluteSelfLink("NO_SUCH_FILE.md", None, "SKILL.md"), "does not exist"),
            (AbsoluteSelfLink("docs/no_such_doc.md", None, "SKILL.md"), "does not exist"),
            # A real file, with an anchor no heading produces.
            (AbsoluteSelfLink("SECURITY.md", "full-dependency-gate", "SKILL.md"), None),
            (AbsoluteSelfLink("SECURITY.md", "no-such-heading", "SKILL.md"), "no heading"),
            # An anchor on a file GitHub does not render as Markdown.
            (AbsoluteSelfLink("mypy-baseline.json", None, "SKILL.md"), None),
            (AbsoluteSelfLink("mypy-baseline.json", "L1", "SKILL.md"), None),
            (
                AbsoluteSelfLink("mypy-baseline.json", "some-heading", "SKILL.md"),
                "does not render as Markdown",
            ),
        ],
    )
    def test_it_distinguishes_a_broken_link_from_a_working_one(
        self, link: AbsoluteSelfLink, expected_fragment: str | None
    ):
        """Non-vacuity, enforced rather than demonstrated once by hand.

        Each case is a link that MUST be judged broken, paired with one that
        must not. Without these, a collector or a slugger that silently stopped
        working would leave the parametrized test above green on a rotten tree.
        """
        problem = _absolute_link_problem(link)
        if expected_fragment is None:
            assert problem is None, f"{link} should resolve, but was reported as: {problem}"
        else:
            assert problem is not None, f"{link} is broken, but the guard reported no problem"
            assert expected_fragment in problem, (
                f"{link} was reported, but the message does not explain the real cause "
                f"({expected_fragment!r} not in {problem!r})"
            )


class TestTheGithubSlugRule:
    """Pins the slugger, because the tree exercises almost none of it.

    ``cao-contributing`` has one anchored link today, so the real tree would
    not notice the rule being wrong about bold text, punctuation, code spans or
    duplicate headings. GitHub's own algorithm is the contract here.
    """

    @pytest.mark.parametrize(
        "markdown,expected",
        [
            ("## Full dependency gate", {"full-dependency-gate"}),
            # Case, punctuation and emphasis are dropped; underscores are kept.
            ("# Mixed CASE Heading", {"mixed-case-heading"}),
            ("## **Bold** heading", {"bold-heading"}),
            ("## What's a gate?", {"whats-a-gate"}),
            ("## `make check-agent-plugin`", {"make-check-agent-plugin"}),
            ("## snake_case_name", {"snake_case_name"}),
            ("## A -- B", {"a----b"}),
            # A link in a heading contributes its text, not its URL.
            ("## [Releasing](docs/RELEASING.md)", {"releasing"}),
            # Duplicates: the second occurrence is reachable as `-1`.
            ("## Notes\n\n## Notes", {"notes", "notes-1"}),
            # A `#` inside a fence is a shell comment, not a heading.
            ("```bash\n# not a heading\n```\n## Real", {"real"}),
            # Setext headings are not ATX and are deliberately not handled;
            # recorded so the limitation is visible rather than surprising.
            ("Underlined\n==========", set()),
        ],
    )
    def test_slugs_match_githubs_rule(self, markdown: str, expected: set):
        assert _github_heading_slugs(markdown) == expected

    def test_the_real_anchor_target_is_covered_by_the_rule(self):
        """Keeps the table above honest: it models this repo, not an invention."""
        slugs = _github_heading_slugs((REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8"))
        assert "full-dependency-gate" in slugs, (
            "SECURITY.md no longer has a heading that slugs to 'full-dependency-gate'. "
            "The skill links there with that anchor; fix one or the other."
        )


# NOTE: there is deliberately no test here that the packaged mirror matches
# ``skills/cao-contributing/SKILL.md``. It would duplicate
# ``test/test_skill_packaging_parity.py::TestPackagingParity``
# ``::test_every_file_is_byte_identical[cao-contributing]``, which auto-parametrizes
# over ``SHIPPED_SKILLS`` and compares with ``filecmp.cmp(..., shallow=False)`` over
# EVERY file in the skill directory -- a strict superset of a single ``SKILL.md``
# text comparison. Removed rather than kept after confirming that test id is
# collected; re-add only if cao-contributing ever leaves ``SHIPPED_SKILLS``.
