"""Tests for the plan-approval gate (issue #583 Bolt 2 ``approval-gate``, Bolt 3
``approval-enforcement-default``).

Four carry the unit's load:

* ``test_a_disabled_gate_does_not_consult_the_approval_store_at_all`` — the configured-off path.
  Enforcement defaulted OFF in Bolt 2 and defaults ON since Bolt 3, so this is no longer a test about
  the default; what it still guarantees is that the disabled path cannot fail a run through a store
  error, which matters MORE after the flip rather than less.
* ``test_a_yaml_run_is_never_gated_even_with_enforcement_on`` — C-1's harder half. YAML runs never freeze a
  manifest, so a gate that keyed off "manifest present" instead of the tier would refuse all of them.
  This is what stopped the Bolt 3 flip from silently starting to gate YAML.
* ``test_the_env_var_cannot_turn_the_gate_off`` — the asymmetric precedence. Written as an assertion about
  PRECEDENCE rather than about a value, because an implementation that followed the house
  ``env > file`` rule by habit would pass every other test in this file.
* ``test_the_two_refusal_causes_are_distinguishable_by_type`` — Bolt 3. The two causes call for
  different operator actions (retry vs approve), and a test asserting only "it refuses" passes
  whether or not they are distinguishable.
"""

import json

import pytest

from cli_agent_orchestrator.services import approval_gate, approval_store, settings_service
from cli_agent_orchestrator.services.approval_gate import (
    PlanApprovalRequiredError,
    PlanIdentityUnavailableError,
    ensure_plan_approved,
    plan_id_from_manifest,
)

PLAN = "plan-v1:abc123"
MANIFEST = json.dumps({"plan_id": PLAN, "source_hash": "sha256:deadbeef"})


@pytest.fixture()
def enforcement_on(monkeypatch):
    monkeypatch.setattr(approval_gate, "is_workflow_approval_required", lambda: True)


@pytest.fixture()
def approved(monkeypatch):
    """Approve exactly ``PLAN`` and nothing else, without touching a database."""
    monkeypatch.setattr(approval_store, "is_approved", lambda plan_id: plan_id == PLAN)


@pytest.fixture()
def nothing_approved(monkeypatch):
    monkeypatch.setattr(approval_store, "is_approved", lambda plan_id: False)


# ---------------------------------------------------------------------------
# The three load-bearing properties
# ---------------------------------------------------------------------------


def test_a_disabled_gate_does_not_consult_the_approval_store_at_all(monkeypatch):
    """``is_approved`` must not even be consulted when the gate is off.

    Renamed at Bolt 3: enforcement no longer defaults off, so this is a test about the CONFIGURED-off
    path rather than about the default (the default is now covered by
    ``test_the_setting_defaults_to_enabled_with_no_settings_file_at_all``). The property it guards
    matters MORE after the flip, not less — it is what keeps a store fault out of the disabled path
    entirely, so an operator who turned the gate off cannot have runs refused by a database error.
    """
    consulted = []
    monkeypatch.setattr(approval_store, "is_approved", lambda p: consulted.append(p) or False)
    monkeypatch.setattr(approval_gate, "is_workflow_approval_required", lambda: False)

    ensure_plan_approved(tier="script", manifest_json=MANIFEST)  # must not raise

    assert consulted == [], "the disabled path must not consult the approval store at all"


def test_a_yaml_run_is_never_gated_even_with_enforcement_on(enforcement_on, nothing_approved):
    """A YAML run never freezes a manifest, so a manifest-keyed gate would refuse every one of them."""
    ensure_plan_approved(tier="yaml", manifest_json=None)  # must not raise


