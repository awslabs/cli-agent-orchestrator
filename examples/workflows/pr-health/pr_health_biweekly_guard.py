#!/usr/bin/env python3
"""Gate a weekly CAO flow to an exact 14-day cadence."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

# Customize these two values before registering the flow.
REPOSITORY = "awslabs/cli-agent-orchestrator"
ANCHOR = date(2026, 1, 5)  # Monday; due dates repeat every 14 days.


def is_due(today: date) -> bool:
    days = (today - ANCHOR).days
    return days >= 0 and days % 14 == 0


def today_utc() -> date:
    """Today in UTC.

    The workflow's idle/threshold math compares ``as_of`` against GitHub's UTC
    timestamps, so deriving ``as_of`` from the server's local date would shift
    the 7/14/21-day threshold crossings by a day for runs near midnight.
    """
    return datetime.now(timezone.utc).date()


def main() -> None:
    today = today_utc()
    as_of = today.isoformat()
    due = is_due(today)
    # Identifiers are mode-qualified so the dry-run and apply flows can both be
    # registered: a shared snapshot_id would make the second flow to run on a
    # due date collide with the first flow's manifest.
    print(
        json.dumps(
            {
                "execute": due,
                "output": {
                    "as_of": as_of,
                    "repo": REPOSITORY,
                    "run_id_dry_run": f"pr-health-biweekly-dry-run-{as_of}",
                    "snapshot_id_dry_run": f"scheduled-dry-run-{as_of}",
                    "run_id_apply": f"pr-health-biweekly-apply-{as_of}",
                    "snapshot_id_apply": f"scheduled-apply-{as_of}",
                },
            },
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
