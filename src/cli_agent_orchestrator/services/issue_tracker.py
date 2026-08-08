"""Project-scoped issue tracking.

A project is a *declared* grouping: one name, any number of directories,
sessions and git remotes, one issue log. That shape is the whole point — the
CAO system spans two repositories (``cao-conductor`` and
``cli-agent-orchestrator``) plus their worktrees plus a rotating cast of tmux
sessions, and its defects belong to one list rather than to whichever checkout
happened to notice them.

Resolution is deliberately ordered and deliberately strict:

    explicit project id  ->  session name  ->  path  ->  git remote

Explicit wins because a caller that names a project has already answered the
question. Session beats path because a supervisor and its workers share a
session while working across several directories. Path beats git remote
because a worktree is a directory fact before it is a remote fact. And an
identifier resolving to two projects is refused at *registration* time rather
than resolved by row order at read time: a scope that silently picks one of
two issue logs is worse than no scope, because the filing appears to succeed.

Nothing here reads or writes ``ProjectAliasModel``. That table is the memory
subsystem's automatic identity cache; grouping two repos under one issue log
must not merge their memory recall as a side effect.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import func

from cli_agent_orchestrator.clients.database import (
    SessionLocal,
    TrackerCommentModel,
    TrackerEventModel,
    TrackerIssueModel,
    TrackerLinkModel,
    TrackerProjectModel,
    TrackerScopeModel,
)

logger = logging.getLogger(__name__)

ITEM_KINDS: Tuple[str, ...] = ("issue", "feature")

# Workflow vocabulary. `open` and `closed` are the load-bearing ends; the
# middle states exist so a long-running project can distinguish "nobody has
# looked at this" from "somebody is on it" from "it is waiting on something
# else", which is the distinction that makes a stale tracker readable.
STATUSES: Tuple[str, ...] = (
    "open",
    "triage",
    "in-progress",
    "blocked",
    "resolved",
    "closed",
    "wontfix",
    "duplicate",
)
# Statuses that mean the issue no longer needs attention. `resolved` is NOT
# terminal: it means a fix landed and verification has not been recorded.
TERMINAL_STATUSES: frozenset = frozenset({"closed", "wontfix", "duplicate"})

# P0 exists because the ledger being migrated uses it for the most severe
# class ("Lane D live acceptance can kill the production tmux server"). Two
# entries carry it; folding them into P1 would erase the one distinction their
# author made. Ordered most-severe-first, which is also their sort order.
SEVERITIES: Tuple[str, ...] = ("P0", "P1", "P2", "P3", "P4", "unset")

SCOPE_KINDS: Tuple[str, ...] = ("path", "session", "git_remote", "project_id")

LINK_KINDS: Tuple[str, ...] = ("blocks", "relates", "duplicates", "caused-by")

PROJECT_STATUSES: Tuple[str, ...] = ("active", "archived")

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PREFIX_RE = re.compile(r"^[a-z][a-z0-9-]{0,15}$")
_KEY_RE = re.compile(r"^[a-z][a-z0-9-]{0,15}-\d{1,9}$")

# Fields a caller may PATCH, and whether an empty string clears them.
_EDITABLE_FIELDS: Tuple[str, ...] = (
    "title",
    "body",
    "status",
    "severity",
    "component",
    "assignee",
    "reporter",
    "failing_command",
    "evidence",
    "resolution",
    "duplicate_of",
    "labels",
    "kind",
)

MAX_TITLE = 300
MAX_BODY = 200_000
MAX_LABELS = 32
MAX_LABEL_LEN = 64


class TrackerError(Exception):
    """A refusal the caller can act on.

    ``code`` maps to an HTTP status at the API boundary and to an exit code at
    the CLI boundary, so the same refusal reads the same way from both.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    """Serialize a stored timestamp as an explicit-UTC ISO-8601 string.

    Every timestamp in these tables is written by ``_utcnow``, so a naive value
    read back from SQLite is UTC that lost its tzinfo in transit — stamping it
    UTC restores the truth rather than assuming it. (Contrast the conductor's
    ``last_active``, which is naive *local* time; assuming UTC there shifts
    every reading by the host offset.)
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_labels(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if isinstance(item, (str, int, float))]


def normalise_labels(labels: Optional[Iterable[Any]]) -> List[str]:
    """Trim, de-duplicate and bound a label list, preserving first-seen order."""
    if labels is None:
        return []
    if isinstance(labels, str):
        labels = [chunk for chunk in re.split(r"[,\s]+", labels) if chunk]
    out: List[str] = []
    seen = set()
    for item in labels:
        text = str(item).strip()
        if not text:
            continue
        if len(text) > MAX_LABEL_LEN:
            raise TrackerError("invalid", f"label too long (max {MAX_LABEL_LEN}): {text[:32]}...")
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) > MAX_LABELS:
            raise TrackerError("invalid", f"too many labels (max {MAX_LABELS})")
    return out


def slugify(text: str) -> str:
    """Derive a project slug from a display name.

    Never invents characters: it lowercases, collapses runs of anything
    non-alphanumeric into a single hyphen, and trims. An input that reduces to
    nothing is an error rather than a generated id, because a project called
    ``"???"`` silently becoming ``project-1`` is a naming decision the caller
    did not make.
    """
    lowered = str(text or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    slug = slug[:64].rstrip("-")
    if not slug:
        raise TrackerError("invalid", f"cannot derive a project id from {text!r}")
    return slug


def _validate_slug(slug: str) -> str:
    slug = str(slug or "").strip().lower()
    if not _SLUG_RE.match(slug):
        raise TrackerError(
            "invalid",
            f"invalid project id {slug!r}: use lowercase letters, digits, '.', '_' or '-'",
        )
    return slug


def _validate_prefix(prefix: str) -> str:
    prefix = str(prefix or "").strip().lower()
    if not _PREFIX_RE.match(prefix):
        raise TrackerError(
            "invalid",
            f"invalid issue prefix {prefix!r}: use 1-16 lowercase letters, digits or '-'",
        )
    return prefix


def _validate_choice(value: str, allowed: Sequence[str], label: str) -> str:
    text = str(value or "").strip()
    if text not in allowed:
        raise TrackerError(
            "invalid", f"invalid {label} {text!r}: expected one of {', '.join(allowed)}"
        )
    return text


def _validate_kind(kind: str) -> str:
    text = str(kind or "").strip().lower()
    if text not in ITEM_KINDS:
        raise TrackerError(
            "invalid", f"invalid kind {text!r}: expected one of {', '.join(ITEM_KINDS)}"
        )
    return text


def normalise_scope_value(kind: str, value: str) -> str:
    """Canonicalise a scope value so equal things compare equal.

    Paths are realpath'd (a worktree symlinked into place must not register as
    a second, competing scope) and stripped of a trailing separator. Git
    remotes lose their scheme, credentials, ``.git`` suffix and trailing slash,
    so ``git@github.com:o/r.git`` and ``https://github.com/o/r`` are one scope.
    """
    kind = _validate_choice(kind, SCOPE_KINDS, "scope kind")
    text = str(value or "").strip()
    if not text:
        raise TrackerError("invalid", "scope value must not be empty")
    if kind == "path":
        expanded = os.path.expanduser(text)
        # Checked BEFORE realpath, which would resolve a relative path against
        # whatever directory the server happens to be running in and register
        # a scope the caller never named.
        if not os.path.isabs(expanded):
            raise TrackerError("invalid", f"path scope must be absolute: {text!r}")
        try:
            resolved = os.path.realpath(expanded)
        except OSError:
            resolved = os.path.abspath(expanded)
        return resolved.rstrip(os.sep) or os.sep
    if kind == "git_remote":
        return _normalise_remote(text)
    return text


def _normalise_remote(url: str) -> str:
    """Strip a git remote down to ``host/owner/repo``.

    Credentials are dropped rather than stored: a remote can carry
    ``https://user:token@host/...`` and this row is readable by every dashboard
    client.
    """
    text = url.strip()
    text = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", text)
    text = re.sub(r"^[^/@]*@", "", text)  # user[:token]@host
    text = re.sub(r"^([^/:]+):(\d+)/", r"\1/", text)  # ssh://host:22/o/r -> host/o/r
    text = text.replace(":", "/", 1) if "/" not in text.split(":", 1)[0] else text
    text = re.sub(r"\.git$", "", text)
    text = text.rstrip("/")
    return text.lower()


def _path_contains(scope: str, candidate: str) -> bool:
    """Whether ``candidate`` is ``scope`` or lives inside it.

    Compares path components, never string prefixes: ``/a/foo`` must not claim
    ``/a/foobar``, and that difference is exactly the
    ``cao-conductor`` / ``cao-conductor-worktrees`` case this system has to get
    right on day one.
    """
    if candidate == scope:
        return True
    return candidate.startswith(scope.rstrip(os.sep) + os.sep)


# --------------------------------------------------------------------------
# projects
# --------------------------------------------------------------------------


def _project_row(
    row: TrackerProjectModel, *, counts: Optional[Dict[str, int]] = None
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "id": row.id,
        "name": row.name,
        "description": row.description or "",
        "status": row.status,
        "issue_prefix": row.issue_prefix,
        "next_issue_number": row.next_issue_number,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }
    if counts is not None:
        out["counts"] = counts
    return out


def create_project(
    *,
    name: str,
    project_id: Optional[str] = None,
    description: str = "",
    issue_prefix: Optional[str] = None,
    scopes: Optional[Iterable[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Create a project and, atomically, its initial scopes.

    Scopes are validated and inserted in the same transaction as the project.
    A half-created project whose scopes were rejected would resolve nothing and
    look like a working project, so either all of it exists or none of it does.
    """
    display = str(name or "").strip()
    if not display:
        raise TrackerError("invalid", "project name must not be empty")
    slug = _validate_slug(project_id) if project_id else slugify(display)
    prefix = _validate_prefix(issue_prefix) if issue_prefix else _default_prefix(slug)

    prepared = [
        (
            normalise_scope_value(s.get("kind", ""), s.get("value", "")),
            _validate_choice(s.get("kind", ""), SCOPE_KINDS, "scope kind"),
        )
        for s in (scopes or [])
    ]
    # Two scopes in ONE request that normalise to the same value used to reach
    # the unique constraint and surface as a raw 500. It is the same conflict as
    # a cross-project clash and deserves the same answer.
    seen_values = set()
    for value, _kind in prepared:
        if value in seen_values:
            raise TrackerError("conflict", f"scope {value!r} is listed twice in this request")
        seen_values.add(value)

    with SessionLocal() as db:
        if db.get(TrackerProjectModel, slug) is not None:
            raise TrackerError("conflict", f"project {slug!r} already exists")
        _assert_prefix_free(db, prefix, owner=slug)
        for value, _kind in prepared:
            clash = db.query(TrackerScopeModel).filter(TrackerScopeModel.value == value).first()
            if clash is not None:
                raise TrackerError(
                    "conflict",
                    f"scope {value!r} is already registered to project {clash.project_id!r}",
                )
        row = TrackerProjectModel(
            id=slug,
            name=display,
            description=str(description or ""),
            status="active",
            issue_prefix=prefix,
            next_issue_number=1,
        )
        db.add(row)
        for value, kind in prepared:
            db.add(TrackerScopeModel(project_id=slug, kind=kind, value=value))
        db.commit()
        db.refresh(row)
        return _project_row(row)


