"""MCP server models."""

from typing import Optional

from pydantic import BaseModel, Field


class HandoffResult(BaseModel):
    """Result of a handoff operation.

    When the MCP transport times out before the step completes (issue #447),
    ``pending=True`` and ``job_id`` identify the in-flight job.  The caller
    can retrieve the final result later via ``GET /handoff-results/{job_id}``.
    """

    success: bool = Field(description="Whether the handoff was successful")
    message: str = Field(description="A message describing the result of the handoff")
    output: Optional[str] = Field(None, description="The output from the target agent")
    terminal_id: Optional[str] = Field(None, description="The terminal ID used for the handoff")
    # Async-retrieval fields (issue #447). Present only when the transport timed
    # out before the result arrived — absent (None) on the normal synchronous path.
    job_id: Optional[str] = Field(
        None,
        description=(
            "Opaque identifier for this handoff job. Present when pending=True. "
            "Use with GET /handoff-results/{job_id} to retrieve the result later."
        ),
    )
    pending: Optional[bool] = Field(
        None,
        description=(
            "True when the transport timed out before the result was delivered. "
            "The job is still running (or completed) server-side; poll "
            "GET /handoff-results/{job_id} until state='completed'."
        ),
    )
