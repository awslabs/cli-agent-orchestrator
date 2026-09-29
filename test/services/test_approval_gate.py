"""Tests for the plan-approval gate (issue #583 Bolt 2, unit ``approval-gate``).

Three carry the unit's load:

* ``test_enforcement_is_off_by_default_so_an_unapproved_script_run_proceeds`` — C-1. If this fails, every
  existing script-tier caller breaks, because a ``plan_id`` does not exist until run start and the first run
  of any new plan is therefore refused.
* ``test_a_yaml_run_is_never_gated_even_with_enforcement_on`` — C-1's harder half. YAML runs never freeze a
  manifest, so a gate that keyed off "manifest present" instead of the tier would refuse all of them.
* ``test_the_env_var_cannot_turn_the_gate_off`` — the asymmetric precedence. Written as an assertion about
  PRECEDENCE rather than about a value, because an implementation that followed the house
  ``env > file`` rule by habit would pass every other test in this file.
"""

import json

import pytest

from cli_agent_orchestrator.services import approval_gate, approval_store, settings_service
from cli_agent_orchestrator.services.approval_gate import (
    PlanApprovalRequiredError,
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
    monkeypatch.setattr(
        approval_store,
        "approval_state",
        lambda plan_id: approval_store.APPROVED if plan_id == PLAN else approval_store.ABSENT,
    )


@pytest.fixture()
def nothing_approved(monkeypatch):
    monkeypatch.setattr(approval_store, "approval_state", lambda plan_id: approval_store.ABSENT)


# ---------------------------------------------------------------------------
# The three load-bearing properties
# ---------------------------------------------------------------------------


def test_enforcement_is_off_by_default_so_an_unapproved_script_run_proceeds(monkeypatch):
    """C-1 and the default. The approval store must not even be consulted."""
    consulted = []
    monkeypatch.setattr(
        approval_store, "approval_state", lambda p: consulted.append(p) or approval_store.ABSENT
    )
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
    rather than on permission.
    """
    with pytest.raises(PlanApprovalRequiredError) as excinfo:
        ensure_plan_approved(tier="script", manifest_json=manifest)
    assert excinfo.value.plan_id is None


def test_a_store_fault_is_not_reported_as_a_missing_approval(enforcement_on, monkeypatch):
    """Issue #696 regression. A store fault must NOT reach the operator as "approve this plan".

    When the approval database is unreadable, the gate cannot know whether ``plan_id`` is approved.
    Reporting that as a missing approval hands the operator a remedy — "approve this plan identifier
    and run again" — that cannot work, because nothing about the plan is wrong. The refusal must be
    distinguishable from a genuine absence.
    """
    import sqlite3

    def unreadable_store(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(approval_store, "_connect", unreadable_store)

    with pytest.raises(
        Exception
    ) as excinfo:  # noqa: PT011 - asserting on the message, not the type
        ensure_plan_approved(tier="script", manifest_json=MANIFEST)

    message = str(excinfo.value)
    assert "has not been approved" not in message, (
        "a store fault must not be reported as a missing approval — the operator would be told to "
        "approve a plan that may already be approved, and the remedy cannot address a database fault"
    )
    assert "Approve this plan identifier" not in message


def test_a_database_error_refuses_rather_than_permits(enforcement_on, monkeypatch):
    """A store fault refuses rather than permits — the fail-closed posture is preserved.

    The refusal is now a distinct :class:`PlanApprovalUnavailableError` (issue #696) rather than
    :class:`PlanApprovalRequiredError`, but it is still a refusal: an unreadable store never admits
    a run.
    """
    import sqlite3

    def unreadable_store(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(approval_store, "_connect", unreadable_store)
    with pytest.raises(approval_gate.PlanApprovalUnavailableError):
        ensure_plan_approved(tier="script", manifest_json=MANIFEST)


def test_an_unknown_store_state_is_a_distinct_refusal_carrying_the_plan_id(
    enforcement_on, monkeypatch
):
    """Issue #696. UNKNOWN maps to its own refusal class, carries the plan_id, and is not a 400/absence.

    The distinction is the whole point: a caller branching on the refusal must be able to tell "the
    store is unreadable, retry" from "this plan is not approved, approve it".
    """
    monkeypatch.setattr(approval_store, "approval_state", lambda _plan_id: approval_store.UNKNOWN)

    with pytest.raises(approval_gate.PlanApprovalUnavailableError) as excinfo:
        ensure_plan_approved(tier="script", manifest_json=MANIFEST)

    assert excinfo.value.plan_id == PLAN, "the refusal must still carry the plan_id for diagnosis"
    assert not isinstance(excinfo.value, PlanApprovalRequiredError), (
        "a store fault is a fact about the store, not about the plan — the two refusals must be "
        "distinguishable so a caller does not apply the approve-this-plan remedy to a database fault"
    )
    assert not isinstance(
        excinfo.value, ValueError
    ), "transported as 503, so it must not be a ValueError the API's trailing 400 arm can claim"


# ---------------------------------------------------------------------------
# The refusal type
# ---------------------------------------------------------------------------


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


def test_the_setting_defaults_to_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("CAO_WORKFLOW_REQUIRE_APPROVAL", raising=False)
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", tmp_path / "absent.json")
    assert settings_service.is_workflow_approval_required() is False


def test_settings_json_can_enable_and_disable(monkeypatch, tmp_path):
    monkeypatch.delenv("CAO_WORKFLOW_REQUIRE_APPROVAL", raising=False)
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", settings_file)

    settings_file.write_text(json.dumps({"workflow": {"require_approval": True}}))
    assert settings_service.is_workflow_approval_required() is True

    settings_file.write_text(json.dumps({"workflow": {"require_approval": False}}))
    assert settings_service.is_workflow_approval_required() is False


def test_an_unreadable_setting_resolves_to_disabled(monkeypatch, tmp_path):
    """The ONE place this mechanism is deliberately not fail-closed.

    Resolving an unparseable settings file to "gate on" would refuse every script run in the
    installation on the strength of a JSON typo. The asymmetry above bounds the residual: an operator
    who enabled the gate via the environment is unaffected by a corrupt file.
    """
    monkeypatch.delenv("CAO_WORKFLOW_REQUIRE_APPROVAL", raising=False)
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{not valid json")
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", settings_file)

    assert settings_service.is_workflow_approval_required() is False


def test_a_non_dict_workflow_section_resolves_to_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("CAO_WORKFLOW_REQUIRE_APPROVAL", raising=False)
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"workflow": "not a dict"}))
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", settings_file)

    assert settings_service.is_workflow_approval_required() is False


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