def test_the_env_var_cannot_turn_the_gate_off(monkeypatch, tmp_path):
    """ASYMMETRIC PRECEDENCE: env may enable, only settings.json may disable.

    Asserted as a statement about precedence, not about a value: an implementation that followed the
    house ``env > file > default`` rule would pass every other test here and fail only this one.
    """
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"workflow": {"require_approval": True}}))
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", settings_file)

    for disabling_value in ("0", "false", "no", ""):
        monkeypatch.setenv("CAO_WORKFLOW_REQUIRE_APPROVAL", disabling_value)
        assert settings_service.is_workflow_approval_required() is True, (
            f"env value {disabling_value!r} must NOT be able to disable the gate — a control the "
            "environment can switch off is not a control"
        )


# ---------------------------------------------------------------------------
# Enforcement on
# ---------------------------------------------------------------------------


def test_an_unapproved_plan_is_refused_and_the_refusal_carries_the_plan_id(
    enforcement_on, nothing_approved
):
    """Without the plan_id an operator meeting a first-run refusal has nothing to act on."""
    with pytest.raises(PlanApprovalRequiredError) as excinfo:
        ensure_plan_approved(tier="script", manifest_json=MANIFEST)

    assert excinfo.value.plan_id == PLAN
    assert PLAN in str(excinfo.value)


def test_an_approved_plan_proceeds(enforcement_on, approved):
    ensure_plan_approved(tier="script", manifest_json=MANIFEST)  # must not raise


def test_an_approval_for_a_different_plan_does_not_admit_this_one(enforcement_on, approved):
    """The whole re-approval mechanism: a changed plan is a different plan_id."""
    other = json.dumps({"plan_id": "plan-v1:something-else"})
    with pytest.raises(PlanApprovalRequiredError):
        ensure_plan_approved(tier="script", manifest_json=other)


@pytest.mark.parametrize(
    "manifest",
    [
        None,
        "",
        "not json at all",
        "[]",
        '"a string"',
        "{}",
        json.dumps({"plan_id": None}),
        json.dumps({"plan_id": ""}),
    ],
)
def test_an_unreadable_manifest_refuses(enforcement_on, approved, manifest):
    """FAIL CLOSED — the promise both freeze call sites already make in writing.

    A freeze that failed writes NULL, and every shape of unreadable manifest must converge here
    rather than on permission. Since Bolt 3 it converges on the NARROWER type, so the assertion is
    about the type as well as the refusal.
    """
    with pytest.raises(PlanIdentityUnavailableError) as excinfo:
        ensure_plan_approved(tier="script", manifest_json=manifest)
    assert excinfo.value.plan_id is None


def test_a_database_error_refuses_rather_than_permits(enforcement_on, monkeypatch):
    """``approval_store.is_approved`` already answers False on sqlite3.Error; the gate must honour it."""
    monkeypatch.setattr(approval_store, "is_approved", lambda p: False)
    with pytest.raises(PlanApprovalRequiredError):
        ensure_plan_approved(tier="script", manifest_json=MANIFEST)


# ---------------------------------------------------------------------------
# The refusal type
# ---------------------------------------------------------------------------


def test_the_two_refusal_causes_are_distinguishable_by_type(enforcement_on, nothing_approved):
    """Bolt 3. The operator's next action differs, so "it refuses" is not enough.

    A test that only asserted ``pytest.raises(PlanApprovalRequiredError)`` on both causes would pass
    whether or not they were distinguishable, because the narrow type IS a subclass. The
    discrimination therefore has to be asserted with ``type(...) is``.
    """
    with pytest.raises(PlanApprovalRequiredError) as unapproved:
        ensure_plan_approved(tier="script", manifest_json=MANIFEST)
    with pytest.raises(PlanApprovalRequiredError) as unreadable:
        ensure_plan_approved(tier="script", manifest_json=None)

    assert type(unapproved.value) is PlanApprovalRequiredError, (
        "a readable-but-unapproved plan is a fact about the PLAN; reporting it as an unavailable "
        "identity would tell the operator to retry when they need to approve"
    )
    assert type(unreadable.value) is PlanIdentityUnavailableError
    assert unapproved.value.plan_id == PLAN
    assert unreadable.value.plan_id is None


