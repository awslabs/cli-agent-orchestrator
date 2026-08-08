"""HTTP surface for project-scoped issue tracking.

Kept out of ``api/main.py`` on purpose: that module is already 5,500 lines,
and a tracker that has to be read alongside the managed-launch state machine
to be understood is a tracker nobody will extend.

Every route carries the same scope dependency as the rest of the API. A read
route accepts read/write/admin; a mutating route demands write or admin. The
scope check is a no-op while auth is disabled (the default localhost posture),
which is exactly why it must be present now rather than added the day auth is
turned on.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from cli_agent_orchestrator.security.auth import (
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    require_any_scope,
)
from cli_agent_orchestrator.services import issue_tracker as tracker

logger = logging.getLogger(__name__)

router = APIRouter(tags=["issue-tracker"])

_READ = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN))
_WRITE = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN))

# A refusal the service classified keeps that classification at the HTTP
# boundary. Collapsing them all to 400 would make "you spelled the status
# wrong" and "that project is gone" indistinguishable to a CLI client that can
# only branch on the status code.
_STATUS_FOR_CODE = {
    "invalid": status.HTTP_400_BAD_REQUEST,
    "not-found": status.HTTP_404_NOT_FOUND,
    "conflict": status.HTTP_409_CONFLICT,
    "unresolved": status.HTTP_422_UNPROCESSABLE_ENTITY,
}


def _http(exc: tracker.TrackerError) -> HTTPException:
    return HTTPException(
        status_code=_STATUS_FOR_CODE.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=exc.message,
    )


# --------------------------------------------------------------------------
# request bodies
# --------------------------------------------------------------------------


class StrictBody(BaseModel):
    """Reject unknown fields instead of ignoring them.

    Pydantic's default is to drop what it does not recognise, which turns a
    misspelled or non-editable field into a silent no-op answered with 200 —
    `PATCH {"project_id": "other"}` looked like it moved an issue between
    projects and did nothing at all. A 422 naming the field is the only answer
    a client can act on.
    """

    model_config = ConfigDict(extra="forbid")


class ScopeBody(StrictBody):
    kind: str
    value: str


class ProjectCreateBody(StrictBody):
    name: str
    id: Optional[str] = None
    description: str = ""
    issue_prefix: Optional[str] = None
    scopes: List[ScopeBody] = Field(default_factory=list)


class ProjectUpdateBody(StrictBody):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    issue_prefix: Optional[str] = None


class IssueCreateBody(StrictBody):
    title: str
    project_id: Optional[str] = None
    body: str = ""
    status: str = "open"
    severity: str = "unset"
    component: Optional[str] = None
    reporter: Optional[str] = None
    assignee: Optional[str] = None
    labels: List[str] = Field(default_factory=list)
    failing_command: Optional[str] = None
    evidence: Optional[str] = None
    session_name: Optional[str] = None
    terminal_id: Optional[str] = None
    source_path: Optional[str] = None
    cwd: Optional[str] = None
    alias: Optional[str] = None
    key: Optional[str] = None
    origin: str = "api"


class IssueUpdateBody(StrictBody):
    """Every field optional; only the ones present are applied.

    ``model_fields_set`` distinguishes "not sent" from "sent as null", which is
    what lets the dashboard clear an assignee (send ``""``) without every other
    unsent field being wiped.
    """

    title: Optional[str] = None
    body: Optional[str] = None
    status: Optional[str] = None
    severity: Optional[str] = None
    component: Optional[str] = None
    reporter: Optional[str] = None
    assignee: Optional[str] = None
    labels: Optional[List[str]] = None
    failing_command: Optional[str] = None
    evidence: Optional[str] = None
    resolution: Optional[str] = None
    duplicate_of: Optional[str] = None
    kind: Optional[str] = None
    actor: Optional[str] = None


class FeatureCreateBody(StrictBody):
    title: str
    project_id: Optional[str] = None
    body: str = ""
    status: str = "open"
    severity: str = "unset"
    component: Optional[str] = None
    reporter: Optional[str] = None
    assignee: Optional[str] = None
    labels: List[str] = Field(default_factory=list)
    evidence: Optional[str] = None
    session_name: Optional[str] = None
    terminal_id: Optional[str] = None
    source_path: Optional[str] = None
    cwd: Optional[str] = None
    alias: Optional[str] = None
    key: Optional[str] = None
    origin: str = "api"


class FeatureUpdateBody(StrictBody):
    title: Optional[str] = None
    body: Optional[str] = None
    status: Optional[str] = None
    severity: Optional[str] = None
    component: Optional[str] = None
    reporter: Optional[str] = None
    assignee: Optional[str] = None
    labels: Optional[List[str]] = None
    evidence: Optional[str] = None
    resolution: Optional[str] = None
    duplicate_of: Optional[str] = None
    kind: Optional[str] = None
    actor: Optional[str] = None


class CommentBody(StrictBody):
    body: str
    author: Optional[str] = None


class LinkBody(StrictBody):
    to_key: str
    kind: str
    actor: Optional[str] = None


# --------------------------------------------------------------------------
# projects
# --------------------------------------------------------------------------


@router.get("/tracker/vocabulary")
async def tracker_vocabulary(_scopes: List[str] = _READ) -> Dict[str, Any]:
    """The enumerations the server will accept.

    Served rather than duplicated in the dashboard so a status added here can
    never disagree with the dropdown that offers it.
    """
    return {
        "statuses": list(tracker.STATUSES),
        "terminal_statuses": sorted(tracker.TERMINAL_STATUSES),
        "statuses_by_kind": {"issue": list(tracker.STATUSES), "feature": list(tracker.STATUSES)},
        "terminal_statuses_by_kind": {
            "issue": sorted(tracker.TERMINAL_STATUSES),
            "feature": sorted(tracker.TERMINAL_STATUSES),
        },
        "item_kinds": list(tracker.ITEM_KINDS),
        "severities": list(tracker.SEVERITIES),
        "scope_kinds": list(tracker.SCOPE_KINDS),
        "link_kinds": list(tracker.LINK_KINDS),
        "project_statuses": list(tracker.PROJECT_STATUSES),
    }


@router.get("/tracker/projects")
async def list_projects(
    include_archived: bool = Query(False),
    _scopes: List[str] = _READ,
) -> List[Dict[str, Any]]:
    return tracker.list_projects(include_archived=include_archived)


@router.post("/tracker/projects", status_code=status.HTTP_201_CREATED)
async def create_project(
    body: ProjectCreateBody,
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    try:
        return tracker.create_project(
            name=body.name,
            project_id=body.id,
            description=body.description,
            issue_prefix=body.issue_prefix,
            scopes=[s.model_dump() for s in body.scopes],
        )
    except tracker.TrackerError as exc:
        raise _http(exc) from exc


@router.get("/tracker/projects/resolve")
async def resolve_project(
    project: Optional[str] = Query(None),
    session: Optional[str] = Query(None),
    alias: Optional[str] = Query(None, description="a project_id-kind scope value"),
    cwd: Optional[str] = Query(None),
    git_remote: Optional[str] = Query(None),
    _scopes: List[str] = _READ,
) -> Dict[str, Any]:
    """Answer "which project would an issue filed here belong to?".

    Declared before ``/tracker/projects/{project_id}`` so the literal path wins
    over the parameterised one; ``resolve`` is a legal slug and would otherwise
    be swallowed as a project id.
    """
    try:
        return tracker.resolve_project(
            project=project, session=session, alias=alias, cwd=cwd, git_remote=git_remote
        ).as_dict()
    except tracker.TrackerError as exc:
        raise _http(exc) from exc


@router.get("/tracker/projects/{project_id}")
async def get_project(project_id: str, _scopes: List[str] = _READ) -> Dict[str, Any]:
    try:
        return tracker.get_project(project_id)
    except tracker.TrackerError as exc:
        raise _http(exc) from exc


@router.patch("/tracker/projects/{project_id}")
async def update_project(
    project_id: str,
    body: ProjectUpdateBody,
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    try:
        return tracker.update_project(
            project_id,
            name=body.name,
            description=body.description,
            status=body.status,
            issue_prefix=body.issue_prefix,
        )
    except tracker.TrackerError as exc:
        raise _http(exc) from exc


@router.delete("/tracker/projects/{project_id}")
async def delete_project(
    project_id: str,
    force: bool = Query(False, description="also delete the project's issues"),
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    try:
        return tracker.delete_project(project_id, force=force)
    except tracker.TrackerError as exc:
        raise _http(exc) from exc


@router.get("/tracker/projects/{project_id}/export")
async def export_project_markdown(
    project_id: str,
    open_only: bool = Query(True),
    kind: Optional[str] = Query(None),
    _scopes: List[str] = _READ,
) -> Response:
    """Render the issue log as markdown.

    The ledger files this replaces become a view produced on demand, not a
    second source of truth somebody has to keep in step.
    """
    effective_kind = kind if kind is not None else "issue"
    try:
        text = tracker.render_markdown(project_id, open_only=open_only, kind=effective_kind)
    except tracker.TrackerError as exc:
        raise _http(exc) from exc
    return Response(content=text, media_type="text/markdown; charset=utf-8")


@router.post("/tracker/projects/{project_id}/scopes", status_code=status.HTTP_201_CREATED)
async def add_scope(
    project_id: str,
    body: ScopeBody,
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    try:
        return tracker.add_scope(project_id, kind=body.kind, value=body.value)
    except tracker.TrackerError as exc:
        raise _http(exc) from exc


@router.delete("/tracker/projects/{project_id}/scopes/{scope_id}")
async def remove_scope(
    project_id: str,
    scope_id: int,
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    try:
        return tracker.remove_scope(project_id, scope_id)
    except tracker.TrackerError as exc:
        raise _http(exc) from exc


# --------------------------------------------------------------------------
# issues
# --------------------------------------------------------------------------


@router.get("/tracker/issues")
async def list_issues(
    project_id: Optional[str] = Query(None),
    status_filter: Optional[List[str]] = Query(None, alias="status"),
    severity: Optional[List[str]] = Query(None),
    component: Optional[str] = Query(None),
    assignee: Optional[str] = Query(None),
    reporter: Optional[str] = Query(None),
    label: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    open_only: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    order: str = Query("created_desc"),
    kind: Optional[str] = Query(None, description="issue|feature|all"),
    _scopes: List[str] = _READ,
) -> Dict[str, Any]:
    # Default is issue-only for backward compatibility
    effective_kind = kind if kind is not None else "issue"
    try:
        return tracker.list_issues(
            project_id=project_id,
            status=status_filter,
            severity=severity,
            component=component,
            assignee=assignee,
            reporter=reporter,
            label=label,
            query=q,
            open_only=open_only,
            limit=limit,
            offset=offset,
            order=order,
            kind=effective_kind,
        )
    except tracker.TrackerError as exc:
        raise _http(exc) from exc


@router.get("/tracker/issues/stats")
async def issue_stats(
    project_id: Optional[str] = Query(None),
    _scopes: List[str] = _READ,
) -> Dict[str, Any]:
    try:
        return tracker.stats(project_id)
    except tracker.TrackerError as exc:
        raise _http(exc) from exc


@router.post("/tracker/issues", status_code=status.HTTP_201_CREATED)
async def create_issue(
    body: IssueCreateBody,
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    try:
        return tracker.create_issue(
            project_id=body.project_id,
            title=body.title,
            body=body.body,
            status=body.status,
            severity=body.severity,
            component=body.component,
            reporter=body.reporter,
            assignee=body.assignee,
            labels=body.labels,
            failing_command=body.failing_command,
            evidence=body.evidence,
            session_name=body.session_name,
            terminal_id=body.terminal_id,
            source_path=body.source_path,
            cwd=body.cwd,
            alias=body.alias,
            key=body.key,
            origin=body.origin,
        )
    except tracker.TrackerError as exc:
        raise _http(exc) from exc


@router.get("/tracker/issues/{issue_key}")
async def get_issue(issue_key: str, _scopes: List[str] = _READ) -> Dict[str, Any]:
    try:
        return tracker.get_issue(issue_key)
    except tracker.TrackerError as exc:
        raise _http(exc) from exc


@router.patch("/tracker/issues/{issue_key}")
async def update_issue(
    issue_key: str,
    body: IssueUpdateBody,
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    changes = {
        name: value
        for name, value in body.model_dump().items()
        if name != "actor" and name in body.model_fields_set
    }
    try:
        existing = tracker.get_issue(issue_key)
        _assert_issue(existing)
        # Duplicate status requires canonical key validation (P1)
        if (
            changes.get("status") == "duplicate"
            and not changes.get("duplicate_of")
            and not existing.get("duplicate_of")
        ):
            raise tracker.TrackerError(
                "invalid", "duplicate status requires duplicate_of canonical key"
            )
        return tracker.update_issue(issue_key, actor=body.actor, **changes)
    except tracker.TrackerError as exc:
        raise _http(exc) from exc


@router.delete("/tracker/issues/{issue_key}")
async def delete_issue(issue_key: str, _scopes: List[str] = _WRITE) -> Dict[str, Any]:
    try:
        existing = tracker.get_issue(issue_key)
        _assert_issue(existing)
        return tracker.delete_issue(issue_key)
    except tracker.TrackerError as exc:
        raise _http(exc) from exc


@router.post("/tracker/issues/{issue_key}/comments", status_code=status.HTTP_201_CREATED)
async def add_comment(
    issue_key: str,
    body: CommentBody,
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    try:
        return tracker.add_comment(issue_key, body=body.body, author=body.author)
    except tracker.TrackerError as exc:
        raise _http(exc) from exc


@router.delete("/tracker/issues/{issue_key}/comments/{comment_id}")
async def delete_comment(
    issue_key: str,
    comment_id: int,
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    try:
        return tracker.delete_comment(issue_key, comment_id)
    except tracker.TrackerError as exc:
        raise _http(exc) from exc


@router.post("/tracker/issues/{issue_key}/links", status_code=status.HTTP_201_CREATED)
async def add_link(
    issue_key: str,
    body: LinkBody,
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    try:
        return tracker.add_link(issue_key, to_key=body.to_key, kind=body.kind, actor=body.actor)
    except tracker.TrackerError as exc:
        raise _http(exc) from exc


@router.delete("/tracker/issues/{issue_key}/links/{link_id}")
async def remove_link(
    issue_key: str,
    link_id: int,
    actor: Optional[str] = Query(None),
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    try:
        return tracker.remove_link(link_id, actor=actor)
    except tracker.TrackerError as exc:
        raise _http(exc) from exc


# --------------------------------------------------------------------------
# features — typed aliases over shared storage (D4)
# --------------------------------------------------------------------------


def _assert_feature(row: dict) -> None:
    if row.get("kind") != "feature":
        raise tracker.TrackerError("not-found", f"no such feature: {row.get('key')}")


def _assert_issue(row: dict) -> None:
    if row.get("kind") != "issue":
        raise tracker.TrackerError(
            "not-found", f"no such issue: {row.get('key')} (kind={row.get('kind')})"
        )


@router.get("/tracker/features")
async def list_features(
    project_id: Optional[str] = Query(None),
    status_filter: Optional[List[str]] = Query(None, alias="status"),
    severity: Optional[List[str]] = Query(None),
    component: Optional[str] = Query(None),
    assignee: Optional[str] = Query(None),
    reporter: Optional[str] = Query(None),
    label: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    open_only: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    order: str = Query("created_desc"),
    _scopes: List[str] = _READ,
) -> Dict[str, Any]:
    try:
        return tracker.list_features(
            project_id=project_id,
            status=status_filter,
            severity=severity,
            component=component,
            assignee=assignee,
            reporter=reporter,
            label=label,
            query=q,
            open_only=open_only,
            limit=limit,
            offset=offset,
            order=order,
        )
    except tracker.TrackerError as exc:
        raise _http(exc) from exc


@router.get("/tracker/features/stats")
async def feature_stats(
    project_id: Optional[str] = Query(None),
    _scopes: List[str] = _READ,
) -> Dict[str, Any]:
    try:
        return tracker.stats(project_id, kind="feature")
    except tracker.TrackerError as exc:
        raise _http(exc) from exc


@router.post("/tracker/features", status_code=201)
async def create_feature(
    body: FeatureCreateBody,
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    try:
        return tracker.create_feature(
            project_id=body.project_id,
            title=body.title,
            body=body.body,
            status=body.status,
            severity=body.severity,
            component=body.component,
            reporter=body.reporter,
            assignee=body.assignee,
            labels=body.labels,
            evidence=body.evidence,
            session_name=body.session_name,
            terminal_id=body.terminal_id,
            source_path=body.source_path,
            cwd=body.cwd,
            alias=body.alias,
            key=body.key,
            origin=body.origin,
        )
    except tracker.TrackerError as exc:
        raise _http(exc) from exc


@router.get("/tracker/features/{feature_key}")
async def get_feature(feature_key: str, _scopes: List[str] = _READ) -> Dict[str, Any]:
    try:
        row = tracker.get_issue(feature_key)
        _assert_feature(row)
        return row
    except tracker.TrackerError as exc:
        raise _http(exc) from exc


@router.patch("/tracker/features/{feature_key}")
async def update_feature(
    feature_key: str,
    body: FeatureUpdateBody,
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    try:
        existing = tracker.get_issue(feature_key)
        _assert_feature(existing)
        changes = {
            name: value
            for name, value in body.model_dump().items()
            if name != "actor" and name in body.model_fields_set
        }
        return tracker.update_issue(feature_key, actor=body.actor, **changes)
    except tracker.TrackerError as exc:
        raise _http(exc) from exc


@router.delete("/tracker/features/{feature_key}")
async def delete_feature(feature_key: str, _scopes: List[str] = _WRITE) -> Dict[str, Any]:
    try:
        existing = tracker.get_issue(feature_key)
        _assert_feature(existing)
        return tracker.delete_issue(feature_key)
    except tracker.TrackerError as exc:
        raise _http(exc) from exc


@router.post("/tracker/features/{feature_key}/comments", status_code=201)
async def add_feature_comment(
    feature_key: str,
    body: CommentBody,
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    try:
        existing = tracker.get_issue(feature_key)
        _assert_feature(existing)
        return tracker.add_comment(feature_key, body=body.body, author=body.author)
    except tracker.TrackerError as exc:
        raise _http(exc) from exc


@router.delete("/tracker/features/{feature_key}/comments/{comment_id}")
async def delete_feature_comment(
    feature_key: str,
    comment_id: int,
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    try:
        existing = tracker.get_issue(feature_key)
        _assert_feature(existing)
        return tracker.delete_comment(feature_key, comment_id)
    except tracker.TrackerError as exc:
        raise _http(exc) from exc


@router.post("/tracker/features/{feature_key}/links", status_code=201)
async def add_feature_link(
    feature_key: str,
    body: LinkBody,
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    try:
        existing = tracker.get_issue(feature_key)
        _assert_feature(existing)
        return tracker.add_link(feature_key, to_key=body.to_key, kind=body.kind, actor=body.actor)
    except tracker.TrackerError as exc:
        raise _http(exc) from exc


@router.delete("/tracker/features/{feature_key}/links/{link_id}")
async def remove_feature_link(
    feature_key: str,
    link_id: int,
    actor: Optional[str] = Query(None),
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    try:
        existing = tracker.get_issue(feature_key)
        _assert_feature(existing)
        # Verify link belongs to this feature (URL key must match link's from_key/to_key)
        from cli_agent_orchestrator.clients.database import SessionLocal, TrackerLinkModel

        with SessionLocal() as db:
            link = db.get(TrackerLinkModel, int(link_id))
            if link is None or (
                link.from_key != feature_key.lower() and link.to_key != feature_key.lower()
            ):
                raise tracker.TrackerError(
                    "not-found", f"no link {link_id} on feature {feature_key}"
                )
        return tracker.remove_link(link_id, actor=actor)
    except tracker.TrackerError as exc:
        raise _http(exc) from exc


@router.get("/tracker/projects/{project_id}/features/export")
async def export_project_features_markdown(
    project_id: str,
    open_only: bool = Query(True),
    _scopes: List[str] = _READ,
) -> Response:
    try:
        text = tracker.render_markdown(project_id, open_only=open_only, kind="feature")
    except tracker.TrackerError as exc:
        raise _http(exc) from exc
    return Response(content=text, media_type="text/markdown; charset=utf-8")