def _assert_prefix_free(db: Any, prefix: str, *, owner: str) -> None:
    """Refuse a key prefix another project already uses.

    Issue keys are unique across the whole installation, not per project, so
    that `cond-0242` in a commit message, a report or an evidence path means
    exactly one thing. Two projects sharing a prefix would therefore collide at
    key-allocation time with a confusing "issue already exists" from a project
    the caller never mentioned. Refusing here moves that failure to the moment
    somebody chooses the prefix, where it is actionable.
    """
    clash = (
        db.query(TrackerProjectModel)
        .filter(TrackerProjectModel.issue_prefix == prefix, TrackerProjectModel.id != owner)
        .first()
    )
    if clash is not None:
        raise TrackerError(
            "conflict",
            f"issue prefix {prefix!r} is already used by project {clash.id!r}; "
            "issue keys are unique across the installation, so each project needs its own",
        )

    # A project that VACATED a prefix leaves its issues behind still holding
    # those keys, so "no project owns it" is not the same as "the namespace is
    # free". Handing it to a new project made every filing there collide with a
    # key that already exists — and because the counter bump and the insert
    # share one transaction, the rollback took the bump with it, so the counter
    # never advanced and the wedge was permanent. The key table is the real
    # namespace; ask it.
    taken = (
        db.query(TrackerIssueModel)
        .filter(TrackerIssueModel.key.like(f"{prefix}-%"))
        .filter(TrackerIssueModel.project_id != owner)
        .first()
    )
    if taken is not None:
        raise TrackerError(
            "conflict",
            f"issue prefix {prefix!r} is still held by existing keys "
            f"(e.g. {taken.key} in project {taken.project_id!r}); keys outlive the "
            "project that minted them, so the prefix is not free",
        )


def _default_prefix(slug: str) -> str:
    """Derive an issue prefix from a slug: initials for multiword, else the slug."""
    parts = [p for p in re.split(r"[^a-z0-9]+", slug) if p]
    if len(parts) > 1:
        initials = "".join(p[0] for p in parts)[:16]
        if _PREFIX_RE.match(initials):
            return initials
    candidate = (parts[0] if parts else slug)[:16]
    return candidate if _PREFIX_RE.match(candidate) else "issue"


