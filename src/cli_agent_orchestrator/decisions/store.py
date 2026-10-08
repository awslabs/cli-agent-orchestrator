"""Failure-isolated record storage with independent decision and launch updates."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import or_
from sqlalchemy.orm import Session, sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.constants import DB_DIR
from cli_agent_orchestrator.decisions.hashing import (
    InsecureKeyError,
    InvalidKeyError,
    KeyCache,
    cleanup_temps,
    rotate_key,
)

logger = logging.getLogger(__name__)


class DecisionStore:
    def __init__(
        self,
        sessions: sessionmaker[Session] | None = None,
        key_path: Path | None = None,
        *,
        emit: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.sessions = sessions if sessions is not None else database.SessionLocal
        self.key_path = key_path if key_path is not None else DB_DIR / "decision-hash.key"
        self._shadow_rows: set[int] = set()
        self._key = KeyCache(self.key_path)
        if emit is None:
            from cli_agent_orchestrator.decisions.telemetry import emit_record

            emit = emit_record
        self.emit = emit

    def insert(self, values: dict[str, Any], message: str | None = None) -> int | None:
        try:
            data: dict[Any, Any] = dict(values)
            if data.get("outcome") == "rejected":
                data.update(launch_status="not_launched", decision_status="done", terminal_id=None)
            if message is not None:
                data["message_hash"], data["hash_key_id"] = self._key.digest(message)
                data["message_bytes"] = len(message.encode("utf-8"))
            if isinstance(data.get("probabilities"), dict):
                data["probabilities"] = json.dumps(data["probabilities"])
            with self.sessions() as db:
                row = database.DecisionRecordModel(**data)
                db.add(row)
                db.commit()
                record_id = int(row.id)
                if row.outcome == "rejected":
                    self._emit(self._dict(row))
                return record_id
        except InvalidKeyError:
            logger.warning(
                "Decision hash key file %s is invalid; records are not written", self.key_path.name
            )
            return None
        except InsecureKeyError:
            logger.warning(
                "Decision hash key file %s cannot be restricted to 0600; records are not written",
                self.key_path.name,
            )
            return None
        except Exception:
            logger.warning("Decision record insert failed")
            return None

    @staticmethod
    def _dict(row: Any) -> dict[str, Any]:
        data = {
            column.name: getattr(row, column.name)
            for column in database.DecisionRecordModel.__table__.columns
        }
        if data["probabilities"] is not None:
            data["probabilities"] = json.loads(data["probabilities"])
        for key in ("created_at", "updated_at"):
            if isinstance(data[key], datetime):
                data[key] = data[key].isoformat()
        return data

    def _emit(self, row: dict[str, Any]) -> None:
        try:
            self.emit(row)
        except Exception:
            logger.warning("Decision telemetry export failed")

    def shadow_started(self, record_id: int) -> None:
        self._shadow_rows.add(record_id)

    def update_decision(self, record_id: int, values: dict[str, Any], *, emit: bool = True) -> None:
        try:
            data: dict[Any, Any] = dict(values)
            if isinstance(data.get("probabilities"), dict):
                data["probabilities"] = json.dumps(data["probabilities"])
            data["updated_at"] = datetime.now(timezone.utc)
            with self.sessions() as db:
                changed = (
                    db.query(database.DecisionRecordModel)
                    .filter_by(id=record_id, decision_status="pending")
                    .update(data)
                )
                db.commit()
                row = db.get(database.DecisionRecordModel, record_id)
                if changed and emit and row is not None:
                    self._emit(self._dict(row))
                    if row.launch_status != "pending":
                        self._shadow_rows.discard(record_id)
        except Exception:
            logger.warning("Decision record update failed")

    def bind(
        self,
        record_ids: tuple[int, ...],
        *,
        launch_status: str,
        terminal_id: str | None = None,
        launched_model: str | None = None,
        model_honored: bool | None = None,
        not_honored: bool = False,
    ) -> None:
        for record_id in record_ids:
            try:
                with self.sessions() as db:
                    query = db.query(database.DecisionRecordModel).filter_by(
                        id=record_id, launch_status="pending"
                    )
                    values: dict[Any, Any] = dict(
                        launch_status=launch_status,
                        terminal_id=terminal_id,
                        launched_model=launched_model,
                        model_honored=model_honored,
                        updated_at=datetime.now(timezone.utc),
                    )
                    if not_honored:
                        values.update(outcome="fallback", reason="not_honored", applied_value=None)
                    changed = query.update(values)
                    if not changed and launch_status == "launch_failed":
                        db.query(database.DecisionRecordModel).filter_by(
                            id=record_id, launch_status="launched"
                        ).update(
                            {
                                "launch_status": "launch_failed",
                                "updated_at": datetime.now(timezone.utc),
                            }
                        )
                    db.commit()
                    row = db.get(database.DecisionRecordModel, record_id)
                    if changed and row is not None:
                        if record_id not in self._shadow_rows:
                            self._emit(self._dict(row))
                        elif row.decision_status != "pending":
                            self._shadow_rows.discard(record_id)
            except Exception:
                logger.warning("Decision launch bind failed")

    def get(self, record_id: int) -> dict[str, Any] | None:
        with self.sessions() as db:
            row = db.get(database.DecisionRecordModel, record_id)
            return self._dict(row) if row is not None else None

    def list(
        self, *, point: str | None = None, since: datetime | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self.sessions() as db:
            query = db.query(database.DecisionRecordModel)
            if point is not None:
                query = query.filter_by(point=point)
            if since is not None:
                query = query.filter(database.DecisionRecordModel.created_at >= since)
            return [
                self._dict(row)
                for row in query.order_by(database.DecisionRecordModel.id.desc())
                .limit(max(1, min(limit, 10000)))
                .all()
            ]

    def sweep(self) -> None:
        try:
            cleanup_temps(self.key_path)
            with self.sessions() as db:
                pending = or_(
                    database.DecisionRecordModel.decision_status == "pending",
                    database.DecisionRecordModel.launch_status == "pending",
                )
                ids = [
                    int(row.id)
                    for row in db.query(database.DecisionRecordModel.id)
                    .filter(pending, database.DecisionRecordModel.outcome != "rejected")
                    .all()
                ]
                for record_id in ids:
                    row = db.get(database.DecisionRecordModel, record_id)
                    if row is None:
                        continue
                    accepted_shadow = row.state == "shadow" and (
                        row.decision_status in ("pending", "interrupted")
                        or row.latency_ms is not None
                    )
                    decision_changed = (
                        db.query(database.DecisionRecordModel)
                        .filter_by(id=record_id, decision_status="pending")
                        .update({"decision_status": "interrupted"})
                    )
                    launch_changed = (
                        db.query(database.DecisionRecordModel)
                        .filter_by(id=record_id, launch_status="pending")
                        .update({"launch_status": "unknown"})
                    )
                    db.commit()
                    row = db.get(database.DecisionRecordModel, record_id)
                    emission_changed = decision_changed if accepted_shadow else launch_changed
                    if emission_changed and row is not None:
                        self._emit(self._dict(row))
                    self._shadow_rows.discard(record_id)
        except Exception:
            logger.warning("Decision startup sweep failed")

    @staticmethod
    def _purge(db: Session, before: datetime | None) -> int:
        query = db.query(database.DecisionRecordModel)
        if before is not None:
            query = query.filter(database.DecisionRecordModel.created_at < before)
        return int(query.delete())

    def purge(
        self,
        *,
        before: datetime | None = None,
        rotate: bool = False,
        session: Session | None = None,
        all_records: bool = False,
    ) -> int:
        if (before is None) == (not all_records):
            raise ValueError("provide exactly one of before or all_records")
        if session is not None:
            if rotate:
                raise ValueError("key rotation requires a store-owned transaction")
            return self._purge(session, before)
        with self.sessions() as db:
            count = self._purge(db, before)
            db.commit()
        if rotate:
            rotate_key(self.key_path)
        return count
