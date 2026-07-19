"""Filesystem-first repair of memory metadata projections.

Markdown topics are the durable authority. This module performs a bounded
canonical-file scan and repairs only SQLite metadata and matching index lines;
it never invokes wiki lint, linking, compilation, models, or network clients.
"""

from __future__ import annotations

import fcntl
import os
import re
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional

from cli_agent_orchestrator.constants import MEMORY_BASE_DIR
from cli_agent_orchestrator.models.memory import MemoryType
from cli_agent_orchestrator.services.memory_format import (
    TOPIC_HEADER_RE,
    normalize_memory_tags,
    parse_index_entry,
)

_KEY_RE = re.compile(r"^[a-z0-9-]{1,60}$")
_SCOPE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_TIMESTAMP_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)$", re.MULTILINE)
_INDEX_ID_RE = re.compile(r"^- \[(?P<key>[^\]]+)\]\((?P<path>[^)]+)\)")

DEFAULTED_METADATA_FIELDS = (
    "source_provider",
    "source_terminal_id",
    "last_accessed_at",
    "last_compiled_at",
    "related_keys",
    "access_count",
)


class RepairAction(str, Enum):
    """Closed vocabulary for reconciliation plan actions."""

    CREATE_METADATA = "create_metadata"
    UPDATE_METADATA = "update_metadata"
    REBUILD_INDEX = "rebuild_index"
    UNCHANGED = "unchanged"
    MALFORMED = "malformed"
    CONFLICT = "conflict"
    UNSAFE_PATH = "unsafe_path"
    FAILED = "failed"


@dataclass(frozen=True, order=True)
class MemoryIdentity:
    """Canonical memory identity derived exclusively from a topic path."""

    key: str
    scope: str
    scope_id: Optional[str] = None


@dataclass(frozen=True)
class RepairFinding:
    """Actionable non-content finding emitted by discovery or apply."""

    kind: str
    message: str


@dataclass(frozen=True)
class RepairRecord:
    """Deterministic per-topic reconciliation plan or result."""

    identity: Optional[MemoryIdentity]
    file_path: str
    actions: tuple[RepairAction, ...]
    status: str
    finding: Optional[RepairFinding] = None
    defaulted_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepairReport:
    """Complete deterministic report for one dry-run or apply pass."""

    records: tuple[RepairRecord, ...]
    applied: bool

    @property
    def counts(self) -> dict[str, int]:
        """Return stable action and outcome counts."""
        counts = {
            "total": len(self.records),
            "repaired": 0,
            "unchanged": 0,
            "skipped": 0,
            "failed": 0,
            "create_metadata": 0,
            "update_metadata": 0,
            "rebuild_index": 0,
            "malformed": 0,
            "conflict": 0,
            "unsafe_path": 0,
        }
        for record in self.records:
            if record.status in counts:
                counts[record.status] += 1
            for action in record.actions:
                if action.value in counts and action.value not in {"unchanged", "failed"}:
                    counts[action.value] += 1
        return counts

    @property
    def has_unresolved(self) -> bool:
        """Whether unsafe, malformed, conflicting, or failed records remain."""
        return any(record.status in {"skipped", "failed"} for record in self.records)

    def summary_text(self) -> str:
        """Render a bounded content-free summary for logs and CLI output."""
        counts = self.counts
        mode = "apply" if self.applied else "dry-run"
        return (
            f"memory_repair mode={mode} total={counts['total']} "
            f"repaired={counts['repaired']} unchanged={counts['unchanged']} "
            f"skipped={counts['skipped']} failed={counts['failed']}"
        )


class MemoryReconciliationError(RuntimeError):
    """Unexpected per-record failures after all independent records were tried."""

    def __init__(self, report: RepairReport):
        self.report = report
        super().__init__(
            f"{report.summary_text()}; memory metadata repair had "
            f"{report.counts['failed']} unexpected failure(s). "
            "Run `cao memory repair --apply`."
        )


@dataclass(frozen=True)
class _Topic:
    identity: MemoryIdentity
    file_path: Path
    index_path: Path
    relative_path: str
    memory_id: str
    memory_type: str
    tags: str
    created_at: datetime
    updated_at: datetime
    updated_text: str
    token_estimate: int
    index_token_estimate: int


@dataclass(frozen=True)
class _Candidate:
    file_path: Path
    scope: str
    scope_id: Optional[str]
    index_path: Path
    relative_path: str