def list_projects(*, include_archived: bool = False) -> List[Dict[str, Any]]:
    """List projects with per-status issue counts (issue-only legacy) plus by_kind."""
    with SessionLocal() as db:
        query = db.query(TrackerProjectModel)
        if not include_archived:
            query = query.filter(TrackerProjectModel.status == "active")
        rows = query.order_by(TrackerProjectModel.name.asc()).all()
        # Single snapshot for all counts to avoid concurrency drift (PR1-2 gate 4)
        all_issues = db.query(TrackerIssueModel).all()
        # Compute tallies in Python from one snapshot
        tallies: Dict[str, int] = {}
        open_tallies: Dict[str, int] = {}
        all_tallies: Dict[str, int] = {}
        all_open: Dict[str, int] = {}
        by_kind_total: Dict[str, Dict[str, int]] = {k: {} for k in ITEM_KINDS}
        by_kind_open: Dict[str, Dict[str, int]] = {k: {} for k in ITEM_KINDS}
        for iss in all_issues:
            pid = iss.project_id
            kind = getattr(iss, "kind", "issue")
            is_open = iss.status not in TERMINAL_STATUSES
            # all
            all_tallies[pid] = all_tallies.get(pid, 0) + 1
            if is_open:
                all_open[pid] = all_open.get(pid, 0) + 1
            # by_kind
            by_kind_total[kind][pid] = by_kind_total[kind].get(pid, 0) + 1
            if is_open:
                by_kind_open[kind][pid] = by_kind_open[kind].get(pid, 0) + 1
            # legacy issue-only
            if kind == "issue":
                tallies[pid] = tallies.get(pid, 0) + 1
                if is_open:
                    open_tallies[pid] = open_tallies.get(pid, 0) + 1
        out = []
        for row in rows:
            counts: Dict[str, Any] = {
                "total": int(tallies.get(row.id, 0)),
                "open": int(open_tallies.get(row.id, 0)),
                "all_total": int(all_tallies.get(row.id, 0)),
                "all_open": int(all_open.get(row.id, 0)),
                "by_kind": {
                    k: {
                        "total": int(by_kind_total[k].get(row.id, 0)),
                        "open": int(by_kind_open[k].get(row.id, 0)),
                    }
                    for k in ITEM_KINDS
                },
            }
            out.append(_project_row(row, counts=counts))
        return out


def get_project(project_id: str) -> Dict[str, Any]:
    """Return one project with its scopes and issue counts."""
    slug = _validate_slug(project_id)
    with SessionLocal() as db:
        row = db.get(TrackerProjectModel, slug)
        if row is None:
            raise TrackerError("not-found", f"no such project: {slug}")
        scopes = (
            db.query(TrackerScopeModel)
            .filter(TrackerScopeModel.project_id == slug)
            .order_by(TrackerScopeModel.kind.asc(), TrackerScopeModel.value.asc())
            .all()
        )
        # Single snapshot for project stats (PR1-2 gate 4)
        all_issues = db.query(TrackerIssueModel).filter(TrackerIssueModel.project_id == slug).all()
        by_status: Dict[str, int] = {}
        all_by_status: Dict[str, int] = {}
        by_kind: Dict[str, Any] = {k: {"total": 0, "open": 0, "by_status": {}} for k in ITEM_KINDS}
        for iss in all_issues:
            kind = getattr(iss, "kind", "issue")
            all_by_status[iss.status] = all_by_status.get(iss.status, 0) + 1
            if kind == "issue":
                by_status[iss.status] = by_status.get(iss.status, 0) + 1
            # per-kind
            sub = by_kind[kind]
            sub["by_status"][iss.status] = sub["by_status"].get(iss.status, 0) + 1
        for k in ITEM_KINDS:
            sub = by_kind[k]["by_status"]
            by_kind[k]["total"] = sum(int(v) for v in sub.values())
            by_kind[k]["open"] = sum(int(v) for kk, v in sub.items() if kk not in TERMINAL_STATUSES)
        total = sum(int(v) for v in by_status.values())
        open_count = sum(int(v) for k, v in by_status.items() if k not in TERMINAL_STATUSES)
        payload = _project_row(
            row,
            counts={
                "total": total,
                "open": open_count,
                "by_status": {k: int(v) for k, v in by_status.items()},
                "by_kind": by_kind,
                "all_total": sum(int(v) for v in all_by_status.values()),
                "all_open": sum(
                    int(v) for kk, v in all_by_status.items() if kk not in TERMINAL_STATUSES
                ),
            },
        )
        payload["scopes"] = [
            {"id": s.id, "kind": s.kind, "value": s.value, "created_at": _iso(s.created_at)}
            for s in scopes
        ]
        return payload


def update_project(
    project_id: str,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = None,
    issue_prefix: Optional[str] = None,
) -> Dict[str, Any]:
    """Rename, re-describe, archive or re-prefix a project.

    Changing ``issue_prefix`` affects only keys allocated afterwards. Existing
    keys are never rewritten — they are quoted in commit messages, reports and
    evidence paths that this database cannot reach.
    """
    slug = _validate_slug(project_id)
    with SessionLocal() as db:
        row = db.get(TrackerProjectModel, slug)
        if row is None:
            raise TrackerError("not-found", f"no such project: {slug}")
        if name is not None:
            display = str(name).strip()
            if not display:
                raise TrackerError("invalid", "project name must not be empty")
            row.name = display
        if description is not None:
            row.description = str(description)
        if status is not None:
            row.status = _validate_choice(status, PROJECT_STATUSES, "project status")
        if issue_prefix is not None:
            candidate = _validate_prefix(issue_prefix)
            _assert_prefix_free(db, candidate, owner=slug)
            row.issue_prefix = candidate
        db.commit()
        db.refresh(row)
        return _project_row(row)


