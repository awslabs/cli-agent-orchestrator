"""Stale-update rejection on the spec write path (issue #583, Bolt 3, unit 4).

**Closes FR-8's second criterion**: "an update presenting a stale source hash is
rejected". Bolt 2's Definition of Done had to declare this one unsatisfied because no
unit owned it.

Two tests carry the FR-8 claim, and the distinction between them is the point:

- ``test_rejects_a_deliberately_wrong_hash`` proves **the comparison works**.
- ``test_rejects_a_genuinely_stale_update_and_preserves_the_other_writers_content``
  proves **the criterion** — a real read-modify-write against content that changed
  since the read, with the other writer's content surviving.

A suite with only the first would pass while the requirement went untested in any
realistic shape (BR-3A4-9).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from cli_agent_orchestrator.models.workflow import StaleSpecError, TierCollisionError
from cli_agent_orchestrator.services import workflow_spec_service as svc

GOOD = "INPUTS = {'topic': {'type': 'string', 'required': True}}\n"
REVISED = "INPUTS = {'topic': {'type': 'string'}}\n# revised\n"


def _entries(base: Path) -> list[str]:
    return sorted(os.listdir(base))


# ---------------------------------------------------------------------------
# The FR-8 criterion
# ---------------------------------------------------------------------------
def test_rejects_a_genuinely_stale_update_and_preserves_the_other_writers_content(
    tmp_path: Path,
) -> None:
    """The realistic lost-update scenario, which is what FR-8 is actually about.

    An agent reads a spec, reasons for a while, and submits an update. Meanwhile
    someone else changed the file. Without this check the agent's write would land and
    the other writer's edit would vanish with no error.
    """
    created = svc.create_workflow("shared", GOOD, scan_dir=str(tmp_path))
    stale_hash = created.content_hash

    # Another writer changes the file after our read.
    (tmp_path / "shared.py").write_text("# someone else got here first\nINPUTS = {}\n")
    other_content = (tmp_path / "shared.py").read_bytes()

    with pytest.raises(StaleSpecError):
        svc.update_workflow("shared", REVISED, stale_hash, scan_dir=str(tmp_path))

    assert (
        tmp_path / "shared.py"
    ).read_bytes() == other_content, "the other writer's content must survive"
    assert _entries(tmp_path) == ["shared.py"], "no temp file left behind"


def test_rejects_a_deliberately_wrong_hash(tmp_path: Path) -> None:
    """Proves the comparison itself, independently of any concurrency scenario."""
    svc.create_workflow("wf", GOOD, scan_dir=str(tmp_path))
    original = (tmp_path / "wf.py").read_bytes()

    with pytest.raises(StaleSpecError):
        svc.update_workflow("wf", REVISED, "0" * 64, scan_dir=str(tmp_path))

    assert (tmp_path / "wf.py").read_bytes() == original


def test_a_matching_hash_lets_the_update_through(tmp_path: Path) -> None:
    """BR-3A4-10 — otherwise the check is a wall rather than a guard.

    This is the test that catches an INVERTED comparison, which a mismatch-only suite
    would happily pass.
    """
    created = svc.create_workflow("wf", GOOD, scan_dir=str(tmp_path))

    spec = svc.update_workflow("wf", REVISED, created.content_hash, scan_dir=str(tmp_path))

    assert (tmp_path / "wf.py").read_text() == REVISED
    assert spec.content_hash != created.content_hash


def test_the_error_carries_both_hashes_and_the_name(tmp_path: Path) -> None:
    """BR-3A4-3 — the caller must learn it should re-read, and a diagnostician which
    values disagreed."""
    created = svc.create_workflow("named", GOOD, scan_dir=str(tmp_path))
    (tmp_path / "named.py").write_text("# changed\n")
    actual = hashlib.sha256((tmp_path / "named.py").read_bytes()).hexdigest()

    with pytest.raises(StaleSpecError) as excinfo:
        svc.update_workflow("named", REVISED, created.content_hash, scan_dir=str(tmp_path))

    err = excinfo.value
    assert err.name == "named"
    assert err.expected == created.content_hash
    assert err.actual == actual
    assert "re-read" in str(err), "the message must tell the caller what to do"


def test_stale_spec_error_is_a_valueerror_subclass(tmp_path: Path) -> None:
    """So existing broad handlers still catch it, while the boundary can map 409.

    Mirrors ``TierCollisionError``'s design. Pass 3B owns the actual HTTP mapping.
    """
    assert issubclass(StaleSpecError, ValueError)
    assert StaleSpecError is not TierCollisionError


# ---------------------------------------------------------------------------
# Hash definition agreement (BR-3A4-5) — the fail-open risk
# ---------------------------------------------------------------------------
def test_the_computed_hash_agrees_with_what_get_workflow_reports(tmp_path: Path) -> None:
    """If these definitions diverged the check would fail OPEN in the worst way.

    A caller's hash from ``get_workflow`` would never match, every update would be
    refused, and the natural "fix" would be to weaken or delete the check. A control
    that misfires reliably gets removed.
    """
    svc.create_workflow("agree", GOOD, scan_dir=str(tmp_path))

    from_read_path = svc.get_workflow("agree", scan_dir=str(tmp_path)).content_hash
    computed = svc._current_source_hash(str(tmp_path / "agree.py"), os.path.realpath(str(tmp_path)))

    assert computed == from_read_path


def test_a_hash_obtained_from_get_workflow_is_accepted_for_an_update(
    tmp_path: Path,
) -> None:
    """The round trip a real caller performs: get, then update with what get returned."""
    svc.create_workflow("via-get", GOOD, scan_dir=str(tmp_path))
    got = svc.get_workflow("via-get", scan_dir=str(tmp_path))

    svc.update_workflow("via-get", REVISED, got.content_hash, scan_dir=str(tmp_path))

    assert (tmp_path / "via-get.py").read_text() == REVISED


# ---------------------------------------------------------------------------
# Ordering (BR-3A4-6)
# ---------------------------------------------------------------------------
def test_a_missing_spec_is_a_filenotfound_not_a_stale_error(tmp_path: Path) -> None:
    """The existence check precedes the comparison, so the clearer error wins."""
    with pytest.raises(FileNotFoundError):
        svc.update_workflow("absent", REVISED, "0" * 64, scan_dir=str(tmp_path))


def test_a_collision_is_a_collision_not_a_stale_error(tmp_path: Path) -> None:
    """The collision check also precedes the comparison."""
    (tmp_path / "clash.py").write_text(GOOD)
    (tmp_path / "clash.yaml").write_text("name: clash\nmode: sequential\nsteps: []\n")

    with pytest.raises(TierCollisionError):
        svc.update_workflow("clash", REVISED, "0" * 64, scan_dir=str(tmp_path))


def test_a_stale_update_is_refused_before_the_source_is_linted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The comparison precedes validation, so a stale update costs no lint pass.

    Proven by making the linter explode: if it were called, this test would see that
    error instead of ``StaleSpecError``.
    """
    created = svc.create_workflow("order", GOOD, scan_dir=str(tmp_path))
    (tmp_path / "order.py").write_text("# changed\n")

    def _must_not_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("lint_script must not run for a stale update")

    monkeypatch.setattr(svc, "lint_script", _must_not_run)

    with pytest.raises(StaleSpecError):
        svc.update_workflow("order", REVISED, created.content_hash, scan_dir=str(tmp_path))


# ---------------------------------------------------------------------------
# The required-parameter property (BR-3A4-1)
# ---------------------------------------------------------------------------
def test_expected_hash_has_no_default(tmp_path: Path) -> None:
    """FR-8 holds unconditionally only because there is no unguarded path.

    An optional parameter would make the guarantee opt-in, and an omitted-by-default
    argument is exactly what a caller leaves out. Asserted against the signature so a
    future default cannot be added silently.
    """
    import inspect

    param = inspect.signature(svc.update_workflow).parameters["expected_hash"]
    assert param.default is inspect.Parameter.empty
    assert param.annotation in ("str", str)
