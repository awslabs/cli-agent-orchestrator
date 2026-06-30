"""Daily Audit Log.

Append-only markdown log at ``<base>/logs/memory/YYYY-MM-DD.md``. One bullet
per event; each bullet starts with ``- <ISO8601Z> [event_type]`` and ends
with ``\\n``. The bullet anchor + trailing newline are the recovery
primitives for parsers that survive a torn write.

Hard contracts:

- Log-injection bytes (Unicode line separators included) are sanitised
  via the compiler's extended ``_sanitize_for_log``.
- ``_resolve_log_path`` is the single source of truth for path
  composition — used at write, sweep, and CLI ``--date`` parse.
- ``os.open`` includes ``O_NOFOLLOW``/``O_CLOEXEC`` (POSIX); a post-open
  ``fstat`` mode check rejects anything other than ``0o600``.
- Append-only ``os.write``; the ``- `` bullet anchor is the parser
  recovery primitive; flock is opt-in via setting (default OFF).
- Per-day soft cap defaults to 100 MiB (operator override via
  ``memory.audit_log_day_cap_bytes`` JSON-only setting).
- ``memory.enabled = False`` short-circuits at the very top —
  documented forensic gap.
- ``summary`` is wrapped in backticks; ``<>&`` are HTML-entity
  escaped on both summary and field values; inner backticks escaped.
- Events are classified sync (await) or nowait; the classification
  sets are the source of truth.

Non-blocking promise: the write boundary catches every OSError /
PermissionError and emits a Python-logger WARNING. Memory ops never
see an audit failure.
"""

import asyncio
import logging
import os
import platform
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from cli_agent_orchestrator.constants import MEMORY_BASE_DIR
from cli_agent_orchestrator.services.wiki_compiler import _sanitize_for_log

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

PER_FIELD_CAP_BYTES = 4 * 1024
PER_ENTRY_CAP_BYTES = 8 * 1024
SUMMARY_MAX_CHARS = 200  # mirrors the _sanitize_for_log default

LOG_FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Sync vs nowait classification. The combined set is the closed event
# whitelist — anything else is dropped at the write boundary.
SYNC_AUDIT_EVENTS: frozenset = frozenset(
    {
        "lint_run_completed",
        # Phase 4 U1 — wiki self-healing. Each applied mutation emits an
        # AWAITED audit event; ``heal_run_completed`` summarises the run.
        "orphan_pruned",
        "contradiction_resolved",
        "stale_claim_pruned",
        "poison_access_zeroed",
        "heal_run_completed",
        # Emitted when a batch commit fails AFTER an irreversible filesystem
        # mutation already happened — the DB rolls back but the on-disk change
        # persists, so the forensic trail must still record it.
        "heal_partial_mutation",
    }
)
NOWAIT_AUDIT_EVENTS: frozenset = frozenset(
    {
        "memory_stored",
        "memory_recalled",
        "memory_forgot",
        "audit_log_error",
        # Compile-pipeline outcomes. NOWAIT: all are emitted from the
        # deferred background compile task / repair sweep, never on the
        # agent-critical path, and the emit-site uses write_audit_nowait.
        "compile_completed",
        "compile_fallback",
        "compile_stale_dropped",
        "compile_error",
        "find_related_completed",
    }
)
AUDIT_EVENT_WHITELIST: frozenset = SYNC_AUDIT_EVENTS | NOWAIT_AUDIT_EVENTS

# Defaults that can be overridden via settings.json (deliberately no env support).
AUDIT_LOG_DAY_CAP_BYTES_DEFAULT = 100 * 1024 * 1024  # 100 MiB
AUDIT_DIR_MODE = 0o700
AUDIT_FILE_MODE = 0o600

# Drop reasons (closed vocabulary).
DROP_REASONS: frozenset = frozenset(
    {
        "unknown_event",
        "sanitise_failed",
        "path_invalid",
        "perm_violation",
        "day_cap_exceeded",
        "disk_full",
        "loop_unavailable",
    }
)


# -----------------------------------------------------------------------------
# Settings adapters — read-only; never raise out
# -----------------------------------------------------------------------------


def _audit_day_cap_bytes() -> int:
    try:
        from cli_agent_orchestrator.services.settings_service import get_memory_settings

        v = get_memory_settings().get("audit_log_day_cap_bytes", AUDIT_LOG_DAY_CAP_BYTES_DEFAULT)
        return int(v)
    except Exception:
        return AUDIT_LOG_DAY_CAP_BYTES_DEFAULT


def _audit_log_flock_enabled() -> bool:
    try:
        from cli_agent_orchestrator.services.settings_service import get_memory_settings

        return bool(get_memory_settings().get("audit_log_flock", False))
    except Exception:
        return False


