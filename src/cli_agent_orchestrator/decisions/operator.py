"""In-process operator access; intentionally has no HTTP routes."""

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from cli_agent_orchestrator.clients.database import init_db
from cli_agent_orchestrator.decisions.settings import load_settings
from cli_agent_orchestrator.decisions.store import DecisionStore


def status() -> dict[str, Any]:
    return asdict(load_settings())


def parse_date(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (
        parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(timezone.utc)
    )


def records(
    *, point: str | None = None, since: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    init_db()
    return DecisionStore().list(point=point, since=parse_date(since), limit=limit)


def purge(*, before: str | None = None, all_records: bool = False, rotate: bool = False) -> int:
    if (before is None) == (not all_records):
        raise ValueError("provide exactly one of before or all")
    cutoff = parse_date(before)
    init_db()
    return DecisionStore().purge(before=cutoff, rotate=rotate, all_records=all_records)