def test_the_new_error_is_still_caught_by_the_original_handler():
    """BR-6's additive property: no existing catch site could start missing this condition.

    Every ``except PlanApprovalRequiredError`` in the API predates Bolt 3. If the new type were a
    sibling rather than a subclass, each of those sites would begin returning an unhandled 500.
    """
    assert issubclass(PlanIdentityUnavailableError, PlanApprovalRequiredError)

    try:
        raise PlanIdentityUnavailableError("x")
    except PlanApprovalRequiredError as caught:
        assert isinstance(caught, PlanIdentityUnavailableError)
    else:  # pragma: no cover - the except must fire
        pytest.fail("the base-class handler did not catch the subclass")


def test_the_narrow_handler_must_precede_the_broad_one_in_the_api():
    """The ordering is the requirement, and getting it wrong produces a PLAUSIBLE answer.

    Python matches ``except`` clauses in order, so a broad-first arrangement returns 403 for a failed
    freeze — no exception, no error, just a wrong status that reads as correct. Asserted structurally
    against the source because the runtime symptom is indistinguishable from correct behaviour at any
    single call site.
    """
    from pathlib import Path

    import cli_agent_orchestrator.api.main as api_main

    source = Path(api_main.__file__).read_text()
    narrow = "except approval_gate.PlanIdentityUnavailableError"
    broad = "except approval_gate.PlanApprovalRequiredError"

    narrow_positions = [i for i in range(len(source)) if source.startswith(narrow, i)]
    broad_positions = [i for i in range(len(source)) if source.startswith(broad, i)]

    assert len(narrow_positions) == 3, "all three gate call sites must transport the narrow cause"
    assert len(broad_positions) == 3
    for narrow_at, broad_at in zip(narrow_positions, broad_positions):
        assert narrow_at < broad_at, (
            "the PlanIdentityUnavailableError clause must come first at every site; ordered after "
            "its base class it is dead code and the response is silently 403"
        )


def test_the_refusal_is_not_a_valueerror():
    """The API resume handler ends with ``except ValueError -> 400``.

    Subclassing ValueError would transport a security refusal as a bad request, and a caller reading
    400 would go looking for a malformed argument instead of approving a plan.
    """
    assert not issubclass(PlanApprovalRequiredError, ValueError)


def test_the_refusal_is_distinct_from_the_resume_ladders_other_outcomes():
    from cli_agent_orchestrator.services import workflow_service

    assert not issubclass(PlanApprovalRequiredError, workflow_service.ResumeNotAllowedError)
    assert not issubclass(PlanApprovalRequiredError, workflow_service.ResumeCorruptError)
    assert not issubclass(PlanApprovalRequiredError, KeyError)


# ---------------------------------------------------------------------------
# plan_id extraction — total, and never recomputed
# ---------------------------------------------------------------------------


def test_plan_id_is_read_from_the_manifest_verbatim():
    assert plan_id_from_manifest(MANIFEST) == PLAN


def test_plan_id_extraction_is_total():
    """Every malformed shape answers None rather than raising — absence never becomes permission."""
    for bad in (None, "", "{", "null", "3", "[]", '{"plan_id": 7}', '{"other": "x"}'):
        assert plan_id_from_manifest(bad) is None


# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------


def test_the_env_var_can_turn_the_gate_on(monkeypatch, tmp_path):
    """The environment belongs to the CAO server process that resolves this setting."""
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", tmp_path / "absent.json")
    for enabling_value in ("1", "true", "yes", "TRUE", " 1 "):
        monkeypatch.setenv("CAO_WORKFLOW_REQUIRE_APPROVAL", enabling_value)
        assert settings_service.is_workflow_approval_required() is True