def _is_memory_enabled_safe() -> bool:
    try:
        from cli_agent_orchestrator.services.settings_service import is_memory_enabled

        return bool(is_memory_enabled())
    except Exception:
        # Conservative — if the setting is unreadable, fall back to True so
        # we don't silently lose forensic events on misconfiguration.
        return True


# -----------------------------------------------------------------------------
# Sanitisation wrappers
# -----------------------------------------------------------------------------


def _sanitize_field_value(v: object) -> str:
    s = _sanitize_for_log(str(v), max_len=PER_FIELD_CAP_BYTES)
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = s.replace("=", "\\=")
    enc = s.encode("utf-8")
    if len(enc) > PER_FIELD_CAP_BYTES:
        s = enc[:PER_FIELD_CAP_BYTES].decode("utf-8", errors="ignore") + "[…truncated]"
    return s


def _sanitize_summary(s: str) -> str:
    out = _sanitize_for_log(s, max_len=SUMMARY_MAX_CHARS)
    out = out.replace("`", "\\`")
    out = out.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    out = out.replace('"', '\\"')
    if len(out) > SUMMARY_MAX_CHARS:
        out = out[: SUMMARY_MAX_CHARS - 1] + "…"
    return out


# -----------------------------------------------------------------------------
# Path / open primitives
# -----------------------------------------------------------------------------


def _resolve_log_path(base_dir: Path, date_str: str) -> Optional[Path]:
    """Single source of truth for log path composition."""
    if not LOG_FILENAME_RE.match(date_str or ""):
        return None
    try:
        target = (base_dir / "logs" / "memory" / f"{date_str}.md").resolve()
        base_resolved = str(base_dir.resolve())
    except OSError:
        return None
    if not (str(target) == base_resolved or str(target).startswith(base_resolved + os.sep)):
        return None
    return target


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _ensure_log_dir(base_dir: Path) -> bool:
    try:
        log_dir = base_dir / "logs" / "memory"
        log_dir.mkdir(parents=True, exist_ok=True)
        if platform.system() != "Windows":
            try:
                os.chmod(log_dir, AUDIT_DIR_MODE)
            except OSError as e:
                logger.debug(f"audit_log: chmod on log dir failed (non-fatal): {e}")
        return True
    except OSError as e:
        logger.warning("audit_log_open_failed errno_name=%s", type(e).__name__)
        return False


