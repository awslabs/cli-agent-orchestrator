"""The run page's default step retries must match the engine's.

The run page shows a running step as "Attempt 2 of 4". When the step's spec sets
no ``retries``, the page takes the total from ``DEFAULT_STEP_RETRIES`` in
``web/src/components/workflow/runPage.ts``, a copy of the engine's
``WORKFLOW_DEFAULT_STEP_RETRIES``. Two constants in two languages that must move
together drift silently, so this test reads both and fails when they differ.
Precedent: ``test/agent_plugins/test_web_timeout_drift.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

from cli_agent_orchestrator.constants import WORKFLOW_DEFAULT_STEP_RETRIES

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_PAGE_TS = REPO_ROOT / "web" / "src" / "components" / "workflow" / "runPage.ts"


def _web_default_retries() -> int:
    """Parse ``DEFAULT_STEP_RETRIES`` out of runPage.ts."""
    text = RUN_PAGE_TS.read_text(encoding="utf-8")
    match = re.search(r"^export const DEFAULT_STEP_RETRIES = (\d+)$", text, re.MULTILINE)
    assert match, f"no DEFAULT_STEP_RETRIES found in {RUN_PAGE_TS}"
    return int(match.group(1))


class TestRunPageRetriesMatchTheEngine:
    def test_the_run_page_module_exists_where_this_test_expects_it(self):
        """A moved file must fail loudly rather than silently stop guarding."""
        assert RUN_PAGE_TS.is_file()

    def test_the_web_default_equals_the_engine_default(self):
        web = _web_default_retries()

        assert web == WORKFLOW_DEFAULT_STEP_RETRIES, (
            f"runPage.ts DEFAULT_STEP_RETRIES is {web} while the engine's "
            f"WORKFLOW_DEFAULT_STEP_RETRIES is {WORKFLOW_DEFAULT_STEP_RETRIES}: the run "
            f"page would show the wrong attempt total for a step with no retries."
        )
