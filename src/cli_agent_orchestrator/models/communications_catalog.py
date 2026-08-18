"""Wire models for the bounded communications catalog API (design §7).

The fork does not own the vocabulary of kinds, scopes, actors, or delivery
states, so every field that the conductor authors is an opaque carrier.  The
only enumerations here are the envelope bookkeeping the fork is responsible
for: coverage values, reason codes, and byte/count bounds.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class QuarantineInfo(BaseModel):
    """A tombstone for content that has been quarantined upstream."""

    reason: str
    actor: Optional[str] = None
    quarantined_at: Optional[str] = None
    receipt_sha256: Optional[str] = None


class DocumentEntry(BaseModel):
    """One attachment or body document exactly as the publisher wrote it."""

    model_config = ConfigDict(extra="allow")

    attachment_id: str
    document_id: str
    role: str
    display_name: str
    media_type: str
    sha256: str
    byte_size: int
    blob_id: str
    content_state: str
    capture_kind: Optional[str] = None
    redaction_applied: Optional[bool] = None
    provenance: Optional[Dict[str, Any]] = None
    quarantine: Optional[QuarantineInfo] = None


class CommunicationListItem(BaseModel):
    """Metadata for one communication; the list endpoint never carries bodies."""

    model_config = ConfigDict(extra="allow")

    communication_id: str
    project_id: str
    session_id: Optional[str] = None
    lane_id: Optional[str] = None
    task_occurrence_id: Optional[str] = None
    goal_version: Optional[str] = None
    kind: Optional[str] = None
    report_scope: Optional[str] = None
    authored_by_type: Optional[str] = None
    authored_by_id: Optional[str] = None
    authored_at: Optional[str] = None
    recorded_at: Optional[str] = None
    title: Optional[str] = None
    delivery_state: Optional[str] = None
    visibility: Optional[str] = None
    request_key: Optional[str] = None
    supersedes_communication_id: Optional[str] = None
    superseded_by: Optional[str] = None
    body: Optional[DocumentEntry] = None
    documents: List[DocumentEntry] = []


class CatalogReason(BaseModel):
    """Why one project contributed nothing or less than it holds."""

    source: str
    reason: str


class CommunicationsListResponse(BaseModel):
    """One page of the catalog, bounded and coverage-typed."""

    model_config = ConfigDict(populate_by_name=True)

    catalog_schema: str = Field(alias="schema")
    coverage: str
    reasons: List[CatalogReason] = []
    communications: List[CommunicationListItem] = []
    next_cursor: Optional[str] = None
    total: int = 0


class CommunicationDetailResponse(BaseModel):
    """One communication with its body content, when the publisher says it is present."""

    communication: CommunicationListItem
    content: Optional[str] = None
    reason: Optional[str] = None


class AttachmentDetailResponse(BaseModel):
    """One attachment with its content, when the publisher says it is present."""

    document: DocumentEntry
    content: Optional[str] = None
    reason: Optional[str] = None