@dataclass(frozen=True)
class _Row:
    id: str
    identity: MemoryIdentity
    file_path: str
    memory_type: str
    tags: str
    created_at: Optional[datetime]
    updated_at: Optional[datetime]
    token_estimate: Optional[int]


@dataclass(frozen=True)
class _IndexState:
    """One linear parse of an index shared by all topics in its container."""

    lines: tuple[str, ...]
    entries: dict[MemoryIdentity, tuple[str, ...]]


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _under(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _first_symlink_component(path: Path, base: Path) -> Optional[Path]:
    """Return the first symlink below base in path's lexical component chain."""
    absolute_base = base.absolute()
    try:
        relative = path.absolute().relative_to(absolute_base)
    except ValueError:
        return None
    current = absolute_base
    for component in relative.parts:
        current /= component
        if current.is_symlink():
            return current
    return None


def discover_canonical_scope_dirs(base_dir: Path) -> tuple[tuple[str, Optional[str], Path], ...]:
    """Discover canonical scope containers without SQLite or index seeds."""
    discovered: set[tuple[str, Optional[str], Path]] = set()
    global_container = base_dir / "global"
    global_wiki = global_container / "wiki"
    if (global_wiki / "global").is_dir():
        discovered.add(("global", None, global_container))
    for scope in ("session", "agent"):
        scope_root = global_wiki / scope
        if scope_root.is_dir():
            try:
                children = scope_root.iterdir()
                scope_children = tuple(children)
            except OSError:
                continue
            for child in scope_children:
                if child.is_dir():
                    discovered.add((scope, child.name, global_container))
    if base_dir.is_dir():
        try:
            containers = tuple(base_dir.iterdir())
        except OSError:
            containers = ()
        for container in containers:
            if container.name in {"global", "federated"}:
                continue
            if (container / "wiki" / "project").is_dir():
                discovered.add(("project", container.name, container))
    return tuple(sorted(discovered, key=lambda item: (item[0], item[1] or "", str(item[2]))))


class MemoryReconciliationService:
    """Plan and apply non-destructive repairs from canonical Markdown topics."""

    def __init__(self, base_dir: Optional[Path] = None, db_engine: Any = None):
        self.base_dir = Path(base_dir or MEMORY_BASE_DIR)
        self._db_engine = db_engine
        self._db_session_factory: Any = None
        if db_engine is not None:
            from sqlalchemy.orm import sessionmaker

            self._db_session_factory = sessionmaker(
                autocommit=False, autoflush=False, bind=db_engine
            )

    def _get_db_session(self) -> Any:
        if self._db_session_factory is not None:
            return self._db_session_factory()
        from cli_agent_orchestrator.clients.database import SessionLocal

        return SessionLocal()

    def _candidate_record(
        self,
        path: Path,
        action: RepairAction,
        kind: str,
        message: str,
        identity: Optional[MemoryIdentity] = None,
    ) -> RepairRecord:
        return RepairRecord(
            identity=identity,
            file_path=str(path),
            actions=(action,),
            status="skipped",
            finding=RepairFinding(kind=kind, message=message),
        )

    def _iter_candidates(self) -> tuple[list[_Candidate], list[RepairRecord]]:
        candidates: list[_Candidate] = []
        findings: list[RepairRecord] = []
        if not self.base_dir.exists():
            return candidates, findings

        try:
            base_resolved = self.base_dir.resolve()
        except OSError:
            findings.append(
                self._candidate_record(
                    self.base_dir,
                    RepairAction.UNSAFE_PATH,
                    "directory_enumeration_failed",
                    "memory base directory could not be resolved",
                )
            )
            return candidates, findings

        def add_scope(
            root: Path,
            scope: str,
            scope_id: Optional[str],
            index_path: Path,
            relative_prefix: str,
        ) -> None:
            if not root.exists():
                return
            if _first_symlink_component(root, self.base_dir) is not None:
                findings.append(
                    self._candidate_record(
                        root,
                        RepairAction.UNSAFE_PATH,
                        "symlink_component",
                        "canonical scope path contains a symlink component",
                    )
                )
                return
            try:
                root_resolved = root.resolve()
            except OSError:
                findings.append(
                    self._candidate_record(
                        root,
                        RepairAction.UNSAFE_PATH,
                        "unsafe_path",
                        "canonical scope directory could not be resolved",
                    )
                )
                return
            if not _under(root_resolved, base_resolved):
                findings.append(
                    self._candidate_record(
                        root,
                        RepairAction.UNSAFE_PATH,
                        "symlink_escape",
                        "canonical scope directory resolves outside the memory base",
                    )
                )
                return
            if not root.is_dir():
                return
            try:
                paths = sorted(root.iterdir(), key=lambda item: item.name)
            except OSError:
                findings.append(
                    self._candidate_record(
                        root,
                        RepairAction.UNSAFE_PATH,
                        "directory_enumeration_failed",
                        "canonical scope directory could not be enumerated",
                    )
                )
                return
            for path in paths:
                if path.name == "index.md" or path.suffix != ".md":
                    continue
                candidates.append(
                    _Candidate(
                        file_path=path,
                        scope=scope,
                        scope_id=scope_id,
                        index_path=index_path,
                        relative_path=f"{relative_prefix}/{path.name}",
                    )
                )

        global_container = self.base_dir / "global"
        global_wiki = global_container / "wiki"
        global_index = global_wiki / "index.md"
        add_scope(global_wiki / "global", "global", None, global_index, "global")

        for scope in ("session", "agent"):
            scope_root = global_wiki / scope
            if not scope_root.exists():
                continue
            if _first_symlink_component(scope_root, self.base_dir) is not None:
                findings.append(
                    self._candidate_record(
                        scope_root,
                        RepairAction.UNSAFE_PATH,
                        "symlink_component",
                        "canonical scope path contains a symlink component",
                    )
                )
                continue
            try:
                scope_root_resolved = scope_root.resolve()
            except OSError:
                findings.append(
                    self._candidate_record(
                        scope_root,
                        RepairAction.UNSAFE_PATH,
                        "directory_enumeration_failed",
                        "canonical scope root could not be resolved",
                    )
                )
                continue
            if not _under(scope_root_resolved, base_resolved):
                findings.append(
                    self._candidate_record(
                        scope_root,
                        RepairAction.UNSAFE_PATH,
                        "symlink_escape",
                        "canonical scope directory resolves outside the memory base",
                    )
                )
                continue
            try:
                identity_dirs = sorted(scope_root.iterdir(), key=lambda item: item.name)
            except OSError:
                findings.append(
                    self._candidate_record(
                        scope_root,
                        RepairAction.UNSAFE_PATH,
                        "directory_enumeration_failed",
                        "canonical scope root could not be enumerated",
                    )
                )
                continue
            for identity_dir in identity_dirs:
                add_scope(
                    identity_dir,
                    scope,
                    identity_dir.name,
                    global_index,
                    f"{scope}/{identity_dir.name}",
                )

        try:
            containers = sorted(self.base_dir.iterdir(), key=lambda item: item.name)
        except OSError:
            findings.append(
                self._candidate_record(
                    self.base_dir,
                    RepairAction.UNSAFE_PATH,
                    "directory_enumeration_failed",
                    "memory base directory could not be enumerated",
                )
            )
            containers = []
        for container in containers:
            if container.name in {"global", "federated"}:
                continue
            project_root = container / "wiki" / "project"
            if project_root.exists():
                add_scope(
                    project_root,
                    "project",
                    container.name,
                    container / "wiki" / "index.md",
                    "project",
                )

        return candidates, findings

    def _parse_candidate(self, candidate: _Candidate) -> _Topic | RepairRecord:
        path = candidate.file_path
        identity = MemoryIdentity(path.stem, candidate.scope, candidate.scope_id)
        if _first_symlink_component(path, self.base_dir) is not None:
            return self._candidate_record(
                path,
                RepairAction.UNSAFE_PATH,
                "symlink_component",
                "canonical topic path contains a symlink component",
                identity,
            )
        try:
            base_resolved = self.base_dir.resolve()
            resolved = path.resolve()
        except OSError:
            return self._candidate_record(
                path,
                RepairAction.UNSAFE_PATH,
                "unsafe_path",
                "topic path could not be resolved",
                identity,
            )
        if not _under(resolved, base_resolved):
            return self._candidate_record(
                path,
                RepairAction.UNSAFE_PATH,
                "symlink_escape",
                "topic resolves outside the memory base",
                identity,
            )
        if not _KEY_RE.fullmatch(identity.key):
            return self._candidate_record(
                path,
                RepairAction.MALFORMED,
                "invalid_key",
                "topic filename is not a valid memory key",
                identity,
            )
        if identity.scope_id is not None and not _SCOPE_ID_RE.fullmatch(identity.scope_id):
            return self._candidate_record(
                path,
                RepairAction.MALFORMED,
                "invalid_scope_id",
                "canonical scope ID is invalid",
                identity,
            )
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return self._candidate_record(
                path,
                RepairAction.MALFORMED,
                "unreadable_topic",
                "topic is not readable UTF-8 Markdown",
                identity,
            )
        lines = text.splitlines()
        if len(lines) < 2 or lines[0] != f"# {identity.key}":
            return self._candidate_record(
                path,
                RepairAction.CONFLICT,
                "path_header_conflict",
                "topic heading does not match its canonical path identity",
                identity,
            )
        header = TOPIC_HEADER_RE.fullmatch(lines[1])
        if header is None:
            return self._candidate_record(
                path,
                RepairAction.MALFORMED,
                "malformed_header",
                "topic metadata header is malformed",
                identity,
            )
        try:
            uuid.UUID(header.group("id"))
        except ValueError:
            return self._candidate_record(
                path,
                RepairAction.MALFORMED,
                "malformed_header",
                "topic metadata ID is not a UUID",
                identity,
            )
        if header.group("scope").strip() != identity.scope:
            return self._candidate_record(
                path,
                RepairAction.CONFLICT,
                "path_header_conflict",
                "topic header scope conflicts with its canonical path",
                identity,
            )
        memory_type = header.group("type").strip()
        try:
            MemoryType(memory_type)
        except ValueError:
            return self._candidate_record(
                path,
                RepairAction.MALFORMED,
                "malformed_header",
                "topic metadata type is invalid",
                identity,
            )
        timestamp_texts = _TIMESTAMP_RE.findall(text)
        if not timestamp_texts:
            return self._candidate_record(
                path,
                RepairAction.MALFORMED,
                "malformed_timestamp",
                "topic contains no canonical entry timestamp",
                identity,
            )
        try:
            timestamps = [
                datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                for value in timestamp_texts
            ]
        except ValueError:
            return self._candidate_record(
                path,
                RepairAction.MALFORMED,
                "malformed_timestamp",
                "topic contains an invalid entry timestamp",
                identity,
            )
        return _Topic(
            identity=identity,
            file_path=resolved,
            index_path=candidate.index_path,
            relative_path=candidate.relative_path,
            memory_id=header.group("id"),
            memory_type=memory_type,
            tags=normalize_memory_tags(header.group("tags")),
            created_at=min(timestamps),
            updated_at=max(timestamps),
            updated_text=max(timestamp_texts),
            token_estimate=len(text) // 4,
            index_token_estimate=int(len(text.split()) * 1.3),
        )

    def _load_rows(self) -> list[_Row]:
        from cli_agent_orchestrator.clients.database import MemoryMetadataModel

        with self._get_db_session() as db:
            rows = db.query(MemoryMetadataModel).all()
            return [
                _Row(
                    id=row.id,
                    identity=MemoryIdentity(row.key, row.scope, row.scope_id),
                    file_path=row.file_path,
                    memory_type=row.memory_type,
                    tags=row.tags or "",
                    created_at=_utc(row.created_at),
                    updated_at=_utc(row.updated_at),
                    token_estimate=row.token_estimate,
                )
                for row in rows
            ]

    @staticmethod
    def _index_line_identity(
        scope: Optional[str],
        key: str,
        relative_path: str,
        project_scope_id: Optional[str],
    ) -> Optional[MemoryIdentity]:
        """Derive an index identity without using the index as a discovery seed."""
        if scope == "global":
            return MemoryIdentity(key, scope)
        if scope == "project" and project_scope_id is not None:
            return MemoryIdentity(key, scope, project_scope_id)
        if scope in {"session", "agent"}:
            parts = relative_path.split("/")
            if len(parts) >= 3 and parts[0] == scope:
                return MemoryIdentity(key, scope, parts[1])
        return None

    def _load_index_state(
        self, index_path: Path, project_scope_id: Optional[str] = None
    ) -> _IndexState:
        try:
            lines = tuple(index_path.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeError):
            lines = ()
        current_scope: Optional[str] = None
        entries: dict[MemoryIdentity, list[str]] = {}
        for line in lines:
            if line.startswith("## "):
                current_scope = line[3:].strip()
                continue
            match = _INDEX_ID_RE.match(line)
            if match is None:
                continue
            identity = self._index_line_identity(
                current_scope,
                match.group("key"),
                match.group("path"),
                project_scope_id,
            )
            if identity is not None:
                entries.setdefault(identity, []).append(line)
        return _IndexState(
            lines=lines,
            entries={identity: tuple(matches) for identity, matches in entries.items()},
        )

    def _load_index_states(self, topics: Iterable[_Topic]) -> dict[Path, _IndexState]:
        project_ids: dict[Path, Optional[str]] = {}
        for topic in topics:
            project_ids.setdefault(
                topic.index_path,
                topic.identity.scope_id if topic.identity.scope == "project" else None,
            )
        return {
            path: self._load_index_state(path, project_scope_id)
            for path, project_scope_id in project_ids.items()
        }

    def _index_is_current(self, topic: _Topic, state: _IndexState) -> bool:
        lines = state.entries.get(topic.identity, ())
        if len(lines) != 1:
            return False
        match = parse_index_entry(lines[0])
        if match is None:
            return False
        return (
            match.group("key") == topic.identity.key
            and match.group("path") == topic.relative_path
            and match.group("type") == topic.memory_type
            and normalize_memory_tags(match.group("tags")) == topic.tags
            and int(match.group("tokens")) == topic.index_token_estimate
            and match.group("updated") == topic.updated_text
        )

    def _resolved_row_path(self, row: _Row) -> Optional[Path]:
        try:
            return Path(row.file_path).resolve()
        except OSError:
            return None

    def _row_maps(self, rows: Iterable[_Row]) -> tuple[
        dict[MemoryIdentity, list[_Row]],
        dict[Path, list[_Row]],
    ]:
        """Index one metadata snapshot for linear topic lookups."""
        rows_by_identity: dict[MemoryIdentity, list[_Row]] = {}
        rows_by_path: dict[Path, list[_Row]] = {}
        for row in rows:
            rows_by_identity.setdefault(row.identity, []).append(row)
            resolved_path = self._resolved_row_path(row)
            if resolved_path is not None:
                rows_by_path.setdefault(resolved_path, []).append(row)
        return rows_by_identity, rows_by_path

    def _metadata_is_current(self, row: _Row, topic: _Topic) -> bool:
        return (
            row.memory_type == topic.memory_type
            and normalize_memory_tags(row.tags) == topic.tags
            and self._resolved_row_path(row) == topic.file_path
            and row.created_at == topic.created_at
            and row.updated_at == topic.updated_at
            and row.token_estimate == topic.token_estimate
        )

    def _plan_topic(
        self,
        topic: _Topic,
        rows: Iterable[_Row] = (),
        *,
        rows_by_identity: Optional[dict[MemoryIdentity, list[_Row]]] = None,
        rows_by_path: Optional[dict[Path, list[_Row]]] = None,
        index_state: Optional[_IndexState] = None,
    ) -> RepairRecord:
        if rows_by_identity is not None and rows_by_path is not None:
            identity_rows = rows_by_identity.get(topic.identity, [])
            path_rows = rows_by_path.get(topic.file_path, [])
        else:
            all_rows = list(rows)
            identity_rows = [row for row in all_rows if row.identity == topic.identity]
            path_rows = [row for row in all_rows if self._resolved_row_path(row) == topic.file_path]
        if len(identity_rows) > 1:
            return self._candidate_record(
                topic.file_path,
                RepairAction.CONFLICT,
                "duplicate_database_identity",
                "multiple SQLite rows match the canonical identity",
                topic.identity,
            )
        if any(row.identity != topic.identity for row in path_rows):
            return self._candidate_record(
                topic.file_path,
                RepairAction.CONFLICT,
                "ambiguous_database_path",
                "SQLite maps the canonical topic path to a different identity",
                topic.identity,
            )
        actions: list[RepairAction] = []
        defaulted: tuple[str, ...] = ()
        if not identity_rows:
            actions.append(RepairAction.CREATE_METADATA)
            defaulted = DEFAULTED_METADATA_FIELDS
        else:
            row = identity_rows[0]
            if self._resolved_row_path(row) != topic.file_path:
                return self._candidate_record(
                    topic.file_path,
                    RepairAction.CONFLICT,
                    "database_path_conflict",
                    "SQLite identity points to a different resolved topic path",
                    topic.identity,
                )
            if not self._metadata_is_current(row, topic):
                actions.append(RepairAction.UPDATE_METADATA)
        if index_state is None:
            project_scope_id = (
                topic.identity.scope_id if topic.identity.scope == "project" else None
            )
            index_state = self._load_index_state(topic.index_path, project_scope_id)
        if not self._index_is_current(topic, index_state):
            actions.append(RepairAction.REBUILD_INDEX)
        if not actions:
            actions.append(RepairAction.UNCHANGED)
        return RepairRecord(
            identity=topic.identity,
            file_path=str(topic.file_path),
            actions=tuple(actions),
            status="unchanged" if actions == [RepairAction.UNCHANGED] else "planned",
            defaulted_fields=defaulted,
        )

    def plan(self) -> RepairReport:
        """Return a deterministic pure-read repair plan."""
        candidates, records = self._iter_candidates()
        rows = self._load_rows()
        topics: list[_Topic] = []
        parsed_records: list[RepairRecord] = []
        for candidate in candidates:
            parsed = self._parse_candidate(candidate)
            if isinstance(parsed, RepairRecord):
                parsed_records.append(parsed)
            else:
                topics.append(parsed)
        index_states = self._load_index_states(topics)

        rows_by_identity, rows_by_path = self._row_maps(rows)

        by_identity: dict[MemoryIdentity, list[_Topic]] = {}
        by_path: dict[Path, list[_Topic]] = {}
        for topic in topics:
            by_identity.setdefault(topic.identity, []).append(topic)
            by_path.setdefault(topic.file_path, []).append(topic)
        duplicate_identities = {
            identity
            for identity, matching_topics in by_identity.items()
            if len(matching_topics) > 1
        }
        duplicate_paths = {
            path for path, matching_topics in by_path.items() if len(matching_topics) > 1
        }
        for topic in topics:
            if topic.identity in duplicate_identities:
                parsed_records.append(
                    self._candidate_record(
                        topic.file_path,
                        RepairAction.CONFLICT,
                        "duplicate_canonical_identity",
                        "multiple canonical topics resolve to the same identity",
                        topic.identity,
                    )
                )
            elif topic.file_path in duplicate_paths:
                parsed_records.append(
                    self._candidate_record(
                        topic.file_path,
                        RepairAction.CONFLICT,
                        "duplicate_canonical_path",
                        "multiple canonical identities resolve to the same topic path",
                        topic.identity,
                    )
                )
            else:
                parsed_records.append(
                    self._plan_topic(
                        topic,
                        rows_by_identity=rows_by_identity,
                        rows_by_path=rows_by_path,
                        index_state=index_states[topic.index_path],
                    )
                )

        all_records = records + parsed_records
        all_records.sort(
            key=lambda record: (
                record.identity.scope if record.identity else "",
                record.identity.scope_id or "" if record.identity else "",
                record.identity.key if record.identity else "",
                record.file_path,
                ",".join(action.value for action in record.actions),
            )
        )
        return RepairReport(records=tuple(all_records), applied=False)

    def _repair_index_batch(self, topics: list[_Topic]) -> None:
        """Replace all targeted entries in one shared-index read/write cycle."""
        if not topics:
            return
        index_path = topics[0].index_path
        index_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = index_path.parent / ".index.lock"
        with lock_path.open("w") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                if index_path.exists():
                    lines = index_path.read_text(encoding="utf-8").splitlines()
                else:
                    lines = ["# CAO Memory Index", ""]
                project_scope_id = next(
                    (
                        topic.identity.scope_id
                        for topic in topics
                        if topic.identity.scope == "project"
                    ),
                    None,
                )
                target_identities = {topic.identity for topic in topics}
                filtered: list[str] = []
                current_scope: Optional[str] = None
                for line in lines:
                    if line.startswith("## "):
                        current_scope = line[3:].strip()
                    match = _INDEX_ID_RE.match(line)
                    identity = (
                        self._index_line_identity(
                            current_scope,
                            match.group("key"),
                            match.group("path"),
                            project_scope_id,
                        )
                        if match is not None
                        else None
                    )
                    if identity not in target_identities:
                        filtered.append(line)
                lines = filtered
                latest = max(topic.updated_text for topic in topics)
                header_updated = False
                for index, line in enumerate(lines):
                    if line.startswith("<!-- Updated:"):
                        lines[index] = f"<!-- Updated: {latest} -->"
                        header_updated = True
                        break
                if not header_updated:
                    lines.insert(1 if lines else 0, f"<!-- Updated: {latest} -->")

                additions: dict[str, list[str]] = {}
                for topic in sorted(topics, key=lambda item: item.identity):
                    additions.setdefault(topic.identity.scope, []).append(
                        f"- [{topic.identity.key}]({topic.relative_path}) — "
                        f"type:{topic.memory_type} tags:{topic.tags} "
                        f"~{topic.index_token_estimate}tok updated:{topic.updated_text}"
                    )

                rendered: list[str] = []
                for line in lines:
                    rendered.append(line)
                    if line.startswith("## "):
                        rendered.extend(additions.pop(line[3:].strip(), ()))
                for scope in sorted(additions):
                    if rendered and rendered[-1] != "":
                        rendered.append("")
                    rendered.append(f"## {scope}")
                    rendered.extend(additions[scope])

                temporary = index_path.parent / ".index.md.tmp"
                temporary.write_text("\n".join(rendered) + "\n", encoding="utf-8")
                os.replace(str(temporary), str(index_path))
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)

    def _repair_metadata(self, topic: _Topic, action: RepairAction) -> None:
        from cli_agent_orchestrator.clients.database import MemoryMetadataModel

        with self._get_db_session() as db:
            query = db.query(MemoryMetadataModel).filter(
                MemoryMetadataModel.key == topic.identity.key,
                MemoryMetadataModel.scope == topic.identity.scope,
                (
                    MemoryMetadataModel.scope_id == topic.identity.scope_id
                    if topic.identity.scope_id is not None
                    else MemoryMetadataModel.scope_id.is_(None)
                ),
            )
            rows = query.all()
            if action == RepairAction.CREATE_METADATA:
                if rows:
                    raise RuntimeError("metadata identity appeared while repair lock was held")
                row = MemoryMetadataModel(
                    id=str(uuid.uuid4()),
                    key=topic.identity.key,
                    memory_type=topic.memory_type,
                    scope=topic.identity.scope,
                    scope_id=topic.identity.scope_id,
                    file_path=str(topic.file_path),
                    tags=topic.tags,
                    source_provider=None,
                    source_terminal_id=None,
                    token_estimate=topic.token_estimate,
                    created_at=topic.created_at,
                    updated_at=topic.updated_at,
                    access_count=0,
                    last_accessed_at=None,
                    last_compiled_at=None,
                    related_keys=None,
                )
                db.add(row)
            elif action == RepairAction.UPDATE_METADATA:
                if len(rows) != 1:
                    raise RuntimeError("metadata identity changed while repair lock was held")
                row = rows[0]
                row.memory_type = topic.memory_type
                row.tags = topic.tags
                row.file_path = str(topic.file_path)
                row.created_at = topic.created_at
                row.updated_at = topic.updated_at
                row.token_estimate = topic.token_estimate
            db.commit()

    def _candidate_from_record(self, record: RepairRecord) -> _Candidate:
        assert record.identity is not None
        path = Path(record.file_path)
        identity = record.identity
        if identity.scope == "project":
            container = self.base_dir / str(identity.scope_id)
            prefix = "project"
        else:
            container = self.base_dir / "global"
            prefix = (
                f"{identity.scope}/{identity.scope_id}"
                if identity.scope in {"session", "agent"}
                else identity.scope
            )
        return _Candidate(
            file_path=path,
            scope=identity.scope,
            scope_id=identity.scope_id,
            index_path=container / "wiki" / "index.md",
            relative_path=f"{prefix}/{path.name}",
        )

    @staticmethod
    def _failed_record(record: RepairRecord, exc: Exception) -> RepairRecord:
        return RepairRecord(
            identity=record.identity,
            file_path=record.file_path,
            actions=record.actions + (RepairAction.FAILED,),
            status="failed",
            finding=RepairFinding(
                kind="unexpected_error",
                message=f"unexpected {type(exc).__name__}; run cao memory repair --apply",
            ),
            defaulted_fields=record.defaulted_fields,
        )

    @staticmethod
    def _sort_records(records: list[RepairRecord]) -> None:
        records.sort(
            key=lambda record: (
                record.identity.scope if record.identity else "",
                record.identity.scope_id or "" if record.identity else "",
                record.identity.key if record.identity else "",
                record.file_path,
                ",".join(action.value for action in record.actions),
            )
        )

    def apply(self) -> RepairReport:
        """Apply valid repairs per record and raise only after all are attempted."""
        planned = self.plan()
        results: list[RepairRecord] = []
        groups: dict[Path, list[tuple[RepairRecord, _Candidate]]] = {}
        records_by_path: dict[Path, list[RepairRecord]] = {}
        resolved_paths: list[Optional[Path]] = []
        for record in planned.records:
            if record.status != "skipped" and record.identity is not None:
                resolved_path = Path(record.file_path).resolve()
                records_by_path.setdefault(resolved_path, []).append(record)
                resolved_paths.append(resolved_path)
            else:
                resolved_paths.append(None)
        duplicate_paths = {
            path for path, matching_records in records_by_path.items() if len(matching_records) > 1
        }
        for record, planned_path in zip(planned.records, resolved_paths):
            if record.status == "skipped" or record.identity is None:
                results.append(record)
                continue
            if planned_path in duplicate_paths:
                results.append(
                    self._candidate_record(
                        Path(record.file_path),
                        RepairAction.CONFLICT,
                        "duplicate_resolved_topic_path",
                        "multiple planned identities resolve to the same topic path",
                        record.identity,
                    )
                )
                continue
            candidate = self._candidate_from_record(record)
            groups.setdefault(candidate.index_path, []).append((record, candidate))

        locked_groups: dict[Path, list[tuple[RepairRecord, _Candidate, Any]]] = {}
        locked_all: list[tuple[RepairRecord, _Candidate, Any]] = []
        for index_path in sorted(groups, key=str):
            group = sorted(groups[index_path], key=lambda item: item[1].file_path)
            locked = locked_groups.setdefault(index_path, [])
            for record, candidate in group:
                lock_path = candidate.file_path.parent / f".{candidate.file_path.stem}.lock"
                lock_fd = None
                try:
                    lock_fd = lock_path.open("w")
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                    locked_item = (record, candidate, lock_fd)
                    locked.append(locked_item)
                    locked_all.append(locked_item)
                except Exception as exc:
                    if lock_fd is not None:
                        lock_fd.close()
                    results.append(self._failed_record(record, exc))

        try:
            rows_by_identity: dict[MemoryIdentity, list[_Row]] = {}
            rows_by_path: dict[Path, list[_Row]] = {}
            rows_error: Optional[Exception] = None
            try:
                rows_by_identity, rows_by_path = self._row_maps(self._load_rows())
            except Exception as exc:
                rows_error = exc

            for index_path in sorted(locked_groups, key=str):
                locked = locked_groups[index_path]
                if rows_error is not None:
                    results.extend(
                        self._failed_record(record, rows_error) for record, _, _ in locked
                    )
                    continue
                project_scope_id = next(
                    (
                        record.identity.scope_id
                        for record, _, _ in locked
                        if record.identity is not None and record.identity.scope == "project"
                    ),
                    None,
                )
                index_state = self._load_index_state(index_path, project_scope_id)
                current_items: list[tuple[RepairRecord, _Topic]] = []
                for record, candidate, _ in locked:
                    try:
                        parsed = self._parse_candidate(candidate)
                        if isinstance(parsed, RepairRecord):
                            results.append(parsed)
                            continue
                        current = self._plan_topic(
                            parsed,
                            rows_by_identity=rows_by_identity,
                            rows_by_path=rows_by_path,
                            index_state=index_state,
                        )
                        if current.status == "skipped":
                            results.append(current)
                            continue
                        current_items.append((current, parsed))
                    except Exception as exc:
                        results.append(self._failed_record(record, exc))

                rebuild_items = [
                    (record, topic)
                    for record, topic in current_items
                    if RepairAction.REBUILD_INDEX in record.actions
                ]
                index_error: Optional[Exception] = None
                try:
                    self._repair_index_batch([topic for _, topic in rebuild_items])
                except Exception as exc:
                    index_error = exc

                for current, parsed in current_items:
                    metadata_error: Optional[Exception] = None
                    try:
                        for metadata_action in (
                            RepairAction.CREATE_METADATA,
                            RepairAction.UPDATE_METADATA,
                        ):
                            if metadata_action in current.actions:
                                self._repair_metadata(parsed, metadata_action)
                    except Exception as exc:
                        metadata_error = exc

                    failure = (
                        index_error if RepairAction.REBUILD_INDEX in current.actions else None
                    ) or metadata_error
                    if failure is not None:
                        results.append(self._failed_record(current, failure))
                    else:
                        results.append(
                            replace(
                                current,
                                status=(
                                    "unchanged"
                                    if current.actions == (RepairAction.UNCHANGED,)
                                    else "repaired"
                                ),
                            )
                        )
        finally:
            for _, _, lock_fd in reversed(locked_all):
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    lock_fd.close()

        self._sort_records(results)
        report = RepairReport(records=tuple(results), applied=True)
        if report.counts["failed"]:
            raise MemoryReconciliationError(report)
        return report

    def reconcile(self, *, apply: bool = False) -> RepairReport:
        """Plan by default; mutate only when explicitly requested."""
        return self.apply() if apply else self.plan()


def reconcile_memory_startup() -> Optional[RepairReport]:
    """Apply bounded startup repair unless memory is disabled."""
    from cli_agent_orchestrator.services.settings_service import is_memory_enabled

    if not is_memory_enabled():
        return None
    return MemoryReconciliationService().apply()