def test_the_setting_defaults_to_enabled_with_no_settings_file_at_all(monkeypatch, tmp_path):
    """THE FRESH-INSTALL CASE, and the one whose failure hides (issue #583 Bolt 3, SEC-4).

    Inverted from ``test_the_setting_defaults_to_disabled`` when ``approval-enforcement-default``
    flipped the default. Deliberately asserted with an ABSENT file rather than an empty one, because
    ``_load()`` returns ``{}`` for both an absent file and an unreadable one, and those must resolve
    OPPOSITELY. An implementation that conflated them toward "disabled" would leave the gate inert on
    every fresh installation while every OTHER test in this file — each of which supplies a settings
    file — still passed.
    """
    monkeypatch.delenv("CAO_WORKFLOW_REQUIRE_APPROVAL", raising=False)
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", tmp_path / "absent.json")

    posture = settings_service.resolve_workflow_approval_posture()
    assert posture.required is True
    assert posture.source == settings_service.GATE_SOURCE_DEFAULT, (
        "an absent file is the UNCONFIGURED case, not a read failure — reporting it as "
        "read-failure-fallback would make the startup line lie about a healthy installation"
    )
    assert settings_service.is_workflow_approval_required() is True


def test_settings_json_can_enable_and_disable(monkeypatch, tmp_path):
    monkeypatch.delenv("CAO_WORKFLOW_REQUIRE_APPROVAL", raising=False)
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", settings_file)

    settings_file.write_text(json.dumps({"workflow": {"require_approval": True}}))
    assert settings_service.is_workflow_approval_required() is True

    settings_file.write_text(json.dumps({"workflow": {"require_approval": False}}))
    assert settings_service.is_workflow_approval_required() is False


def test_an_unreadable_setting_resolves_to_disabled(monkeypatch, tmp_path, caplog):
    """The ONE place this mechanism is deliberately not fail-closed.

    Resolving an unparseable settings file to "gate on" would refuse every script run in the
    installation on the strength of a JSON typo.

    THE ORIGINAL JUSTIFICATION FOR THIS BEHAVIOUR EXPIRED AT BOLT 3 AND THE BEHAVIOUR DID NOT. It used
    to be that resolving to disabled "makes the unreadable case behave like the unconfigured case" —
    an equivalence the flipped default BREAKS, since unconfigured now means enabled. What keeps it
    correct is narrower: ``approval_gate``'s docstring records this as a same-user local control and
    not a privilege boundary, so a corrupt file weakening the gate is outside the threat model, while
    the installation-wide outage is not.

    The ``error`` record and the reported source are asserted, not incidental: without them the
    degradation is silent, and a silently weakened control is indistinguishable from a healthy one.
    """
    import logging

    monkeypatch.delenv("CAO_WORKFLOW_REQUIRE_APPROVAL", raising=False)
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{not valid json")
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", settings_file)

    with caplog.at_level(logging.ERROR, logger=settings_service.logger.name):
        posture = settings_service.resolve_workflow_approval_posture()

    assert posture.required is False
    assert posture.source == settings_service.GATE_SOURCE_READ_FAILURE
    assert any(r.levelno >= logging.ERROR for r in caplog.records), (
        "an unreadable settings file silently disables a control the operator believes defaults "
        "on — the error record is the only thing that makes it visible"
    )
    assert settings_service.is_workflow_approval_required() is False


def test_a_non_dict_workflow_section_resolves_to_disabled(monkeypatch, tmp_path, caplog):
    import logging

    monkeypatch.delenv("CAO_WORKFLOW_REQUIRE_APPROVAL", raising=False)
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"workflow": "not a dict"}))
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", settings_file)

    with caplog.at_level(logging.ERROR, logger=settings_service.logger.name):
        posture = settings_service.resolve_workflow_approval_posture()

    assert posture.required is False
    assert posture.source == settings_service.GATE_SOURCE_READ_FAILURE
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_an_empty_but_readable_settings_file_resolves_to_the_default(monkeypatch, tmp_path):
    """The other half of SEC-4: readable-and-empty is NOT a read failure.

    ``{}`` parses fine and configures nothing, so it takes the default (enabled) with source
    ``default`` — not ``read-failure-fallback``. Paired with the unreadable-file test above, this pins
    both directions of the distinction that ``_load()`` alone cannot make.
    """
    monkeypatch.delenv("CAO_WORKFLOW_REQUIRE_APPROVAL", raising=False)
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}")
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", settings_file)

    posture = settings_service.resolve_workflow_approval_posture()
    assert posture.required is True
    assert posture.source == settings_service.GATE_SOURCE_DEFAULT