def delete_project(project_id: str, *, force: bool = False) -> Dict[str, Any]:
    """Delete a project, its scopes, and (only with ``force``) its issues.

    Refuses by default when issues exist. Deleting an issue log because
    somebody tidied a project list is not recoverable, and the archived state
    covers every non-destructive reason to make a project go away.
    """
    slug = _validate_slug(project_id)
    with SessionLocal() as db:
        row = db.get(TrackerProjectModel, slug)
        if row is None:
            raise TrackerError("not-found", f"no such project: {slug}")
        issues = db.query(TrackerIssueModel).filter(TrackerIssueModel.project_id == slug).all()
        if issues and not force:
            raise TrackerError(
                "conflict",
                f"project {slug!r} still holds {len(issues)} issue(s); archive it, or pass force to delete them",
            )
        keys = [issue.key for issue in issues]
        if keys:
            db.query(TrackerCommentModel).filter(TrackerCommentModel.issue_key.in_(keys)).delete(
                synchronize_session=False
            )
            db.query(TrackerEventModel).filter(TrackerEventModel.issue_key.in_(keys)).delete(
                synchronize_session=False
            )
            db.query(TrackerLinkModel).filter(
                (TrackerLinkModel.from_key.in_(keys)) | (TrackerLinkModel.to_key.in_(keys))
            ).delete(synchronize_session=False)
            db.query(TrackerIssueModel).filter(TrackerIssueModel.project_id == slug).delete(
                synchronize_session=False
            )
        db.query(TrackerScopeModel).filter(TrackerScopeModel.project_id == slug).delete(
            synchronize_session=False
        )
        db.delete(row)
        db.commit()
        return {"id": slug, "deleted": True, "issues_deleted": len(keys)}


# --------------------------------------------------------------------------
# scopes
# --------------------------------------------------------------------------


def add_scope(project_id: str, *, kind: str, value: str) -> Dict[str, Any]:
    """Register one identifier as resolving to this project."""
    slug = _validate_slug(project_id)
    kind = _validate_choice(kind, SCOPE_KINDS, "scope kind")
    canonical = normalise_scope_value(kind, value)
    with SessionLocal() as db:
        if db.get(TrackerProjectModel, slug) is None:
            raise TrackerError("not-found", f"no such project: {slug}")
        existing = db.query(TrackerScopeModel).filter(TrackerScopeModel.value == canonical).first()
        if existing is not None:
            if existing.project_id == slug and existing.kind == kind:
                return {
                    "id": existing.id,
                    "project_id": existing.project_id,
                    "kind": existing.kind,
                    "value": existing.value,
                    "created_at": _iso(existing.created_at),
                    "created": False,
                }
            raise TrackerError(
                "conflict",
                f"scope {canonical!r} is already registered to project {existing.project_id!r}",
            )
        row = TrackerScopeModel(project_id=slug, kind=kind, value=canonical)
        db.add(row)
        db.commit()
        db.refresh(row)
        return {
            "id": row.id,
            "project_id": row.project_id,
            "kind": row.kind,
            "value": row.value,
            "created_at": _iso(row.created_at),
            "created": True,
        }


def remove_scope(project_id: str, scope_id: int) -> Dict[str, Any]:
    """Drop one scope row."""
    slug = _validate_slug(project_id)
    with SessionLocal() as db:
        row = db.get(TrackerScopeModel, int(scope_id))
        if row is None or row.project_id != slug:
            raise TrackerError("not-found", f"no scope {scope_id} on project {slug}")
        db.delete(row)
        db.commit()
        return {"id": int(scope_id), "deleted": True}


def list_scopes(project_id: Optional[str] = None) -> List[Dict[str, Any]]:
    with SessionLocal() as db:
        query = db.query(TrackerScopeModel)
        if project_id:
            query = query.filter(TrackerScopeModel.project_id == _validate_slug(project_id))
        rows = query.order_by(
            TrackerScopeModel.project_id.asc(), TrackerScopeModel.kind.asc()
        ).all()
        return [
            {
                "id": r.id,
                "project_id": r.project_id,
                "kind": r.kind,
                "value": r.value,
                "created_at": _iso(r.created_at),
            }
            for r in rows
        ]


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Resolution:
    """How a filing site resolved, and by what evidence.

    ``matched_by`` is part of the answer, not decoration: a caller that filed
    into the wrong project needs to see whether it was the session, the path or
    the remote that put it there.
    """

    project_id: Optional[str]
    matched_by: Optional[str]
    matched_value: Optional[str]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "matched_by": self.matched_by,
            "matched_value": self.matched_value,
        }


def resolve_project(
    *,
    project: Optional[str] = None,
    session: Optional[str] = None,
    alias: Optional[str] = None,
    cwd: Optional[str] = None,
    git_remote: Optional[str] = None,
) -> Resolution:
    """Resolve a filing site to a project.

    ``alias`` matches a ``project_id``-kind scope: an identifier from some
    other system that names this project — a conductor campaign name, a CAO
    memory project id. It ranks just under ``session`` because, like a session,
    it is something the caller stated rather than something inferred from where
    a process happens to be running.

    Returns an *unmatched* Resolution rather than raising when nothing matches:
    "which project is this?" has a legitimate answer of "none registered", and
    callers differ on whether that is fatal.
    """
    if project:
        slug = _validate_slug(project)
        with SessionLocal() as db:
            if db.get(TrackerProjectModel, slug) is None:
                raise TrackerError("not-found", f"no such project: {slug}")
        return Resolution(slug, "explicit", slug)

    with SessionLocal() as db:
        if session:
            row = (
                db.query(TrackerScopeModel)
                .filter(TrackerScopeModel.kind == "session", TrackerScopeModel.value == session)
                .first()
            )
            if row is not None:
                return Resolution(row.project_id, "session", row.value)

        if alias:
            row = (
                db.query(TrackerScopeModel)
                .filter(TrackerScopeModel.kind == "project_id", TrackerScopeModel.value == alias)
                .first()
            )
            if row is not None:
                return Resolution(row.project_id, "alias", row.value)

        if cwd:
            resolved = normalise_scope_value("path", cwd)
            candidates = db.query(TrackerScopeModel).filter(TrackerScopeModel.kind == "path").all()
            # Longest scope wins: a project registered on
            # ~/Projects/cao-conductor/gateway is more specific than one
            # registered on ~/Projects, and the specific answer is the one the
            # operator meant.
            best: Optional[TrackerScopeModel] = None
            for candidate in candidates:
                if _path_contains(candidate.value, resolved):
                    if best is None or len(candidate.value) > len(best.value):
                        best = candidate
            if best is not None:
                return Resolution(best.project_id, "path", best.value)

        remote = git_remote or (_git_remote_for(cwd) if cwd else None)
        if remote:
            canonical = _normalise_remote(remote)
            row = (
                db.query(TrackerScopeModel)
                .filter(
                    TrackerScopeModel.kind == "git_remote", TrackerScopeModel.value == canonical
                )
                .first()
            )
            if row is not None:
                return Resolution(row.project_id, "git_remote", row.value)

    return Resolution(None, None, None)


def _git_remote_for(cwd: str) -> Optional[str]:
    """Read ``remote.origin.url`` for a directory, or None.

    Bounded and non-fatal: resolution runs on the filing path, and a slow or
    broken git must degrade to "no remote match", never hang the filing.
    """
    import subprocess

    try:
        path = Path(os.path.expanduser(cwd))
        if not path.is_dir():
            return None
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug(f"git remote lookup failed in {cwd}: {exc}")
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


