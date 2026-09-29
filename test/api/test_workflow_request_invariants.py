"""Defensive workflow request invariants remain active under ``python -O``."""

from __future__ import annotations

import subprocess
import sys
import textwrap


def test_validate_spec_path_narrowing_is_a_400_under_optimization():
    """A changing request object must not reach ``splitext(None)`` when asserts vanish."""
    program = textwrap.dedent("""
        import asyncio
        from fastapi import HTTPException
        from cli_agent_orchestrator.api.main import validate_workflow_endpoint

        class ChangingBody:
            source = None

            def __init__(self):
                self.reads = 0

            @property
            def path(self):
                self.reads += 1
                return "workflow.py" if self.reads == 1 else None

        try:
            asyncio.run(validate_workflow_endpoint(ChangingBody(), []))
        except HTTPException as exc:
            print(exc.status_code)
            print(exc.detail)
        """)

    result = subprocess.run(
        [sys.executable, "-O", "-c", program],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "400",
        "supply exactly one of 'path' or 'source'",
    ]
