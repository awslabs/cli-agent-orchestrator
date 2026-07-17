"""Conditional shape — shim-based (M1, FR-7.1).

Branches on a simple author-controlled condition and runs a DIFFERENT step
depending on which branch is taken — proving only the taken branch's
``run_step`` call executes, never both.

The branch condition is a plain in-script constant (NOT read from the
environment or an external input) — U4's constructed spawn env carries only
the fixed identity allowlist (``CAO_WORKFLOW_RUN_ID``/``GENERATION``/
``CAO_API_BASE_URL``/``PATH``/``HOME``), so a script's own control flow must
be deterministic from its own source, not from ambient environment state
(the authoring guide's determinism obligation). Edit ``IS_URGENT`` below to
exercise the other branch.

Run standalone via ``cao workflow run --script docs/examples/conditional_example.py``.
"""

from __future__ import annotations

from cao_workflow import emit_output, run_step

IS_URGENT = True


def main() -> None:
    if IS_URGENT:
        handle = run_step("kiro_cli", "reviewer", "escalate this incident", step_id="urgent-branch")
        branch_taken = "urgent"
    else:
        handle = run_step("kiro_cli", "reviewer", "file this for later", step_id="routine-branch")
        branch_taken = "routine"

    emit_output({"branch_taken": branch_taken, "output": handle.output})


if __name__ == "__main__":
    main()