def _open_audit_fd(target: Path) -> int:
    """O_NOFOLLOW + O_CLOEXEC + fstat mode check.

    Returns a writable fd on success, ``-1`` on any guard violation.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(str(target), flags, AUDIT_FILE_MODE)
    except OSError as e:
        logger.warning("audit_log_open_failed errno_name=%s", type(e).__name__)
        return -1
    try:
        st = os.fstat(fd)
    except OSError as e:
        logger.warning("audit_log_open_failed errno_name=%s", type(e).__name__)
        try:
            os.close(fd)
        except OSError:
            pass
        return -1
    if platform.system() != "Windows" and (st.st_mode & 0o7777) != AUDIT_FILE_MODE:
        logger.warning("audit_log_perm_violation event=open mode=%o", st.st_mode & 0o7777)
        try:
            os.close(fd)
        except OSError:
            pass
        return -1
    return fd


def _under_day_cap(fd: int, cap: int) -> bool:
    try:
        return os.fstat(fd).st_size < cap
    except OSError:
        return False


# -----------------------------------------------------------------------------
# Line render
# -----------------------------------------------------------------------------


def _render_line(
    *,
    timestamp: str,
    event_type: str,
    fields: dict,
    summary: str,
) -> bytes:
    """Render a single bytes object: ``- `` first, ``\\n`` last."""
    parts = [f"{k}={fields[k]}" for k in sorted(fields.keys())]
    body = f"- {timestamp} [{event_type}]"
    if parts:
        body += " " + " ".join(parts)
    body += f" — `{summary}`\n"
    return body.encode("utf-8")


# -----------------------------------------------------------------------------
# Core write
# -----------------------------------------------------------------------------


def _drop(reason: str, event_type: str = "unknown") -> None:
    if reason in DROP_REASONS:
        logger.warning("audit_log_drop event_type=%s reason=%s", event_type, reason)
    else:
        logger.warning("audit_log_drop event_type=%s reason=unknown", event_type)


async def write_audit(event_type: str, summary: str, **fields: Any) -> None:
    """Append a single audit entry. Never raises out to callers."""
    try:
        if not _is_memory_enabled_safe():
            return  # kill switch fully off
        if event_type not in AUDIT_EVENT_WHITELIST:
            _drop("unknown_event", event_type)
            # Best-effort self-emit (audit_log_error is in NOWAIT set —
            # may itself be dropped if loop unavailable).
            try:
                if event_type != "audit_log_error":
                    await write_audit(
                        "audit_log_error",
                        f"unknown event_type rejected: {event_type[:32]}",
                        reason="unknown_event",
                    )
            except Exception:
                pass
            return

        try:
            sanitised_summary = _sanitize_summary(summary or "")
            sanitised_fields = {k: _sanitize_field_value(v) for k, v in fields.items()}
        except Exception as e:
            _drop("sanitise_failed", event_type)
            logger.debug(f"audit_log sanitise raised: {e}")
            return

        date_str = _today_utc()
        target = _resolve_log_path(MEMORY_BASE_DIR, date_str)
        if target is None:
            _drop("path_invalid", event_type)
            return
        if not _ensure_log_dir(MEMORY_BASE_DIR):
            _drop("path_invalid", event_type)
            return

        fd = _open_audit_fd(target)
        if fd < 0:
            _drop("perm_violation", event_type)
            return

        try:
            cap = _audit_day_cap_bytes()
            if not _under_day_cap(fd, cap):
                _drop("day_cap_exceeded", event_type)
                return

            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            line = _render_line(
                timestamp=ts,
                event_type=event_type,
                fields=sanitised_fields,
                summary=sanitised_summary,
            )
            if len(line) > PER_ENTRY_CAP_BYTES:
                # Truncate at byte boundary; preserve newline.
                line = line[: PER_ENTRY_CAP_BYTES - 1] + b"\n"

            if _audit_log_flock_enabled() and platform.system() != "Windows":
                try:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX)
                    try:
                        os.write(fd, line)
                    finally:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_UN)
                        except OSError:
                            pass
                except OSError as e:
                    logger.warning("audit_log_drop event_type=%s reason=disk_full", event_type)
                    logger.debug(f"audit_log flock/write error: {e}")
            else:
                try:
                    os.write(fd, line)
                except OSError as e:
                    _drop("disk_full", event_type)
                    logger.debug(f"audit_log os.write error: {e}")
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
    except Exception as e:  # noqa: BLE001 — non-blocking promise
        logger.warning("audit_log unexpected error: %s", type(e).__name__)


def write_audit_nowait(event_type: str, summary: str, **fields: Any) -> None:
    """Schedule an audit write without awaiting it.

    When a running loop is reachable (the server's dominant case) the write is
    scheduled as a background task and the caller returns immediately. When no
    loop is reachable (sync CLI/test paths), it falls back to ``asyncio.run``
    in the caller's thread — a short, blocking append, treated as best-effort
    (any error is dropped). This fallback is NOT offloaded to a worker thread.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        try:
            loop.create_task(write_audit(event_type, summary, **fields))
            return
        except Exception as e:
            logger.warning("audit_log_drop event_type=%s reason=loop_unavailable", event_type)
            logger.debug(f"create_task failed: {e}")
            return
    # No running loop — best-effort sync drain via a fresh loop. We MUST
    # NOT block the caller for long; treat any error as drop.
    try:
        asyncio.run(write_audit(event_type, summary, **fields))
    except Exception as e:
        logger.warning("audit_log_drop event_type=%s reason=loop_unavailable", event_type)
        logger.debug(f"asyncio.run fallback failed: {e}")


# -----------------------------------------------------------------------------
# Retention sweep
# -----------------------------------------------------------------------------


def sweep_old_audit_logs(*, retention_days: int = 30) -> int:
    """Delete log files older than ``retention_days``. Returns count deleted."""
    from datetime import timedelta

    deleted = 0
    log_dir = MEMORY_BASE_DIR / "logs" / "memory"
    if not log_dir.exists():
        return 0
    try:
        base_resolved = str(MEMORY_BASE_DIR.resolve())
    except OSError:
        return 0
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=retention_days)
    for path in log_dir.glob("*.md"):
        stem = path.stem
        if not LOG_FILENAME_RE.match(stem):
            continue  # not one of ours
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not (str(resolved) == base_resolved or str(resolved).startswith(base_resolved + os.sep)):
            # Symlink escapes base — leave alone.
            continue
        try:
            file_date = datetime.strptime(stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_date >= cutoff:
            continue
        try:
            path.unlink(missing_ok=True)
            deleted += 1
        except OSError as e:
            logger.warning("audit_log_sweep_failed errno_name=%s", type(e).__name__)
    return deleted


# -----------------------------------------------------------------------------
# Read helper for CLI ``cao memory logs``
# -----------------------------------------------------------------------------


def read_audit_log(date_str: Optional[str] = None) -> str:
    """Return the bytes (decoded) of the requested day's audit log.

    READ-ONLY. Validates ``date_str`` against ``LOG_FILENAME_RE`` and
    opens the resolved path with ``O_RDONLY``. Returns ``""`` if
    the date doesn't resolve, the file doesn't exist, or any read error.
    """
    if date_str is None:
        date_str = _today_utc()
    target = _resolve_log_path(MEMORY_BASE_DIR, date_str)
    if target is None or not target.exists():
        return ""
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(str(target), flags)
    except OSError:
        return ""
    try:
        chunks: list = []
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
