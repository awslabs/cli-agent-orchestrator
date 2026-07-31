"""Workflow outcome capture for self-learning (Phase 1).

Outcomes are the raw learning signal: one record per unit of agent work
(a workflow step, a package conversion, a review round) with a success
flag, an optional 0-100 score, and short friction notes. The retrospector
agent reads them at session end and distills durable lessons into memory.

Design invariants (mirroring the memory subsystem):
- Records are short labels and notes, never transcripts or file contents.
- Writes are gated on ``is_learning_enabled()`` — learning is opt-in and a
  child of the memory subsystem. Disabled writes raise
  ``LearningDisabledError``; disabled reads return an empty list (silent
  empty reads are a safer no-op than raising).
- Notes are size-capped so a runaway agent cannot bloat the DB.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Caps keep outcome rows cheap and content-free. Notes hold 1-3 short
# sentences of friction description, not logs or diffs.
MAX_TASK_LABEL_CHARS = 200
MAX_NOTES_CHARS = 1000
MAX_LIST_LIMIT = 200

LEARNING_DISABLED_MESSAGE = (
    "workflow self-learning is disabled. Set memory.learning_enabled=true in "
    "settings.json (and keep memory.enabled=true) to enable outcome capture."
)


class LearningDisabledError(RuntimeError):
    """Raised when an outcome write is attempted while learning is disabled."""


def _is_learning_enabled() -> bool:
    """Module-level guard for outcome entry points.

    Imported lazily to avoid a settings → outcome_service circular import at
    module load time. Defaults to False if the import or read fails —
    learning is opt-in, so failures fail closed (unlike memory's default-on).
    """
    try:
        from cli_agent_orchestrator.services.settings_service import is_learning_enabled

        return is_learning_enabled()
    except Exception:
        return False


class OutcomeService:
    """Record and list workflow outcomes.

    Follows the ``MemoryService`` engine-injection pattern: tests pass an
    isolated engine; production uses the global ``SessionLocal``.
    """

    def __init__(self, db_engine: Any = None):
        self._db_session_factory: Any = None
        if db_engine is not None:
            from sqlalchemy.orm import sessionmaker

            self._db_session_factory = sessionmaker(
                autocommit=False, autoflush=False, bind=db_engine
            )

    def _get_db_session(self) -> Any:
        """Get a SQLAlchemy session — uses test engine if provided, else global."""
        if self._db_session_factory:
            return self._db_session_factory()
        from cli_agent_orchestrator.clients.database import SessionLocal

        return SessionLocal()

    def record_outcome(
        self,
        *,
        session_name: str,
        task_label: str,
        success: bool,
        workflow_name: Optional[str] = None,
        agent_profile: Optional[str] = None,
        source_terminal_id: Optional[str] = None,
        score: Optional[int] = None,
        friction_notes: str = "",
    ) -> Dict[str, Any]:
        """Persist one outcome record and return it as a dict.

        Raises:
            LearningDisabledError: when learning is disabled (checked before
                any validation, mirroring the memory store guard order).
            ValueError: on invalid inputs.
        """
        if not _is_learning_enabled():
            raise LearningDisabledError(LEARNING_DISABLED_MESSAGE)

        if not session_name or not session_name.strip():
            raise ValueError("session_name is required")
        task_label = (task_label or "").strip()
        if not task_label:
            raise ValueError("task_label is required")
        if len(task_label) > MAX_TASK_LABEL_CHARS:
            task_label = task_label[:MAX_TASK_LABEL_CHARS]
        if score is not None:
            if isinstance(score, bool) or not isinstance(score, int):
                raise ValueError(f"score must be an int 0-100, got {score!r}")
            if not (0 <= score <= 100):
                raise ValueError(f"score must be between 0 and 100, got {score}")
        friction_notes = (friction_notes or "").strip()
        if len(friction_notes) > MAX_NOTES_CHARS:
            friction_notes = friction_notes[:MAX_NOTES_CHARS]

        from cli_agent_orchestrator.clients.database import WorkflowOutcomeModel

        row = WorkflowOutcomeModel(
            id=str(uuid.uuid4()),
            session_name=session_name.strip(),
            workflow_name=(workflow_name or "").strip() or None,
            task_label=task_label,
            agent_profile=(agent_profile or "").strip() or None,
            source_terminal_id=source_terminal_id,
            success=bool(success),
            score=score,
            friction_notes=friction_notes,
            created_at=datetime.now(timezone.utc),
        )
        with self._get_db_session() as db:
            db.add(row)
            db.commit()
            result = self._to_dict(row)
        logger.info(
            "Recorded outcome for session=%s agent=%s success=%s",
            result["session_name"],
            result["agent_profile"],
            result["success"],
        )
        return result

    def list_outcomes(
        self,
        *,
        session_name: Optional[str] = None,
        agent_profile: Optional[str] = None,
        workflow_name: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """List outcomes newest-first, optionally filtered.

        Returns ``[]`` when learning is disabled — reads are a silent no-op
        so retrospection paths never crash a workflow.
        """
        if not _is_learning_enabled():
            return []

        limit = max(1, min(int(limit), MAX_LIST_LIMIT))

        from cli_agent_orchestrator.clients.database import WorkflowOutcomeModel

        with self._get_db_session() as db:
            query = db.query(WorkflowOutcomeModel)
            if session_name:
                query = query.filter(WorkflowOutcomeModel.session_name == session_name)
            if agent_profile:
                query = query.filter(WorkflowOutcomeModel.agent_profile == agent_profile)
            if workflow_name:
                query = query.filter(WorkflowOutcomeModel.workflow_name == workflow_name)
            rows = query.order_by(WorkflowOutcomeModel.created_at.desc()).limit(limit).all()
            return [self._to_dict(r) for r in rows]

    @staticmethod
    def _to_dict(row: Any) -> Dict[str, Any]:
        return {
            "id": row.id,
            "session_name": row.session_name,
            "workflow_name": row.workflow_name,
            "task_label": row.task_label,
            "agent_profile": row.agent_profile,
            "source_terminal_id": row.source_terminal_id,
            "success": row.success,
            "score": row.score,
            "friction_notes": row.friction_notes,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