# --------------------------------------------------------------------------
# issues
# --------------------------------------------------------------------------


def _issue_row(row: TrackerIssueModel) -> Dict[str, Any]:
    return {
        "key": row.key,
        "project_id": row.project_id,
        "kind": getattr(row, "kind", "issue") or "issue",
        "title": row.title,
        "body": row.body or "",
        "status": row.status,
        "severity": row.severity,
        "component": row.component,
        "reporter": row.reporter,
        "assignee": row.assignee,
        "labels": _parse_labels(row.labels),
        "failing_command": row.failing_command,
        "evidence": row.evidence,
        "resolution": row.resolution,
        "session_name": row.session_name,
        "terminal_id": row.terminal_id,
        "source_path": row.source_path,
        "duplicate_of": row.duplicate_of,
        "origin": row.origin,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "closed_at": _iso(row.closed_at),
    }


def _allocate_key(db: Any, project: TrackerProjectModel) -> str:
    """Allocate the next issue key by compare-and-swap on the counter.

    SQLite has no ``SELECT ... FOR UPDATE``, so the counter is claimed with a
    conditional UPDATE and the caller retries if somebody else won. The unique
    constraint on ``key`` is the backstop: two racers can never both persist
    the same key even if this loop were wrong.
    """
    for _ in range(50):
        current = int(project.next_issue_number or 1)
        claimed = (
            db.query(TrackerProjectModel)
            .filter(
                TrackerProjectModel.id == project.id,
                TrackerProjectModel.next_issue_number == current,
            )
            .update({TrackerProjectModel.next_issue_number: current + 1}, synchronize_session=False)
        )
        if claimed:
            return f"{project.issue_prefix}-{current:04d}"
        db.refresh(project)
    raise TrackerError("conflict", f"could not allocate an issue key for {project.id}")


def create_issue(
    *,
    project_id: Optional[str] = None,
    title: str,
    body: str = "",
    status: str = "open",
    severity: str = "unset",
    component: Optional[str] = None,
    reporter: Optional[str] = None,
    assignee: Optional[str] = None,
    labels: Optional[Iterable[Any]] = None,
    failing_command: Optional[str] = None,
    evidence: Optional[str] = None,
    session_name: Optional[str] = None,
    terminal_id: Optional[str] = None,
    source_path: Optional[str] = None,
    origin: str = "api",
    key: Optional[str] = None,
    created_at: Optional[datetime] = None,
    cwd: Optional[str] = None,
    alias: Optional[str] = None,
    kind: str = "issue",
) -> Dict[str, Any]:
    """File an issue.

    With no ``project_id`` the filing site is resolved from ``session_name`` /
    ``cwd``; an unresolvable site is refused rather than dropped into a default
    bucket, because an issue filed into the wrong log is harder to find than
    one that failed loudly at filing time.
    """
    heading = str(title or "").strip()
    if not heading:
        raise TrackerError("invalid", "issue title must not be empty")
    if len(heading) > MAX_TITLE:
        raise TrackerError("invalid", f"title too long (max {MAX_TITLE} chars)")
    text = str(body or "")
    if len(text) > MAX_BODY:
        raise TrackerError("invalid", f"body too long (max {MAX_BODY} chars)")

    status = _validate_choice(status, STATUSES, "status")
    severity = _validate_choice(severity, SEVERITIES, "severity")
    kind = _validate_kind(kind)
    if status == "duplicate":
        raise TrackerError(
            "invalid",
            "duplicate status requires duplicate_of canonical key (set via update after creation)",
        )
    if kind == "feature" and failing_command:
        raise TrackerError("invalid", "failing_command is not allowed for feature requests")
    label_list = normalise_labels(labels)

    resolution = resolve_project(
        project=project_id, session=session_name, alias=alias, cwd=cwd or source_path
    )
    if resolution.project_id is None:
        raise TrackerError(
            "unresolved",
            "cannot resolve a project for this issue: pass an explicit project, "
            "or register a path/session/git_remote scope for this filing site",
        )

    if key is not None:
        key = str(key).strip().lower()
        if not _KEY_RE.match(key):
            raise TrackerError("invalid", f"invalid issue key {key!r}")

    with SessionLocal() as db:
        project = db.get(TrackerProjectModel, resolution.project_id)
        if project is None:
            raise TrackerError("not-found", f"no such project: {resolution.project_id}")
        if key is None:
            issue_key = _allocate_key(db, project)
        else:
            if db.query(TrackerIssueModel).filter(TrackerIssueModel.key == key).first():
                raise TrackerError("conflict", f"issue {key} already exists")
            issue_key = key
            # An explicit key must not be handed out again later.
            number = int(key.rsplit("-", 1)[1])
            if number >= int(project.next_issue_number or 1):
                project.next_issue_number = number + 1

        stamp = created_at or _utcnow()
        row = TrackerIssueModel(
            key=issue_key,
            project_id=project.id,
            kind=kind,
            title=heading,
            body=text,
            status=status,
            severity=severity,
            component=(component or None),
            reporter=(reporter or None),
            assignee=(assignee or None),
            labels=json.dumps(label_list),
            failing_command=(failing_command or None),
            evidence=(evidence or None),
            session_name=(session_name or None),
            terminal_id=(terminal_id or None),
            source_path=(source_path or None),
            origin=str(origin or "api"),
            created_at=stamp,
            updated_at=stamp,
            closed_at=stamp if status in TERMINAL_STATUSES else None,
        )
        db.add(row)
        db.add(
            TrackerEventModel(
                issue_key=issue_key,
                actor=(reporter or None),
                kind="created",
                field=None,
                old_value=None,
                new_value=heading,
                created_at=stamp,
            )
        )
        db.commit()
        db.refresh(row)
        payload = _issue_row(row)
        payload["resolved_by"] = resolution.matched_by
        return payload


