"""FR-10's sequence, as the shipped skill actually states it (issue #583 Bolt 3).

``SKILL.md`` is a **functional artifact**, not prose: it is loaded into an authoring agent's context and
is the only thing that tells it which surfaces to call in what order. Unit ``authoring-mcp-tools``
established the same standing for tool descriptions, and a code sample is that claim in a stronger
form -- the worked example is the most-copied part of any skill, so it sets the default regardless of
what the guidance two sections above it says.

The two load-bearing tests here:

* ``test_the_worked_example_declares_a_recovery_policy`` -- the example shipped using ``run_step``, the
  form that declares NOTHING, while this unit exists partly to make ``step()``'s declared policy the
  norm. ``components.md``:269-272 records why it matters: emitting ``step()`` is what makes Bolt 1's
  ``missing-recovery-policy`` lint rule reachable from ordinary authoring at all.
* ``test_no_surface_retries_an_approval_refusal`` -- an ABSENCE, and one worth enforcing rather than
  remembering: a retry loop around a human authorisation gate is a bypass by repetition, and it is
  exactly the thing a well-meant "apply our retry helper consistently" change would add.

Both skill copies are checked, because they must stay byte-identical (``test_skill_packaging_parity``
owns that) and because a reader of either one is an agent about to act.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import cli_agent_orchestrator.api.main as api_main

REPO = Path(__file__).resolve().parents[1]
SKILL_COPIES = (
    REPO / "skills" / "cao-workflow" / "SKILL.md",
    REPO / "src" / "cli_agent_orchestrator" / "skills" / "cao-workflow" / "SKILL.md",
)


@pytest.fixture(params=SKILL_COPIES, ids=("repo_copy", "packaged_copy"))
def skill(request: pytest.FixtureRequest) -> str:
    return Path(request.param).read_text()


def _python_blocks(text: str) -> list[str]:
    return re.findall(r"```python\n(.*?)```", text, re.DOTALL)


def _observe_section(text: str) -> str:
    headings = list(re.finditer(r"(?m)^### g\. OBSERVE(?:[ \t].*)?$", text))
    assert len(headings) == 1, f"expected one exact OBSERVE section, found {len(headings)}"
    start = headings[0].end()
    next_heading = re.search(r"(?m)^#{1,6}[ \t]+", text[start:])
    assert next_heading, "OBSERVE section must end at a following Markdown heading"
    return text[start : start + next_heading.start()]


def _table_cells(line: str) -> list[str]:
    stripped = line.strip()
    assert stripped.startswith("|") and stripped.endswith(
        "|"
    ), "classification table rows must start and end with a pipe"
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", stripped[1:-1])]


def _classification_table_keys(text: str) -> set[str]:
    """Parse and validate the sole Markdown table in the exact OBSERVE section."""
    table_blocks: list[list[str]] = []
    current: list[str] = []
    for line in _observe_section(text).splitlines():
        if line.strip().startswith("|"):
            current.append(line)
        elif current:
            table_blocks.append(current)
            current = []
    if current:
        table_blocks.append(current)
    assert (
        len(table_blocks) == 1
    ), f"expected one unambiguous OBSERVE table, found {len(table_blocks)}"

    rows = [_table_cells(line) for line in table_blocks[0]]
    assert rows[0] == [
        "`classification`",
        "What it means",
        "What to do",
    ], f"unexpected classification table header: {rows[0]}"
    assert len(rows) >= 3, "classification table needs a separator and data rows"
    assert len(rows[1]) == 3 and all(
        re.fullmatch(r":?-{3,}:?", cell) for cell in rows[1]
    ), "classification table has an invalid Markdown separator"

    keys = []
    for cells in rows[2:]:
        assert len(cells) == 3, f"classification table row has {len(cells)} cells, expected 3"
        assert all(cells), "classification table data cells must all be non-empty"
        first_cell = cells[0]
        match = re.fullmatch(r"`([^`]+)`", first_cell)
        assert match, f"classification table key is not one backticked value: {first_cell!r}"
        keys.append(match.group(1))
    assert keys, "classification table must contain data rows"
    assert len(keys) == len(set(keys)), f"duplicate classification rows: {keys}"
    return set(keys)


def _api_classification_keys() -> set[str]:
    values = [value for name, value in vars(api_main).items() if name.startswith("CLASSIFICATION_")]
    assert values, "API must export its closed classification constants"
    assert all(
        isinstance(value, str) and value for value in values
    ), "API classification constants must be non-empty strings"
    assert len(values) == len(set(values)), "API classification constants must have unique values"
    return set(values)


def _assert_observe_classification_table(text: str) -> None:
    assert _classification_table_keys(text) == _api_classification_keys()


def _replace_in_observe(text: str, old: str, new: str) -> str:
    section = _observe_section(text)
    assert section.count(old) == 1
    return text.replace(section, section.replace(old, new, 1), 1)


# ---------------------------------------------------------------------------
# The worked example
# ---------------------------------------------------------------------------


def test_the_worked_example_declares_a_recovery_policy(skill):
    """``step(..., recovery=...)``, never ``run_step`` -- asserted on the AST, not on the prose.

    The shipped example used ``run_step``, so the single most-copied artifact in this skill taught the
    undeclared form while the section above it explained why declaring matters. A reader who copied it
    produced a script that ``validate`` reports as ``unenforced-recovery-policy``.
    """
    blocks = _python_blocks(skill)
    assert blocks, "the worked example is the point of this file"

    calls = [
        node
        for block in blocks
        for node in ast.walk(ast.parse(block))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    step_calls = [c for c in calls if c.func.id == "step"]
    run_step_calls = [c for c in calls if c.func.id == "run_step"]

    assert step_calls, "the example must emit step(), which is what makes the lint rule reachable"
    assert not run_step_calls, (
        "the example must not use run_step: it declares no policy, and an example outweighs the "
        "guidance above it"
    )
    for call in step_calls:
        assert any(
            kw.arg == "recovery" for kw in call.keywords
        ), "every step() in the example must show the policy being DECLARED, not defaulted"


def test_the_example_imports_what_it_calls(skill):
    """A copy-paste of the example must run. It imported ``run_step``; it now calls ``step``."""
    blocks = _python_blocks(skill)
    imported = {
        alias.name
        for block in blocks
        for node in ast.walk(ast.parse(block))
        if isinstance(node, ast.ImportFrom) and node.module == "cao_workflow"
        for alias in node.names
    }
    assert "step" in imported
    assert "run_step" not in imported


def test_the_recovery_declaration_is_shown_as_a_judgement(skill):
    """Not an incantation. The skill's own rule is that ``idempotent`` "grants nothing and protects
    nothing" -- so an example that declares it without saying why teaches it as boilerplate, which is
    precisely how a step that charges a card ends up carrying it."""
    assert "manual" in skill, "the honest alternative must appear beside the declaration"


# ---------------------------------------------------------------------------
# The sequence and the approve pause
# ---------------------------------------------------------------------------


def test_the_sequence_names_all_seven_steps_in_order(skill):
    steps = ["AUTHOR", "VALIDATE", "ASK", "PRESENT THE PLAN", "RUN", "RESUME", "OBSERVE"]
    positions = [skill.find(s) for s in steps]
    assert all(p > 0 for p in positions), dict(zip(steps, positions))
    assert positions == sorted(positions), f"out of order: {dict(zip(steps, positions))}"


def test_the_approve_pause_is_documented_as_correct_rather_than_left_to_be_read_as_a_defect(skill):
    """``unit-of-work.md``:87-92 assigns this sentence to this unit.

    FR-10's Fail condition is *asking the user to choose a format* -- not human involvement. Without
    this stated in the shipped text, a reader who meets the pause will reasonably conclude the
    requirement is unmet.
    """
    lowered = skill.lower()
    assert "not a defect" in lowered
    assert "choose a format" in lowered or "choosing a format" in lowered


def test_the_approve_step_names_the_command_and_says_no_tool_can_do_it(skill):
    assert "cao workflow approve" in skill
    lowered = skill.lower()
    assert "deliberately isn't one" in lowered or "deliberately is not one" in lowered, (
        "the absence of a grant tool must be stated as deliberate; an agent that reads it as an "
        "omission will go looking for one"
    )


def test_the_refusal_is_branched_on_a_field_not_a_message(skill):
    assert "approval_required" in skill
    assert "plan_identity_unavailable" in skill, (
        "both kinds, because retrying a 403 is a bypass and presenting an approval after a 503 sends "
        "someone hunting for a plan that was never readable"
    )


def test_the_first_run_being_refused_is_stated_as_by_design(skill):
    lowered = skill.lower()
    assert "refused" in lowered and "by design" in lowered


# ---------------------------------------------------------------------------
# AUTHOR, format, and OBSERVE
# ---------------------------------------------------------------------------


def test_author_routes_through_create_rather_than_a_raw_file_write(skill):
    """A raw write bypasses the pre-write tier-collision check, and the result is a spec that is
    unreachable the moment it lands: writing ``<name>.py`` beside an existing ``<name>.yaml`` succeeds,
    then every ``get_workflow("<name>")`` raises."""
    assert "cao workflow create" in skill
    assert "workflow_create(" in skill
    assert "collision" in skill.lower()


def test_the_agent_is_told_python_is_the_format_and_never_to_ask(skill):
    """FR-10's explicit Fail condition is *the user is asked to choose YAML or Python*.

    ASSERTED POSITIVELY, and the first attempt at this test got it wrong in a way worth recording: it
    checked that the phrase "choose between yaml and python" was ABSENT, and failed against correct
    text -- because the skill states "You are never asked to choose between YAML and Python, and you
    must never ask the user to". A substring absence cannot distinguish OFFERING a choice from
    FORBIDDING the offer, so it is the wrong shape for this property. What is checkable is that the
    skill declares the format and carries the prohibition.
    """
    # WHITESPACE-NORMALISED before matching. Second wrong turn on this one test: the prose is
    # hard-wrapped, so "you must never ask the user to" spans a line break and is not a contiguous
    # substring. Any prose assertion against a wrapped Markdown file needs this, and a raw `in` check
    # fails against text that is perfectly correct.
    lowered = " ".join(skill.lower().split())
    assert "python is the format" in lowered
    assert "never ask the user to" in lowered, "the prohibition must be explicit, not implied"
    assert "yaml" in lowered, (
        "YAML must still be MENTIONED — existing YAML workflows run, and a skill silent on that "
        "reads as though they stopped working"
    )


def test_observe_names_the_classification_field(skill):
    assert "failure_envelope.classification" in skill
    _assert_observe_classification_table(skill)


def test_observe_guard_tracks_the_full_api_classification_taxonomy(skill, monkeypatch):
    monkeypatch.setattr(api_main, "CLASSIFICATION_FUTURE", "future", raising=False)

    with pytest.raises(AssertionError):
        _assert_observe_classification_table(skill)


def test_observe_guard_ignores_out_of_section_decoy_table(skill):
    decoy = (
        "| `classification` | What it means | What to do |\n"
        "| --- | --- | --- |\n"
        "| `transient` | decoy | decoy |\n"
        "| `artifact_defect` | decoy | decoy |\n"
        "| `cancelled` | decoy | decoy |\n\n"
    )
    mutated = skill.replace("| `cancelled` |", "| `future` |", 1)

    with pytest.raises(AssertionError):
        _assert_observe_classification_table(decoy + mutated)


def test_observe_guard_rejects_truncated_one_cell_rows(skill):
    mutated = skill
    for key in _api_classification_keys():
        row = next(line for line in mutated.splitlines() if line.startswith(f"| `{key}` |"))
        mutated = mutated.replace(row, f"| `{key}` |", 1)

    with pytest.raises(AssertionError):
        _assert_observe_classification_table(mutated)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "| `classification` | What it means | What to do |",
            "| Classification | What it means | What to do |",
        ),
        ("| --- | --- | --- |", "| -- | --- | --- |"),
        (
            "| `transient` | a persisted timeout; the artifact may still be usable |",
            "| transient | a persisted timeout; the artifact may still be usable |",
        ),
        (
            "| `cancelled` | the run was deliberately cancelled |",
            "| `transient` | the run was deliberately cancelled |",
        ),
    ],
    ids=("header", "separator", "unbackticked-key", "duplicate-key"),
)
def test_observe_guard_rejects_malformed_table_contract(skill, old, new):
    with pytest.raises(AssertionError):
        _assert_observe_classification_table(_replace_in_observe(skill, old, new))


def test_observe_guard_rejects_a_second_table_inside_the_section(skill):
    second_table = (
        "\n| `classification` | What it means | What to do |\n"
        "| --- | --- | --- |\n"
        "| `transient` | duplicate | duplicate |\n"
    )
    mutated = skill.replace(
        "\n## Parameterized workflows", second_table + "\n## Parameterized workflows"
    )

    with pytest.raises(AssertionError):
        _assert_observe_classification_table(mutated)


# ---------------------------------------------------------------------------
# The absence: nothing retries a refusal
# ---------------------------------------------------------------------------


def test_no_surface_retries_an_approval_refusal():
    """An ABSENCE, enforced rather than remembered (unit 4's precedent).

    A retry around a human authorisation gate is a bypass by repetition. This is deliberately a source
    check rather than a behavioural one: the failure mode is someone adding a generic retry helper to
    "every workflow call", which no single behavioural test would catch.
    """
    sources = {
        "cli": (REPO / "src/cli_agent_orchestrator/cli/commands/workflow.py").read_text(),
        "mcp": (REPO / "src/cli_agent_orchestrator/mcp_server/server.py").read_text(),
        "api": (REPO / "src/cli_agent_orchestrator/api/main.py").read_text(),
    }
    for name, source in sources.items():
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.For, ast.While)):
                continue
            body = ast.get_source_segment(source, node) or ""
            assert not (
                "approval_required" in body or "PlanApprovalRequiredError" in body
            ), f"{name}: a loop encloses an approval refusal — that is a bypass by repetition"


def test_the_skill_tells_the_agent_to_stop_rather_than_retry(skill):
    lowered = skill.lower()
    assert "do not retry" in lowered
    assert "stop and wait" in lowered or "and **stop**" in lowered
