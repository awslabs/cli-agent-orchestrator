"""Both `detail` readers, across every shape this API produces (issue #583 Bolt 3).

``authoring-sequence`` changed the shape of a value **two independent readers already parse**, so every
way this goes wrong is a reliability failure. The readers are:

* ``cli/commands/workflow.py::_extract_detail`` -- 28 call sites, all the workflow verbs.
* ``mcp_server/server.py::_extract_error_detail`` -- 22 call sites, all 15 workflow tools.

A THIRD copy exists at ``mcp_server/app_tools.py`` and is deliberately NOT covered here: it serves
``/sessions`` and ``/terminals/*`` only and cannot reach either object shape. Its own tests live in
``test/mcp_server/test_app_tools.py``.

THE TWO READERS DEGRADED DIFFERENTLY BEFORE THIS UNIT, and only one was noticeable -- which is why
``test_the_mcp_reader_surfaces_an_object_message`` is asserted directly rather than inferred from the
CLI's behaviour:

* ``_extract_detail`` did ``str(body["detail"])`` -> a Python dict repr shown to a human. **Loud.**
* ``_extract_error_detail`` did ``isinstance(detail, str)`` -> the fallback, so an agent received
  ``"status 403"`` with the plan_id gone. **Silent**, and a silent degradation is not the one you
  notice.

The ``list`` shape is LIVE, not defensive programming: FastAPI's own request-validation errors produce
``{"detail": [{"loc": ..., "msg": ...}]}`` and this repo carries a fixture of exactly that at
``tui/src/server.rs``:2298. Subscripting one with a string key raises.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.cli.commands.workflow import _extract_detail
from cli_agent_orchestrator.mcp_server.server import _extract_error_detail

READERS = {
    "cli": _extract_detail,
    "mcp": _extract_error_detail,
}

REFUSAL = {
    "kind": "approval_required",
    "plan_id": "plan-v1:abc",
    "message": "Plan 'plan-v1:abc' has not been approved.",
}
FINDINGS = {
    "findings": [
        {
            "severity": "error",
            "rule_id": "missing-recovery-policy",
            "line": 7,
            "message": "step() call has no recovery= keyword",
        }
    ]
}

# Every shape this API produces, with the body that carries it.
SHAPES = {
    "object_with_message": {"detail": REFUSAL},
    "object_with_findings": {"detail": FINDINGS},
    "object_with_neither": {"detail": {"unrecognised": 1}},
    "list_detail": {"detail": [{"loc": ["query", "x"], "msg": "field required"}]},
    "plain_string": {"detail": "boom"},
    "empty_string": {"detail": ""},
    "no_detail_key": {"unrelated": 1},
    "body_is_a_list": ["not", "a", "dict"],
}


def _response(payload=None, *, raises: bool = False):
    def _json():
        if raises:
            raise ValueError("not json")
        return payload

    return SimpleNamespace(json=_json, status_code=403, text="")


# ---------------------------------------------------------------------------
# The invariant: every shape, every reader, a usable string and never a raise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reader_name", sorted(READERS))
@pytest.mark.parametrize("shape_name", sorted(SHAPES))
def test_every_shape_returns_a_non_empty_string_and_never_raises(reader_name, shape_name):
    """Sixteen cases. The point is TOTALITY, which no type checker will give us here --
    ``mypy`` runs ``continue-on-error`` in CI and cannot fail a PR, so a shape mistake in a
    function like this has no other net."""
    reader = READERS[reader_name]

    result = reader(_response(SHAPES[shape_name]), "FALLBACK")

    assert isinstance(result, str) and result, f"{reader_name} returned {result!r} for {shape_name}"


@pytest.mark.parametrize("reader_name", sorted(READERS))
def test_a_non_json_body_falls_back(reader_name):
    assert READERS[reader_name](_response(raises=True), "FALLBACK") == "FALLBACK"


# ---------------------------------------------------------------------------
# The object arm, per reader
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reader_name", sorted(READERS))
def test_an_object_detail_yields_its_message(reader_name):
    assert READERS[reader_name](_response({"detail": REFUSAL}), "FALLBACK") == REFUSAL["message"]


def test_the_mcp_reader_surfaces_an_object_message(reader_name="mcp"):
    """ASSERTED DIRECTLY, not inferred from the CLI's behaviour.

    This is the reader whose old body returned the fallback for any non-string ``detail``. Changing the
    response shape without changing this function would have replaced a prose message CONTAINING the
    plan_id with the bare string ``"status 403"`` -- strictly worse than what it replaced, with no
    error, no log, and nothing failing. That is the defect this whole unit most easily could have
    shipped while looking, in the diff, like an improvement.
    """
    result = _extract_error_detail(_response({"detail": REFUSAL}), "status 403")

    assert result == REFUSAL["message"]
    assert result != "status 403", "the silent-degradation path"


@pytest.mark.parametrize("reader_name", sorted(READERS))
def test_a_findings_detail_renders_all_four_finding_fields(reader_name):
    """The 422 lint body, which BOTH readers were swallowing before this unit.

    An agent calling ``workflow_run`` on a spec with a lint error received ``"status 422"`` and nothing
    else. All four fields are rendered because ``line`` is a REQUIRED 1-based anchor (FR-2.3) -- it is
    the field a finding exists to provide.
    """
    result = READERS[reader_name](_response({"detail": FINDINGS}), "FALLBACK")

    assert "error" in result
    assert "missing-recovery-policy" in result
    assert "7" in result, "the line number is the actionable part"
    assert "no recovery= keyword" in result


@pytest.mark.parametrize("reader_name", sorted(READERS))
def test_the_rendered_findings_never_echo_source(reader_name):
    """A spec may contain a credential the operator pasted by mistake, and terminal output is the
    easiest place for that to leak -- the reason ``_echo_spec_result`` already records. A finding names
    a line so the AUTHOR can look."""
    secret = "AKIAIOSFODNN7EXAMPLE"
    body = {
        "detail": {
            "findings": [
                {
                    "severity": "error",
                    "rule_id": "disallowed-import",
                    "line": 3,
                    "message": "import cli_agent_orchestrator is banned",
                    # A hostile/verbose server could attach more; a renderer that dumped the whole
                    # finding would carry it through.
                    "source_line": f"TOKEN = '{secret}'",
                }
            ]
        }
    }

    result = READERS[reader_name](_response(body), "FALLBACK")

    assert secret not in result, "only the four LintFinding fields may be rendered"


# ---------------------------------------------------------------------------
# The string arm is permanent, not transitional
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reader_name", sorted(READERS))
def test_a_string_detail_still_works_and_this_is_not_a_legacy_path(reader_name):
    """REMOVING THE STRING ARM WOULD NOT BE A CLEANUP.

    Most of this API returns a string ``detail``; only three routes carry an object, for one condition
    each. And the MCP server can be installed from a DIFFERENT REVISION than the running API server --
    the published ``cao-mcp-server`` package is how CAO wires into MCP hosts -- so a newer reader must
    keep working against an older server that has never heard of the object shape.
    """
    assert READERS[reader_name](_response({"detail": "unknown workflow 'x'"}), "FB") == (
        "unknown workflow 'x'"
    )