def create_feature(
    *,
    project_id: Optional[str] = None,
    title: str,
    body: str = "",
    status: str = "open",
    severity: str = "unset",
    component: Optional[str] = None,
    reporter: Optional[str] = None,
    assignee: Optional[str] = None,
    labels: Optional[Iterable[Any]] = None,
    failing_command: Optional[str] = None,
    evidence: Optional[str] = None,
    session_name: Optional[str] = None,
    terminal_id: Optional[str] = None,
    source_path: Optional[str] = None,
    origin: str = "api",
    key: Optional[str] = None,
    created_at: Optional[datetime] = None,
    cwd: Optional[str] = None,
    alias: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a feature request (thin wrapper over create_issue with kind=feature)."""
    if failing_command:
        raise TrackerError("invalid", "failing_command is not allowed for feature requests")
    return create_issue(
        project_id=project_id,
        title=title,
        body=body,
        status=status,
        severity=severity,
        component=component,
        reporter=reporter,
        assignee=assignee,
        labels=labels,
        failing_command=None,
        evidence=evidence,
        session_name=session_name,
        terminal_id=terminal_id,
        source_path=source_path,
        origin=origin,
        key=key,
        created_at=created_at,
        cwd=cwd,
        alias=alias,
        kind="feature",
    )


def get_issue(key: str) -> Dict[str, Any]:
    """Return one issue with its comments, events and links."""
    key = str(key or "").strip().lower()
    with SessionLocal() as db:
        row = db.query(TrackerIssueModel).filter(TrackerIssueModel.key == key).first()
        if row is None:
            raise TrackerError("not-found", f"no such issue: {key}")
        payload = _issue_row(row)
        payload["comments"] = [
            {
                "id": c.id,
                "author": c.author,
                "body": c.body,
                "created_at": _iso(c.created_at),
            }
            for c in db.query(TrackerCommentModel)
            .filter(TrackerCommentModel.issue_key == key)
            .order_by(TrackerCommentModel.created_at.asc(), TrackerCommentModel.id.asc())
            .all()
        ]
        payload["events"] = [
            {
                "id": e.id,
                "actor": e.actor,
                "kind": e.kind,
                "field": e.field,
                "old_value": e.old_value,
                "new_value": e.new_value,
                "created_at": _iso(e.created_at),
            }
            for e in db.query(TrackerEventModel)
            .filter(TrackerEventModel.issue_key == key)
            .order_by(TrackerEventModel.created_at.asc(), TrackerEventModel.id.asc())
            .all()
        ]
        payload["links"] = [
            {"id": l.id, "kind": l.kind, "from_key": l.from_key, "to_key": l.to_key}
            for l in db.query(TrackerLinkModel)
            .filter((TrackerLinkModel.from_key == key) | (TrackerLinkModel.to_key == key))
            .order_by(TrackerLinkModel.id.asc())
            .all()
        ]
        return payload


def list_issues(
    *,
    project_id: Optional[str] = None,
    status: Optional[Sequence[str]] = None,
    severity: Optional[Sequence[str]] = None,
    component: Optional[str] = None,
    assignee: Optional[str] = None,
    reporter: Optional[str] = None,
    label: Optional[str] = None,
    query: Optional[str] = None,
    open_only: bool = False,
    limit: int = 100,
    offset: int = 0,
    order: str = "created_desc",
    kind: Optional[str] = "issue",
) -> Dict[str, Any]:
    """List issues with filters, returning rows plus the unpaged total.

    ``total`` is the count BEFORE limit/offset so a caller can page without
    guessing whether a short page means the end.
    """
    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))

    with SessionLocal() as db:
        q = db.query(TrackerIssueModel)
        if kind is not None:
            if kind == "all":
                pass
            else:
                _validate_kind(kind)
                q = q.filter(TrackerIssueModel.kind == kind)
        else:
            # kind=None means all kinds (explicit generic surface)
            pass
        if project_id:
            q = q.filter(TrackerIssueModel.project_id == _validate_slug(project_id))
        if status:
            wanted = [_validate_choice(s, STATUSES, "status") for s in status]
            q = q.filter(TrackerIssueModel.status.in_(wanted))
        if open_only:
            q = q.filter(TrackerIssueModel.status.notin_(tuple(TERMINAL_STATUSES)))
        if severity:
            wanted_sev = [_validate_choice(s, SEVERITIES, "severity") for s in severity]
            q = q.filter(TrackerIssueModel.severity.in_(wanted_sev))
        if component:
            q = q.filter(TrackerIssueModel.component == component)
        if assignee:
            q = q.filter(TrackerIssueModel.assignee == assignee)
        if reporter:
            q = q.filter(TrackerIssueModel.reporter == reporter)
        if label:
            # Substring match against the JSON array. Quoted on both sides so
            # `ui` cannot match `ui-polish`. `%` and `_` are LIKE wildcards and
            # are legal label characters, so they are escaped — otherwise a
            # label of `_` matches every single-character label, which is a
            # silently over-broad filter rather than an error anybody sees.
            needle = label.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            q = q.filter(TrackerIssueModel.labels.like(f'%"{needle}"%', escape="\\"))
        if query:
            needle = f"%{query.strip()}%"
            q = q.filter(
                TrackerIssueModel.title.ilike(needle)
                | TrackerIssueModel.body.ilike(needle)
                | TrackerIssueModel.key.ilike(needle)
                | TrackerIssueModel.failing_command.ilike(needle)
                | TrackerIssueModel.evidence.ilike(needle)
            )

        total = q.count()
        q = _apply_order(q, order)
        rows = q.limit(limit).offset(offset).all()
        return {
            "total": int(total),
            "limit": limit,
            "offset": offset,
            "issues": [_issue_row(r) for r in rows],
        }


def list_features(
    *,
    project_id: Optional[str] = None,
    status: Optional[Sequence[str]] = None,
    severity: Optional[Sequence[str]] = None,
    component: Optional[str] = None,
    assignee: Optional[str] = None,
    reporter: Optional[str] = None,
    label: Optional[str] = None,
    query: Optional[str] = None,
    open_only: bool = False,
    limit: int = 100,
    offset: int = 0,
    order: str = "created_desc",
) -> Dict[str, Any]:
    """List feature requests (thin wrapper with kind=feature)."""
    return list_issues(
        project_id=project_id,
        status=status,
        severity=severity,
        component=component,
        assignee=assignee,
        reporter=reporter,
        label=label,
        query=query,
        open_only=open_only,
        limit=limit,
        offset=offset,
        order=order,
        kind="feature",
    )


def _apply_order(q: Any, order: str) -> Any:
    if order == "created_asc":
        return q.order_by(TrackerIssueModel.created_at.asc(), TrackerIssueModel.id.asc())
    if order == "updated_desc":
        return q.order_by(TrackerIssueModel.updated_at.desc(), TrackerIssueModel.id.desc())
    if order == "severity":
        # SQLite sorts the literal strings, and "P0" < "P1" < ... < "unset"
        # lexicographically already, which is the intended rank.
        return q.order_by(TrackerIssueModel.severity.asc(), TrackerIssueModel.created_at.asc())
    if order == "key":
        return q.order_by(TrackerIssueModel.key.asc())
    return q.order_by(TrackerIssueModel.created_at.desc(), TrackerIssueModel.id.desc())


def update_issue(issue_key: str, *, actor: Optional[str] = None, **changes: Any) -> Dict[str, Any]:
    """Apply field changes, recording one audit event per field that moved.

    Only fields the caller actually passed are touched, and a field whose new
    value equals the old one writes no event — an audit trail full of no-ops is
    an audit trail nobody reads.

    The positional parameter is ``issue_key`` rather than ``key`` so that
    ``update_issue(k, key=...)`` reports the intended "not editable" refusal
    instead of a TypeError about duplicate arguments.
    """
    key = str(issue_key or "").strip().lower()
    unknown = set(changes) - set(_EDITABLE_FIELDS)
    if unknown:
        raise TrackerError("invalid", f"not editable: {', '.join(sorted(unknown))}")

    with SessionLocal() as db:
        row = db.query(TrackerIssueModel).filter(TrackerIssueModel.key == key).first()
        if row is None:
            raise TrackerError("not-found", f"no such issue: {key}")

        now = _utcnow()
        events: List[TrackerEventModel] = []
        # Duplicate status requires canonical key (P1)
        if (
            changes.get("status") == "duplicate"
            and not changes.get("duplicate_of")
            and not getattr(row, "duplicate_of", None)
        ):
            raise TrackerError("invalid", "duplicate status requires duplicate_of canonical key")
        # Determine target kind after change (for kind-switch validation)
        target_kind = (
            _validate_kind(changes["kind"]) if "kind" in changes else getattr(row, "kind", "issue")
        )
        if (
            target_kind == "feature"
            and "failing_command" in changes
            and changes["failing_command"]
            and str(changes["failing_command"]).strip()
        ):
            raise TrackerError("invalid", "failing_command is not allowed for feature requests")
        # If switching to feature and existing failing_command would be retained, clear it
        if (
            target_kind == "feature"
            and getattr(row, "failing_command", None)
            and "kind" in changes
            and "failing_command" not in changes
        ):
            # Auto-clear stale failing_command when becoming a feature
            old_fc = row.failing_command
            row.failing_command = None
            events.append(
                TrackerEventModel(
                    issue_key=key,
                    actor=actor,
                    kind="field",
                    field="failing_command",
                    old_value=_as_text(old_fc),
                    new_value=None,
                    created_at=now,
                )
            )

        for field, raw in changes.items():
            if raw is None:
                continue
            old, new = _coerce_field(row, field, raw, db=db)
            if old == new:
                continue
            setattr(row, field, new)
            events.append(
                TrackerEventModel(
                    issue_key=key,
                    actor=actor,
                    kind="field",
                    field=field,
                    old_value=_as_text(old),
                    new_value=_as_text(new),
                    created_at=now,
                )
            )
            if field == "status":
                row.closed_at = now if new in TERMINAL_STATUSES else None

        if not events:
            return _issue_row(row)

        row.updated_at = now
        for event in events:
            db.add(event)
        db.commit()
        db.refresh(row)
        return _issue_row(row)


def _as_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value)


def _coerce_field(row: TrackerIssueModel, field: str, raw: Any, *, db: Any) -> Tuple[Any, Any]:
    """Validate one field change and return ``(old, new)`` in stored form."""
    if field == "kind":
        return getattr(row, "kind", "issue"), _validate_kind(raw)
    if field == "status":
        return row.status, _validate_choice(raw, STATUSES, "status")
    if field == "severity":
        return row.severity, _validate_choice(raw, SEVERITIES, "severity")
    if field == "labels":
        return row.labels, json.dumps(normalise_labels(raw))
    if field == "title":
        text = str(raw).strip()
        if not text:
            raise TrackerError("invalid", "issue title must not be empty")
        if len(text) > MAX_TITLE:
            raise TrackerError("invalid", f"title too long (max {MAX_TITLE} chars)")
        return row.title, text
    if field == "body":
        text = str(raw)
        if len(text) > MAX_BODY:
            raise TrackerError("invalid", f"body too long (max {MAX_BODY} chars)")
        return row.body, text
    if field == "duplicate_of":
        text = str(raw).strip().lower()
        if not text:
            return row.duplicate_of, None
        if text == row.key:
            raise TrackerError("invalid", "an issue cannot duplicate itself")
        if db.query(TrackerIssueModel).filter(TrackerIssueModel.key == text).first() is None:
            raise TrackerError("not-found", f"no such issue: {text}")
        return row.duplicate_of, text
    # Remaining free-text fields: an empty string is an explicit clear.
    text = str(raw)
    return getattr(row, field), (text if text.strip() else None)


def delete_issue(key: str) -> Dict[str, Any]:
    """Delete an issue and everything attached to it."""
    key = str(key or "").strip().lower()
    with SessionLocal() as db:
        row = db.query(TrackerIssueModel).filter(TrackerIssueModel.key == key).first()
        if row is None:
            raise TrackerError("not-found", f"no such issue: {key}")
        db.query(TrackerCommentModel).filter(TrackerCommentModel.issue_key == key).delete(
            synchronize_session=False
        )
        db.query(TrackerEventModel).filter(TrackerEventModel.issue_key == key).delete(
            synchronize_session=False
        )
        db.query(TrackerLinkModel).filter(
            (TrackerLinkModel.from_key == key) | (TrackerLinkModel.to_key == key)
        ).delete(synchronize_session=False)
        db.delete(row)
        db.commit()
        return {"key": key, "deleted": True}


def add_comment(key: str, *, body: str, author: Optional[str] = None) -> Dict[str, Any]:
    """Append a comment and record it in the audit trail."""
    key = str(key or "").strip().lower()
    text = str(body or "").strip()
    if not text:
        raise TrackerError("invalid", "comment body must not be empty")
    if len(text) > MAX_BODY:
        raise TrackerError("invalid", f"comment too long (max {MAX_BODY} chars)")
    with SessionLocal() as db:
        row = db.query(TrackerIssueModel).filter(TrackerIssueModel.key == key).first()
        if row is None:
            raise TrackerError("not-found", f"no such issue: {key}")
        now = _utcnow()
        comment = TrackerCommentModel(issue_key=key, author=author, body=text, created_at=now)
        db.add(comment)
        db.add(
            TrackerEventModel(
                issue_key=key, actor=author, kind="comment", new_value=text[:200], created_at=now
            )
        )
        row.updated_at = now
        db.commit()
        db.refresh(comment)
        return {
            "id": comment.id,
            "issue_key": key,
            "author": comment.author,
            "body": comment.body,
            "created_at": _iso(comment.created_at),
        }


def delete_comment(key: str, comment_id: int) -> Dict[str, Any]:
    key = str(key or "").strip().lower()
    with SessionLocal() as db:
        row = db.get(TrackerCommentModel, int(comment_id))
        if row is None or row.issue_key != key:
            raise TrackerError("not-found", f"no comment {comment_id} on {key}")
        db.delete(row)
        db.commit()
        return {"id": int(comment_id), "deleted": True}


def add_link(
    from_key: str, *, to_key: str, kind: str, actor: Optional[str] = None
) -> Dict[str, Any]:
    """Relate two issues."""
    from_key = str(from_key or "").strip().lower()
    to_key = str(to_key or "").strip().lower()
    kind = _validate_choice(kind, LINK_KINDS, "link kind")
    if from_key == to_key:
        raise TrackerError("invalid", "an issue cannot link to itself")
    with SessionLocal() as db:
        for candidate in (from_key, to_key):
            if (
                db.query(TrackerIssueModel).filter(TrackerIssueModel.key == candidate).first()
                is None
            ):
                raise TrackerError("not-found", f"no such issue: {candidate}")
        existing = (
            db.query(TrackerLinkModel)
            .filter(
                TrackerLinkModel.from_key == from_key,
                TrackerLinkModel.to_key == to_key,
                TrackerLinkModel.kind == kind,
            )
            .first()
        )
        if existing is not None:
            return {
                "id": existing.id,
                "from_key": from_key,
                "to_key": to_key,
                "kind": kind,
                "created": False,
            }
        row = TrackerLinkModel(from_key=from_key, to_key=to_key, kind=kind)
        db.add(row)
        db.add(
            TrackerEventModel(
                issue_key=from_key, actor=actor, kind="link", field=kind, new_value=to_key
            )
        )
        db.commit()
        db.refresh(row)
        return {"id": row.id, "from_key": from_key, "to_key": to_key, "kind": kind, "created": True}


def remove_link(link_id: int, *, actor: Optional[str] = None) -> Dict[str, Any]:
    with SessionLocal() as db:
        row = db.get(TrackerLinkModel, int(link_id))
        if row is None:
            raise TrackerError("not-found", f"no such link: {link_id}")
        db.add(
            TrackerEventModel(
                issue_key=row.from_key,
                actor=actor,
                kind="unlink",
                field=row.kind,
                old_value=row.to_key,
            )
        )
        db.delete(row)
        db.commit()
        return {"id": int(link_id), "deleted": True}


def stats(project_id: Optional[str] = None, *, kind: Optional[str] = "issue") -> Dict[str, Any]:
    """Aggregate counts for a project (or the whole install).

    Default ``kind="issue"`` preserves legacy issue-only counts.
    ``kind=None`` or ``kind="all"`` aggregates across all kinds and also returns ``by_kind``.
    """
    with SessionLocal() as db:
        q = db.query(TrackerIssueModel)
        if kind is not None:
            if kind == "all":
                pass
            else:
                _validate_kind(kind)
                q = q.filter(TrackerIssueModel.kind == kind)
        else:
            # kind=None means all kinds (explicit generic surface)
            pass
        if project_id:
            q = q.filter(TrackerIssueModel.project_id == _validate_slug(project_id))
        rows = q.all()
    by_status: Dict[str, int] = {}
    by_severity: Dict[str, int] = {}
    by_component: Dict[str, int] = {}
    for row in rows:
        by_status[row.status] = by_status.get(row.status, 0) + 1
        by_severity[row.severity] = by_severity.get(row.severity, 0) + 1
        key = row.component or "(none)"
        by_component[key] = by_component.get(key, 0) + 1
    result: Dict[str, Any] = {
        "project_id": project_id,
        "total": len(rows),
        "open": sum(1 for r in rows if r.status not in TERMINAL_STATUSES),
        "by_status": by_status,
        "by_severity": by_severity,
        "by_component": by_component,
    }
    if kind is None or kind == "all":
        with SessionLocal() as db:
            base = db.query(TrackerIssueModel)
            if project_id:
                base = base.filter(TrackerIssueModel.project_id == _validate_slug(project_id))
            all_rows = base.all()
        by_kind: Dict[str, Dict[str, Any]] = {}
        for k in ITEM_KINDS:
            subset = [r for r in all_rows if getattr(r, "kind", "issue") == k]
            by_status_k: Dict[str, int] = {}
            by_sev_k: Dict[str, int] = {}
            for r in subset:
                by_status_k[r.status] = by_status_k.get(r.status, 0) + 1
                by_sev_k[r.severity] = by_sev_k.get(r.severity, 0) + 1
            by_kind[k] = {
                "total": len(subset),
                "open": sum(1 for r in subset if r.status not in TERMINAL_STATUSES),
                "by_status": by_status_k,
                "by_severity": by_sev_k,
            }
        result["by_kind"] = by_kind
        result["all_total"] = len(all_rows)
        result["all_open"] = sum(1 for r in all_rows if r.status not in TERMINAL_STATUSES)
    return result


def render_markdown(
    project_id: str, *, open_only: bool = True, kind: Optional[str] = "issue"
) -> str:
    """Render an issue log as markdown.

    The markdown ledger this replaces is now an *export*: a view produced from
    the database on demand, not a second source of truth that has to be kept in
    step with it.
    """
    project = get_project(project_id)

    # Paged rather than capped. `list_issues` bounds a page at 500, and this
    # export replaces a file that held every entry — a truncated log that
    # announces nothing is the exact failure the markdown ledger never had.
    issues: List[Dict[str, Any]] = []
    total = 0
    while True:
        page = list_issues(
            project_id=project_id,
            open_only=open_only,
            limit=500,
            offset=len(issues),
            order="created_asc",
            kind=kind,
        )
        total = page["total"]
        issues.extend(page["issues"])
        if not page["issues"] or len(issues) >= total:
            break

    lines = [
        f"# {'Open' if open_only else 'All'} issues — {project['name']}",
        "",
        f"Rendered from the CAO issue tracker. Project id `{project['id']}`; " f"{total} issue(s).",
        "",
        "---",
        "",
    ]
    for issue in issues:
        severity = f"[{issue['severity']}] " if issue["severity"] != "unset" else ""
        kind_prefix = f"[{issue.get('kind', 'issue')}] " if kind is None or kind == "all" else ""
        lines.append(f"## {issue['key']} — {kind_prefix}{severity}{issue['title']}")
        lines.append("")
        if kind is None or kind == "all":
            lines.append(f"- **kind:** {issue.get('kind', 'issue')}")
        lines.append(f"- **filed:** {issue['created_at']}")
        lines.append(f"- **reporter:** {issue['reporter'] or 'unknown'}")
        lines.append(f"- **status:** {issue['status']}")
        if issue["component"]:
            lines.append(f"- **component:** {issue['component']}")
        if issue["assignee"]:
            lines.append(f"- **assignee:** {issue['assignee']}")
        if issue["labels"]:
            lines.append(f"- **labels:** {', '.join(issue['labels'])}")
        if issue["failing_command"]:
            lines.append(f"- **failing command:** `{issue['failing_command']}`")
        if issue["evidence"]:
            lines.append(f"- **evidence:** {issue['evidence']}")
        if issue["resolution"]:
            lines.append(f"- **resolution:** {issue['resolution']}")
        lines.append("")
        if issue["body"]:
            lines.append(issue["body"].rstrip())
            lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)
