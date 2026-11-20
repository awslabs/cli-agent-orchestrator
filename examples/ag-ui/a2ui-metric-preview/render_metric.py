"""Render one allow-listed ``emit_ui`` component (a ``metric``) as an A2UI surface.

Runs offline, with no server and no flag: it calls the same functions that back
``POST /agui/v1/preview/a2ui`` and prints the ``ACTIVITY_SNAPSHOT`` frame and the text the
reference renderer draws. An off-list component is refused.

    uv run python examples/ag-ui/a2ui-metric-preview/render_metric.py
"""

from __future__ import annotations

import json

from cli_agent_orchestrator.services.agui.post3_preview import (
    A2uiRefusal,
    activity_snapshot,
    render_text,
    to_a2ui_operations,
)


def main() -> None:
    operations = to_a2ui_operations("metric", {"label": "Tokens", "value": 1200, "unit": "tok"})
    print(json.dumps(activity_snapshot(operations, "a2ui-example"), indent=2))
    print("--- rendered ---")
    print(render_text(operations))
    try:
        to_a2ui_operations("iframe", {"src": "https://example.invalid"})
    except A2uiRefusal as exc:
        print(f"--- off-list refused: {exc}")


if __name__ == "__main__":
    main()
