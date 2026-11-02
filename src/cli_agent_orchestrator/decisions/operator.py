"""In-process operator access; intentionally has no HTTP routes."""

import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from cli_agent_orchestrator.clients.database import init_db
from cli_agent_orchestrator.decisions import egress, settings
from cli_agent_orchestrator.decisions.packages import installed_deciders
from cli_agent_orchestrator.decisions.settings import load_settings
from cli_agent_orchestrator.decisions.store import DecisionStore, read_decider_status
from cli_agent_orchestrator.decisions.types import POINTS
from cli_agent_orchestrator.services import settings_service

logger = logging.getLogger(__name__)
OFF_REENABLED_MESSAGE = (
    "A concurrent change re-enabled decisions during off; re-run cao decisions off."
)


def _effective() -> dict[str, Any]:
    result = asdict(load_settings())
    result["halted"] = egress.is_halted()
    result["installed_deciders"] = installed_deciders()
    return result


def status() -> dict[str, Any]:
    result = _effective()
    result["decider_status"] = read_decider_status()
    for decider in result["installed_deciders"]:
        result["decider_status"]["deciders"].setdefault(
            decider["name"], {"last": None, "counts": {}}
        )
    return result


def set_point(point: str, state: str, decider: str | None = None) -> dict[str, Any]:
    settings.set_point(point, state, decider)
    return _effective()


def _halt_marker_identity() -> tuple[int, int] | None:
    try:
        stat = egress.HALT_FILE.lstat()
    except OSError:
        return None
    return stat.st_dev, stat.st_ino


def off() -> dict[str, Any]:
    """Publish the halt before saving states and waiting for outstanding egress."""
    egress.HALT_FILE.parent.mkdir(parents=True, exist_ok=True)
    egress.HALT_FILE.touch(mode=0o600)
    marker_identity = _halt_marker_identity()
    snapshot = load_settings()
    saved = False
    try:
        data = settings_service._load_or_raise()
        for point in POINTS:
            settings._object(data, "decisions", "points", point)["state"] = "off"
        settings_service._save(data)
        saved = True
    except (OSError, ValueError, settings_service.SettingsUnreadableError):
        logger.warning("Could not persist decision states; halt remains active")
    drained = egress.drain(snapshot.shadow_timeout_ms / 1000 + 5)
    points = asdict(load_settings())["points"]
    halted = egress.is_halted()
    same_halt = marker_identity is not None and _halt_marker_identity() == marker_identity
    all_off = all(points[point]["state"] == "off" for point in POINTS)
    message = None
    if not halted or not same_halt or not all_off:
        message = OFF_REENABLED_MESSAGE
    elif not drained:
        message = "External decision requests did not drain before the timeout"
    return {
        "halted": halted,
        "drained": drained,
        "settings_saved": saved,
        "points": points,
        "success": drained and halted and same_halt and all_off,
        "message": message,
    }


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