def test_an_explicit_setting_reports_the_file_as_its_source(monkeypatch, tmp_path):
    """A deliberate opt-out must be distinguishable from a read failure in the startup line."""
    monkeypatch.delenv("CAO_WORKFLOW_REQUIRE_APPROVAL", raising=False)
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"workflow": {"require_approval": False}}))
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", settings_file)

    posture = settings_service.resolve_workflow_approval_posture()
    assert posture.required is False
    assert posture.source == settings_service.GATE_SOURCE_FILE, (
        "an operator who chose to turn the gate off and an operator with a corrupt file both see a "
        "disabled gate; only the source tells them apart"
    )


def test_the_enabling_env_var_reports_itself_as_the_source(monkeypatch, tmp_path):
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", tmp_path / "absent.json")
    monkeypatch.setenv("CAO_WORKFLOW_REQUIRE_APPROVAL", "1")

    posture = settings_service.resolve_workflow_approval_posture()
    assert posture.required is True
    assert posture.source == settings_service.GATE_SOURCE_ENV


def test_load_still_returns_an_empty_mapping_for_every_failure(monkeypatch, tmp_path):
    """``_load()``'s contract is unchanged by the sibling loader it now delegates to (BR-10).

    None of ``_load()``'s many other callers was edited by this unit, so its totality is what keeps
    every unrelated setting working. Asserted here rather than assumed.
    """
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", settings_file)

    assert settings_service._load() == {}  # absent

    settings_file.write_text("{not valid json")
    assert settings_service._load() == {}  # unparseable

    settings_file.write_text("[1, 2, 3]")
    assert settings_service._load() == {}  # parses, but not an object

    settings_file.write_text(json.dumps({"a": 1}))
    assert settings_service._load() == {"a": 1}


def test_a_vanished_settings_file_reads_as_absent_not_as_a_read_failure(monkeypatch, tmp_path):
    """One read, so there is no stat-then-read window (PERF-4).

    A file that does not exist raises FileNotFoundError from the single read, which is reported as
    ABSENT rather than as a failure. The distinction matters because absent enables the gate and a
    read failure disables it, so a spurious failure classification would weaken the control.
    """
    monkeypatch.delenv("CAO_WORKFLOW_REQUIRE_APPROVAL", raising=False)
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", tmp_path / "never-created.json")

    data, ok = settings_service._load_result()
    assert (data, ok) == ({}, True)


# ---------------------------------------------------------------------------
# The read path this unit had to add
# ---------------------------------------------------------------------------


def test_the_run_row_exposes_manifest_json():
    """``manifest-column`` added the column and ``manifest-freeze`` writes it, but nothing read it back.

    The resume gate is the first reader, so this unit added the field and the SELECT. Without it,
    ``row.manifest_json`` is an AttributeError at resume admission — a crash, not a refusal.
    """
    from cli_agent_orchestrator.services.workflow_journal import RunRow

    assert "manifest_json" in RunRow.__dataclass_fields__
    row = RunRow(
        run_id="r",
        workflow_name="w",
        spec_snapshot="{}",
        inputs_json="{}",
        state="RUNNING",
        current_step_id=None,
        started_at="t",
        finished_at=None,
    )
    assert row.manifest_json is None, "defaulted, so a pre-existing row reads back unchanged"
