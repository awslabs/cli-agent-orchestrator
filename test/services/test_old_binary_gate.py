"""Exact-H_B old-binary gate: the real rollout authority.

The disposable rig executes H_B's actual list/query/watchdog/cleanup/
delete entrypoints (byte-exact from the pinned deployed-base ref) against
constructed v2 forward state under access tracing. The acceptance test
proves zero v2 visibility through the real gate; a contrived hostile
probe proves any v2 access/mutation fails migration/rollback. Synthetic
miniature repos remain unit fixtures for rig mechanics only — they never
satisfy this compatibility acceptance.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cli_agent_orchestrator.services import old_binary_rig, vintage_migration

REPO_ROOT = Path(__file__).resolve().parents[2]
H_B = old_binary_rig.DEFAULT_OLD_BINARY_REF

pytestmark = pytest.mark.skipif(
    not (REPO_ROOT / ".git").exists(), reason="exact-source rig needs the git checkout"
)


@pytest.fixture(scope="module")
def hb_verdict(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("hb-gate")
    return old_binary_rig.prove_old_binary_invisibility(repo=REPO_ROOT, ref=H_B, workdir=workdir)


def test_exact_hb_entrypoints_show_zero_v2_visibility(hb_verdict):
    """The real H_B rig: actual old entrypoints against v2 forward state."""
    assert hb_verdict.surfaces_checked > 0
    assert hb_verdict.zero_visibility, f"violations: {hb_verdict.violations}"


def test_hostile_probe_fails_the_gate(tmp_path):
    """Contrived normal-path failure: any v2 access fails the verdict."""
    verdict = old_binary_rig.prove_old_binary_invisibility(
        repo=REPO_ROOT,
        ref=H_B,
        workdir=tmp_path / "hostile",
        extra_probes=("hostile_cleanup",),
    )
    assert not verdict.zero_visibility
    assert any("hostile_cleanup" in violation for violation in verdict.violations)


def test_migration_refuses_on_gate_violation(tmp_path):
    """The real gate wiring: a failing verdict refuses with zero mutation."""
    db = tmp_path / "state.sqlite"
    failing = lambda: old_binary_rig.RigVerdict(  # noqa: E731
        zero_visibility=False, violations=("v2 access",), surfaces_checked=1
    )
    with pytest.raises(vintage_migration.OldBinaryGateRefused):
        vintage_migration.migrate_v2(db, old_binary_gate=failing)
    # Zero mutation: the v2 surface was never created.
    conn = sqlite3.connect(str(db))
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND "
                "name LIKE 'managed_launch_v2%'"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_migration_and_rollback_journal_the_gate_outcome(tmp_path):
    passing = lambda: old_binary_rig.RigVerdict(  # noqa: E731
        zero_visibility=True, violations=(), surfaces_checked=7
    )
    db = tmp_path / "state.sqlite"
    receipt = vintage_migration.migrate_v2(db, old_binary_gate=passing)
    assert receipt["action"] == "migrate"
    conn = sqlite3.connect(str(db))
    try:
        detail = conn.execute(
            "SELECT detail FROM v2_migration_journal WHERE event_id=?",
            (receipt["event_id"],),
        ).fetchone()[0]
        assert '"zero_visibility": true' in detail
        assert '"surfaces_checked": 7' in detail
    finally:
        conn.close()
    with pytest.raises(vintage_migration.OldBinaryGateRefused):
        vintage_migration.rollback_v2(
            db,
            old_binary_gate=lambda: old_binary_rig.RigVerdict(
                zero_visibility=False, violations=("v2 mutation",), surfaces_checked=1
            ),
        )
    # The v2 tables survive the refused rollback.
    conn = sqlite3.connect(str(db))
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND "
                "name='managed_launch_v2_terminals'"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()
    rolled_back = vintage_migration.rollback_v2(db, old_binary_gate=passing)
    assert rolled_back["action"] == "rollback"


def test_synthetic_fixture_still_valid_for_rig_mechanics(tmp_path):
    """The synthetic miniature repo remains a unit fixture — but it is
    never wired to the migration gate (see the H_B tests above)."""
    import subprocess

    toy = tmp_path / "toy"
    (toy / "src" / "toypkg").mkdir(parents=True)
    (toy / "src" / "toypkg" / "__init__.py").write_text("")
    (toy / "src" / "toypkg" / "probe.py").write_text("def main():\n    pass\n")
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init"], cwd=toy, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=toy, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-m", "x"], cwd=toy, check=True, capture_output=True, env=env)
    verdict = old_binary_rig.run_exact_old_binary(
        ref="HEAD",
        repo=toy,
        state_home=tmp_path / "home",
        workdir=tmp_path / "work",
        probe="toypkg.probe:main",
        v2_surfaces=[old_binary_rig.V2Surface(kind="fs", locator="v2-state.json")],
    )
    assert verdict.zero_visibility
