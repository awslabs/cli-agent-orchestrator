#!/usr/bin/env python3
"""Gate a weekly CAO flow to an exact 14-day cadence."""

from __future__ import annotations

import json
from datetime import date

# Customize these two values before registering the flow.
REPOSITORY = "awslabs/cli-agent-orchestrator"
ANCHOR = date(2026, 1, 5)  # Monday; due dates repeat every 14 days.


def is_due(today: date) -> bool:
    days = (today - ANCHOR).days
    return days >= 0 and days % 14 == 0


def main() -> None:
    today = date.today()
    as_of = today.isoformat()
    due = is_due(today)
    print(
        json.dumps(
            {
                "execute": due,
                "output": {
                    "as_of": as_of,
                    "repo": REPOSITORY,
                    "run_id": f"pr-health-biweekly-{as_of}",
                    "snapshot_id": f"scheduled-{as_of}",
                },
            },
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
