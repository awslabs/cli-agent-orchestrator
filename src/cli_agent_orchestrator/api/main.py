"""Single FastAPI entry point for all HTTP routes."""

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import pty
import re
import signal
import struct
import subprocess
import termios
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Dict, List, Optional, cast

from fastapi import (
    BackgroundTasks,
    Body,
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from cli_agent_orchestrator.api.attachments import router as native_attachments_router
from cli_agent_orchestrator.api.roster import router as roster_router
from cli_agent_orchestrator.api.session_lifecycle import router as session_lifecycle_router
from cli_agent_orchestrator.api.tracker import router as tracker_router
from cli_agent_orchestrator.backends import TerminalBackendError, TerminalNotFoundError
from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend
from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import (
    create_inbox_message,
    get_inbox_messages,
    get_terminal_metadata,
    get_terminal_metadata_v2,
    init_db,
)
from cli_agent_orchestrator.constants import (
    ALLOWED_HOSTS,
    API_BASE_URL,
    CAO_HOME_DIR,
    CORS_ORIGINS,
    DEFAULT_PROVIDER,
    INBOX_POLLING_INTERVAL,
    INBOX_RECONCILE_INTERVAL,
    OTEL_SERVICE_NAME,
    SERVER_HOST,
    SERVER_PORT,
    SERVER_VERSION,
    TERMINALS_RUN_STEP_ROUTE,
    TRUSTED_FORWARDER_IPS,
    WORKFLOW_ENV_ALLOWLIST,
    WORKFLOW_ENV_VALUE_MAX_LEN,
    WS_ALLOWED_CLIENTS,
    add_local_cors_origins,
)
from cli_agent_orchestrator.ext_apps import mount_widget_static
from cli_agent_orchestrator.graph.providers import get_provider

# Import the sinks package for its import-time @register_sink side effects
# ("okf", "obsidian", "graphml"); get_sink resolves by name from the registry.
from cli_agent_orchestrator.graph.sinks import get_sink
from cli_agent_orchestrator.models.annotations import AnnotationsResponse
from cli_agent_orchestrator.models.flow import Flow
from cli_agent_orchestrator.models.inbox import (
    CallbackRecoveryCallbackRequest,
    CallbackRecoveryCompletionRequest,
    CallbackRecoveryDispositionRequest,
    CallbackRecoveryRequest,
    CallbackRecoveryResolutionRequest,
    MessageStatus,
    OrchestrationType,
)
from cli_agent_orchestrator.models.managed_launch import (
    PROTOCOL_VERSION as MANAGED_LAUNCH_PROTOCOL_VERSION,
)
from cli_agent_orchestrator.models.managed_launch import (
    ManagedLaunchAdmitRequest,
    ManagedLaunchCleanupRequest,
    ManagedLaunchObservationRequest,
    ManagedLaunchReserveRequest,
    ManagedLaunchRouteAttestRequest,
)
from cli_agent_orchestrator.models.managed_launch_v2 import (
    ManagedDestructiveRequest,
    ManagedLaunchV2AdmitRequest,
    ManagedLaunchV2BindRequest,
    ManagedLaunchV2CleanupRequest,
    ManagedLaunchV2NegativeRequest,
    ManagedLaunchV2ReserveRequest,
    ManagedV2FenceInstallRequest,
    ManagedV2ParkRequest,
)
from cli_agent_orchestrator.models.memory import (
    MemoryKey,
    MemoryScope,
    MemoryScopeId,
    MemoryType,
)
from cli_agent_orchestrator.models.terminal import Terminal, TerminalId
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.security.auth import (
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    SCOPES_SUPPORTED,
    extract_scopes_from_token,
    get_authorization_servers,
    get_current_scopes,
    is_auth_enabled,
    require_any_scope,
)
from cli_agent_orchestrator.services import (
    annotations,
    callback_recovery,
    companion_receipts,
    control_input_service,
    flow_service,
    image_attachments,
    macro_notation,
    managed_launch,
    managed_launch_v2,
    model_turn_receipt_contract,
    native_attachment_recovery,
    operator_message_service,
    recovery_capabilities,
    secret_gate,
    session_env,
    session_service,
    terminal_projection,
    terminal_service,
    wake_receipts,
)
from cli_agent_orchestrator.services.agent_step import StepExecutionError, run_agent_step
from cli_agent_orchestrator.services.cleanup_service import (
    cleanup_expired_memories,
    cleanup_old_data,
)
from cli_agent_orchestrator.services.config_service import ConfigService
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.services.event_log_service import RING_CAPACITY
from cli_agent_orchestrator.services.event_primitives import KINDS as EVENT_KINDS
from cli_agent_orchestrator.services.fifo_reader import fifo_manager
from cli_agent_orchestrator.services.herdr_inbox_registry import set_herdr_inbox_service
from cli_agent_orchestrator.services.herdr_inbox_service import HerdrInboxService
from cli_agent_orchestrator.services.inbox_service import inbox_service
from cli_agent_orchestrator.services.install_service import InstallResult, install_agent
from cli_agent_orchestrator.services.log_writer import log_writer
from cli_agent_orchestrator.services.status_monitor import status_monitor
from cli_agent_orchestrator.services.step_output_store import _validate_key_part
from cli_agent_orchestrator.services.terminal_service import (
    OutputMode,
    TerminalGenerationMismatchError,
    TerminalInputBlockedError,
)
from cli_agent_orchestrator.telemetry import init_telemetry, shutdown_telemetry
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile, resolve_provider
from cli_agent_orchestrator.utils.logging import install_access_log_redaction, setup_logging
from cli_agent_orchestrator.utils.skills import (
    SkillNameError,
    load_skill_content,
    validate_skill_name,
)
from cli_agent_orchestrator.utils.terminal import validate_tmux_name

logger = logging.getLogger(__name__)

TMUX_KEY_PATTERN = re.compile(
    r"^(?:Up|Down|Left|Right|Enter|Tab|Escape|Space|[A-Za-z0-9]|[CMS]-[A-Za-z0-9])$"
)


async def flow_daemon():
    """Background task to check and execute flows."""
    logger.info("Flow daemon started")
    while True:
        try:
            flows = flow_service.get_flows_to_run()
            for flow in flows:
                try:
                    executed = await flow_service.execute_flow(flow.name)
                    if executed:
                        logger.info(f"Flow '{flow.name}' executed successfully")
                    else:
                        logger.info(f"Flow '{flow.name}' skipped (execute=false)")
                except Exception as e:
                    logger.error(f"Flow '{flow.name}' failed: {e}")
        except Exception as e:
            logger.error(f"Flow daemon error: {e}")

        await asyncio.sleep(60)


async def opencode_inbox_delivery_daemon(registry: PluginRegistry) -> None:
    """Background task to wake OpenCode inbox delivery for pending messages."""
    logger.info("OpenCode inbox delivery poller started")
    while True:
        await asyncio.sleep(INBOX_POLLING_INTERVAL)
        try:
            await asyncio.to_thread(inbox_service.poll_opencode_pending_messages, registry)
        except Exception:
            logger.exception("OpenCode inbox delivery poller error")


async def inbox_reconciliation_daemon(registry: PluginRegistry) -> None:
    """Background task that recovers inbox messages the fast paths missed.

    Safety net for issue #131: the immediate (on POST) delivery path and the
    event-driven StatusMonitor pipeline can both miss a message when the receiver
    is already idle, leaving it PENDING forever. This sweep runs on a slower
    interval and re-attempts delivery for anything left pending past the grace
    window.
    """
    logger.info("Inbox reconciliation daemon started")
    while True:
        await asyncio.sleep(INBOX_RECONCILE_INTERVAL)
        try:
            await asyncio.to_thread(inbox_service.reconcile_orphaned_messages, registry)
        except Exception:
            logger.exception("Inbox reconciliation daemon error")


# Response Models
class TerminalOutputResponse(BaseModel):
    output: str
    mode: str


class CreateTerminalBody(BaseModel):
    """Optional JSON body for POST /sessions/{name}/terminals.

    Carries the deferred-init message payload OUT of the query string:
    prompt content can be large (URL-length 414 risk) and sensitive (query
    strings are routinely captured in HTTP access logs and traces). Routing
    fields (provider, defer_init, etc.) stay as query params; only the
    message content lives here.
    """

    initial_message: Optional[str] = None
    initial_message_orchestration_type: Optional[str] = None
    expected_model: Optional[str] = None
    expected_effort: Optional[str] = None


class RunStepRequest(BaseModel):
    """Request body for the combined step-execution endpoint (N0, #312)."""

    provider: str = Field(description="Provider type (e.g. 'kiro_cli', 'claude_code')")
    agent: str = Field(description="Agent profile name")
    prompt: str = Field(description="Prompt to send (caller applies any prompt shaping first)")
    session_name: Optional[str] = Field(
        default=None,
        description="Existing session to create the terminal in; auto-generated if None",
    )
    reuse_terminal_id: Optional[str] = Field(
        default=None, description="Reuse an existing terminal (skips create + teardown)"
    )
    teardown: bool = Field(
        default=True,
        description="Delete the created terminal after the step (ignored when reusing)",
    )
    timeout: float = Field(default=600.0, description="Max seconds to wait for completion", gt=0)
    working_directory: Optional[str] = Field(
        default=None, description="Working directory for a freshly created terminal"
    )
    caller_id: Optional[str] = Field(
        default=None,
        description="Supervisor terminal ID to record for structural callback routing (#284)",
    )
    allowed_tools: Optional[list[str]] = Field(
        default=None,
        description="Resolved allowed-tools list for a freshly created terminal (handoff inheritance)",
    )
    env_vars: Optional[Dict[str, str]] = Field(
        default=None,
        description=(
            "Workflow identity env vars injected into a freshly created terminal. "
            "Keys are restricted to the WORKFLOW_ENV_ALLOWLIST (NFR-SEC-4); "
            "values are validated but never echoed in error bodies."
        ),
    )

    @field_validator("env_vars")
    @classmethod
    def validate_env_vars(cls, v: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
        """Per-key checks for the env-var injection surface (U2/C6, A2).

        Check order is load-bearing (security-requirements.md): allowlist ->
        length cap -> control chars -> shared validator. Error messages name
        the KEY and the violated rule only — the supplied VALUE is never
        echoed into a 422 body (NFR-SEC-2 extended to the error path).
        """
        if v is None:
            return v
        for key, value in v.items():
            if key not in WORKFLOW_ENV_ALLOWLIST:
                raise ValueError(
                    f"env var key '{key}' not in allowlist "
                    f"{{{', '.join(sorted(WORKFLOW_ENV_ALLOWLIST))}}}"
                )
            # Pre-regex defense-in-depth, NOT redundancy: bounds the input
            # O(1) before any regex evaluation and bounds what can be staged
            # into a terminal environment regardless of future regex changes.
            # Do not simplify away as duplicate validation (the effective
            # accepted length is 64 via WORKFLOW_NAME_RE downstream).
            if len(value) > WORKFLOW_ENV_VALUE_MAX_LEN:
                raise ValueError(
                    f"value for '{key}' exceeds the {WORKFLOW_ENV_VALUE_MAX_LEN}-char cap"
                )
            # Values land in a tmux session environment — escape-sequence
            # injection into a terminal is the concrete threat.
            if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
                raise ValueError(f"value for '{key}' contains control characters")
            try:
                _validate_key_part(value, key)
            except ValueError:
                # The shared validator's message interpolates the VALUE;
                # re-raise with a key-name-only message so the supplied value
                # never round-trips into the 422 body (NFR-SEC-4 sanitized
                # error rule). `from None` drops the value-bearing cause.
                raise ValueError(
                    f"value for '{key}' is invalid (must be a 1-64 char "
                    "[A-Za-z0-9_-] identifier)"
                ) from None
        return v

    @model_validator(mode="after")
    def validate_env_var_shape(self) -> "RunStepRequest":
        """Cross-field checks (U2/C6, A3) — all surface as FastAPI-native 422s.

        RUN_ID <-> GENERATION is a symmetric required pair (ADR-9/10): an
        unanchored generation token — or a run id without its fence — would
        silently no-op the stale-generation fence. STEP_ID requires RUN_ID
        (a step key with no run to journal under is meaningless; RUN_ID
        without STEP_ID is allowed for run-row-level calls).
        """
        keys = set(self.env_vars or {})
        has_run = "CAO_WORKFLOW_RUN_ID" in keys
        has_gen = "CAO_WORKFLOW_GENERATION" in keys
        if has_run and not has_gen:
            raise ValueError("CAO_WORKFLOW_RUN_ID requires CAO_WORKFLOW_GENERATION (required pair)")
        if has_gen and not has_run:
            raise ValueError("CAO_WORKFLOW_GENERATION requires CAO_WORKFLOW_RUN_ID (required pair)")
        if "CAO_WORKFLOW_STEP_ID" in keys and not has_run:
            raise ValueError("CAO_WORKFLOW_STEP_ID requires CAO_WORKFLOW_RUN_ID")
        if self.env_vars and self.reuse_terminal_id:
            # run_agent_step documents env injection as ignored on reused
            # terminals — a silently dropped RUN_ID/GENERATION fence token is
            # the quiet identity failure NFR-SEC-4 exists to prevent (BR-8).
            raise ValueError(
                "env_vars cannot be injected into a reused terminal "
                "(env injection only applies to freshly created terminals)"
            )
        return self


class SessionEnvRebindRequest(BaseModel):
    """The non-secret, fingerprinted env map applied to future panes."""

    env_vars: Dict[str, str]
    fingerprint: str

    @field_validator("fingerprint")
    @classmethod
    def _fingerprint_shape(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("fingerprint must be a lowercase sha256 digest")
        return value

    @field_validator("env_vars")
    @classmethod
    def _env_shape(cls, value: Dict[str, str]) -> Dict[str, str]:
        for key, item in value.items():
            if not isinstance(key, str) or not key or not isinstance(item, str):
                raise ValueError("env_vars must be a map of non-empty names to strings")
            if any(ord(char) < 0x20 or ord(char) == 0x7F for char in item):
                raise ValueError(f"env var value for '{key}' contains control characters")
        return value


class RunStepResponse(BaseModel):
    """Response wrapping an ``AgentStepResult`` from ``run_agent_step``."""

    terminal_id: str
    last_message: str
    status: str


class WorkflowValidateRequest(BaseModel):
    """Request body for ``POST /workflows/validate`` (Bolt 2, N2)."""

    path: str = Field(description="Filesystem path to the workflow spec YAML file")


class StepOutputRequest(BaseModel):
    """Request body for the structured-return endpoint (Bolt 2, N4, C5).

    For the synthetic-key MVP there is no run record, so the step's
    ``output_schema`` arrives WITH the request (F2) rather than being re-resolved
    from a run aggregate.
    """

    output: Dict = Field(description="The worker-emitted JSON output for the step")
    output_schema: Optional[Dict] = Field(
        default=None, description="The step's JSON-Schema (Draft 2020-12); None = no validation"
    )


class WorkflowRunRequest(BaseModel):
    """Request body for ``POST /workflows/runs`` (Bolt 3, N5, C5)."""

    name_or_path: str = Field(description="Workflow name (indexed) or path to a spec YAML file")
    inputs: Dict = Field(
        default_factory=dict, description="Run inputs validated against spec.inputs"
    )
    run_id: Optional[str] = Field(
        default=None,
        description="Optional run id (matches WORKFLOW_NAME_RE); auto-generated if omitted",
    )


class GraphExportRequest(BaseModel):
    """Request body for ``POST /graph/{provider}/export`` (U4, Issue #348)."""

    sink: str = Field(description="Registered sink name (resolved via get_sink; KeyError -> 404)")
    dest: str = Field(
        description=(
            "Export destination, confined UNDER the configured graph-export root "
            "(CAO_GRAPH_EXPORT_ROOT). Treated as a path RELATIVE to that root; an "
            "absolute path is accepted only if it already resolves under the root, "
            "otherwise the export is rejected (400). Traversal/symlink escapes are "
            "rejected via safe_join_under_base."
        )
    )
    options: dict = Field(
        default_factory=dict,
        description="Opaque per-sink options forwarded as **options; the route never inspects them",
    )


class StepOutputResponse(BaseModel):
    """Response for the structured-return endpoint — mirrors the stored record."""

    validated: bool
    errors: List[str]
    state: str


class SkillContentResponse(BaseModel):
    """Response model for a skill content lookup."""

    name: str
    content: str


class WorkingDirectoryResponse(BaseModel):
    """Response model for terminal working directory."""

    working_directory: Optional[str] = Field(
        description="Current working directory of the terminal, or None if unavailable"
    )


class ManagedSessionOperationRequest(BaseModel):
    """One semantic control for an exact managed provider generation."""

    action: str
    operation_id: str
    generation: Optional[str] = None
    message: Optional[str] = None
    config_id: Optional[str] = None
    value: Optional[str] = None
    instruction: Optional[str] = None


class ControlInputRequest(BaseModel):
    """One identity-bound control, typed literally into a provider composer.

    Every field a caller can state about the target lives in
    ``expected_identity`` and is checked before the first byte, so a
    control aimed at a terminal that has since been replaced is refused
    rather than delivered somewhere plausible.
    """

    control_id: str
    # v1/v2 payload fields.  Optional at parse time because a v3 request
    # carries ``events`` instead; the service enforces the either/or rule
    # and the non-empty-text requirement for v1/v2, so a missing field is
    # a typed answer rather than an untyped pydantic failure.
    text: Optional[str] = None
    # Stated, never inferred from the text.  Submitting is the
    # irreversible half of a control, and a default that guessed would
    # make the caller's intent unreadable from the request.  An omitted
    # field keeps the v1 wire default (submit) for v1/v2 requests; an
    # explicit JSON null is a stated non-boolean and fails validation
    # (see ``_stated_enter``), as it did at F1.
    enter: Optional[bool] = None
    expected_identity: Optional[Dict[str, Any]] = None
    request_digest: Optional[str] = None
    protocol: Optional[str] = None
    # v2 only: a provider-pinned steer chord that replaces Enter as the
    # submit/steer effect (``enter`` must be false).  Declared so the field
    # survives parsing -- pydantic's default ``extra='ignore'`` would
    # otherwise drop it and a v2 request would silently deliver as v1
    # text-without-chord.  ``None`` is v1; a non-empty string is v2.
    chord: Optional[str] = None
    # v3 only: an ordered array of structured events
    # (``{"type":"text","text":...}``, ``{"type":"key","key":...}``,
    # ``{"type":"chord","chord":...}``) delivered as one at-most-once
    # control.  Declared for the same reason ``chord`` is: an old server's
    # parser must never silently drop a v3 payload and deliver the request
    # as something else.  Never combined with the v1/v2 fields.
    events: Optional[List[Dict[str, Any]]] = None
    # v4 only: the optional command-class declaration carrier
    # (``payload_class: "command"``).  Declared as ``Any`` on purpose: a
    # non-string value must reach the service, which answers with the
    # *typed* zero-write ``malformed-command-declaration`` refusal, rather
    # than dying as an untyped pydantic 422.  An explicit null is the
    # absent declaration (prose), exactly as ``chord`` treats null.  Never
    # combined with the v1/v2 fields; command-class is declared only by
    # this field, never derived from payload shape.
    payload_class: Optional[Any] = None
    # Bounded on purpose.  An unbounded wait converts a truthful
    # "the pane is busy, nothing was written, try again" into a request
    # that may never answer.
    lease_timeout: float = Field(default=0.0, ge=0.0, le=5.0)


class OperatorMessageRequest(BaseModel):
    """One identity-bound operator message (Lane C, design §8.3).

    A sibling typed operation to control-input — never an extension of it
    (D11): up to 8192 UTF-8 bytes, multi-line only through the provider's
    build-proven composer-newline plan, plus staged image attachments
    referenced by ``[Image #N]`` tokens in the draft text and mapped in
    ``token_map``.  The same 9-field ``expected_identity`` binds it, and
    the same at-most-once discipline governs it: reuse ``operation_id``
    only to reconcile; a refused message may be tried again with a new id.
    """

    operation_id: str
    text: str = ""
    attachments: Optional[List[str]] = None
    token_map: Optional[Dict[str, str]] = None
    expected_identity: Optional[Dict[str, Any]] = None
    # Same bounded-wait discipline as control-input.
    lease_timeout: float = Field(default=0.0, ge=0.0, le=5.0)


class InstallAgentProfileRequest(BaseModel):
    """Request body for installing an agent profile.

    ``env_vars`` travels in the JSON body rather than as a query parameter so
    that any secrets callers inject are not written to HTTP access logs.

    ``provider`` may be omitted (None): the install service then honours the
    profile's frontmatter ``provider:`` key, falling back to the default
    provider — the same flag > frontmatter > default precedence as the CLI.
    """

    source: str
    provider: Optional[str] = None
    env_vars: Optional[Dict[str, str]] = None


class MemorySummary(BaseModel):
    """Memory list entry. Excludes file_path (absolute server filesystem path)."""

    key: str
    scope: str
    scope_id: Optional[str] = Field(
        description="Native for session/agent, derived from storage path for project, None for global"
    )
    memory_type: str
    tags: str
    created_at: datetime
    updated_at: datetime


class MemoryDetail(MemorySummary):
    """Full memory view — adds the latest wiki section content."""

    content: str


class CreateFlowRequest(BaseModel):
    """Request model for creating a flow."""

    name: str
    schedule: str
    agent_profile: str
    provider: str = "kiro_cli"
    prompt_template: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        """Prevent path traversal — flow name becomes a filename."""
        if "/" in v or "\\" in v or ".." in v:
            raise ValueError("Flow name must not contain '/', '\\', or '..'")
        return v


def _reconcile_memory_at_startup() -> None:
    """Apply bounded memory repair and keep server startup resilient."""
    try:
        from cli_agent_orchestrator.services import memory_reconciliation

        repair_report = memory_reconciliation.reconcile_memory_startup()
        if repair_report is not None:
            logger.info(repair_report.summary_text())
    except Exception as exc:
        report = getattr(exc, "report", None)
        if report is not None:
            logger.error(
                "%s; automatic memory repair was incomplete; run `cao memory repair --apply`",
                report.summary_text(),
            )
        else:
            logger.error(
                "automatic memory repair failed (%s); run `cao memory repair --apply`",
                type(exc).__name__,
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    logger.info("Starting CLI Agent Orchestrator server...")
    setup_logging()
    # Scrub credential query params (``?access_token=`` / ``?ticket=``) from
    # uvicorn's access log before any request is served. Installed here — not
    # only in ``main()`` — so the imported-app deployment path
    # (``uvicorn cli_agent_orchestrator.api.main:app``) is covered too. Idempotent.
    install_access_log_redaction()
    # OpenTelemetry (ported): opt-in — no-op unless OTEL_SDK_DISABLED=false.
    # Safe to call unconditionally; failure-isolated so it never blocks boot.
    try:
        init_telemetry(OTEL_SERVICE_NAME)
    except Exception:
        logger.warning("OTel telemetry init failed; continuing", exc_info=True)
    init_db()
    _reconcile_memory_at_startup()
    registry = PluginRegistry()
    await registry.load()
    app.state.plugin_registry = registry

    # Run cleanup in background
    asyncio.create_task(asyncio.to_thread(cleanup_old_data))
    asyncio.create_task(cleanup_expired_memories())
    # Lane C: sweep expired/orphaned image attachments at startup (§8.4 —
    # submitted images past the 24 h retention, crashed-upload orphans,
    # stale staging/failed records; never ``ready`` operator drafts).
    asyncio.create_task(asyncio.to_thread(image_attachments.sweep_attachments))

    # Start flow daemon as background task
    daemon_task = asyncio.create_task(flow_daemon())

    # Register event loop with event bus for thread-safe publishing
    loop = asyncio.get_running_loop()
    bus.set_loop(loop)

    # Start event bus consumers as background tasks
    status_monitor_task = asyncio.create_task(status_monitor.run())
    log_writer_task = asyncio.create_task(log_writer.run())
    inbox_service_task = asyncio.create_task(inbox_service.run(registry))
    logger.info("Event bus consumers started (StatusMonitor, LogWriter, InboxService)")

    # Restart recovery: re-attach output pipelines for terminals created by a
    # previous server process, else their status sticks at UNKNOWN and
    # idle-gated inbox delivery to them never fires.
    try:
        await asyncio.to_thread(terminal_service.reattach_existing_output_pipelines)
    except Exception:
        logger.warning("output-pipeline reattach failed", exc_info=True)

    # Restart recovery: drop persisted session-env rows whose tmux session no
    # longer exists (torn down while the server was dead); live-session rows
    # are retained so post-restart windows keep receiving the forwarded env.
    try:
        await asyncio.to_thread(terminal_service.reconcile_session_env)
    except Exception:
        logger.warning("session-env reconcile failed", exc_info=True)

    # Restart recovery: release provider-session claims whose owning process
    # died with the previous server. Teardown resolves the claims it is
    # present for, and a server exit runs no teardown at all — that gap is
    # what left every native session on an install unresumable. Boot is the
    # cleanest moment to close it: those pids are gone for good, while the
    # panes that genuinely survived the restart still hold live processes and
    # are refused on exactly the same test.
    try:
        await asyncio.to_thread(native_attachment_recovery.sweep_at_startup)
    except Exception:
        logger.warning("native attachment sweep failed", exc_info=True)

    # Start temporary OpenCode inbox poller. GH #115 tracks replacing this
    # provider-specific wakeup path with a unified delivery engine.
    opencode_inbox_task = asyncio.create_task(opencode_inbox_delivery_daemon(registry))

    # Start provider-agnostic reconciliation sweep for orphaned PENDING messages
    # the immediate and event-driven status paths missed (issue #131).
    inbox_reconcile_task = asyncio.create_task(inbox_reconciliation_daemon(registry))

    # Herdr delivers inbox via its own socket events; the tmux backend uses the
    # FIFO -> EventBus pipeline (StatusMonitor / LogWriter / InboxService) started
    # above. Start the herdr inbox service only when the herdr backend is active
    # (additive; no-op for tmux). See #271.
    herdr_inbox_task: Optional[asyncio.Task] = None
    backend = get_backend()
    if isinstance(backend, HerdrBackend):

        def deliver_inbox(terminal_id: str) -> None:
            inbox_service.deliver_pending(terminal_id, registry=registry)

        svc = HerdrInboxService(
            herdr_session=backend.herdr_session,
            delivery_callback=deliver_inbox,
        )
        set_herdr_inbox_service(svc)
        herdr_inbox_task = asyncio.create_task(svc.start())
        logger.info("Herdr inbox service started")

    yield

    # Stop herdr inbox service on shutdown
    if herdr_inbox_task is not None:
        herdr_inbox_task.cancel()
        try:
            await herdr_inbox_task
        except asyncio.CancelledError:
            pass
        set_herdr_inbox_service(None)
        logger.info("Herdr inbox service stopped")

    # Cancel consumer tasks on shutdown
    status_monitor_task.cancel()
    log_writer_task.cancel()
    inbox_service_task.cancel()
    # Cancel daemon on shutdown
    daemon_task.cancel()

    try:
        await asyncio.gather(
            status_monitor_task,
            log_writer_task,
            inbox_service_task,
            daemon_task,
            return_exceptions=True,
        )
    except asyncio.CancelledError:
        pass

    # Cancel OpenCode inbox poller on shutdown
    opencode_inbox_task.cancel()
    try:
        await opencode_inbox_task
    except asyncio.CancelledError:
        pass

    # Cancel inbox reconciliation sweep on shutdown
    inbox_reconcile_task.cancel()
    try:
        await inbox_reconcile_task
    except asyncio.CancelledError:
        pass

    # Stop the pipe-pane liveness watchdog thread (issue #388). It is a plain
    # threading.Thread (not asyncio), so join it directly rather than via
    # asyncio.gather with the tasks above.
    fifo_manager.stop_watchdog()

    await registry.teardown()
    # OpenTelemetry (ported): flush + shut down exporters (no-op when disabled).
    try:
        shutdown_telemetry()
    except Exception:
        logger.warning("Error shutting down OTel telemetry", exc_info=True)
    logger.info("Shutting down CLI Agent Orchestrator server...")


def get_plugin_registry(request: Request) -> PluginRegistry:
    """Return the plugin registry stored on the FastAPI application state."""

    return cast(PluginRegistry, request.app.state.plugin_registry)


# Values that indicate ``TERM`` is effectively unusable and must be overridden
# rather than inherited by the tmux attach subprocess. ``dumb`` is the common
# fallback that containers and devcontainers ship with when no real terminal
# is attached. Empty string and missing key behave the same way.
_UNUSABLE_TERM_VALUES = frozenset({"", "dumb"})
_DEFAULT_PTY_TERM = "xterm-256color"


def _build_pty_env() -> Dict[str, str]:
    """Build the env handed to the tmux PTY attach subprocess.

    Copies the parent process environment so cao-server's normal config
    (PATH, HOME, AWS_*, etc.) reaches tmux, and forces ``TERM`` to a usable
    value when the inherited one would break terminal rendering. Explicit
    non-dumb ``TERM`` values from the operator are preserved verbatim. See
    issue #150.
    """
    env = os.environ.copy()
    if env.get("TERM", "") in _UNUSABLE_TERM_VALUES:
        env["TERM"] = _DEFAULT_PTY_TERM
    return env


app = FastAPI(
    title="CLI Agent Orchestrator",
    description="Simplified CLI Agent Orchestrator API",
    version=SERVER_VERSION,
    lifespan=lifespan,
)

# Security: DNS Rebinding Protection
# Validate Host header to prevent DNS rebinding attacks (CVE mitigation)
# Only allow requests with localhost Host headers
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=ALLOWED_HOSTS,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Project-scoped issue tracking lives in its own module rather than inline
# here; see api/tracker.py.
app.include_router(tracker_router)
app.include_router(native_attachments_router)
# Registered before the app-level /sessions routes below, which is what lets
# /sessions/{name}/lifecycle resolve without inheriting their tmux-existence
# guard — a stopped session has no tmux session, and that is the point.
app.include_router(session_lifecycle_router)
app.include_router(roster_router)


@app.exception_handler(RequestValidationError)
async def _redact_env_vars_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Redact ``env_vars`` VALUES from 422 bodies (U2, NFR-SEC-4).

    FastAPI's default 422 envelope echoes the offending ``input`` back to the
    caller. For ``env_vars`` violations the values are agent- or
    attacker-supplied and must never round-trip into a response body — the
    validator messages already name only the key and the rule, so the echoed
    ``input``/``ctx`` are dropped for those entries. Every other field's 422
    keeps FastAPI's stock shape byte-identical.
    """
    errors = []
    for err in exc.errors():
        # Field-validator errors anchor at ("body", "env_vars"); model-validator
        # errors anchor at ("body",) with the WHOLE body echoed as input — both
        # shapes can carry env_vars values, so both are redacted.
        echoes_env_vars = "env_vars" in err.get("loc", ()) or (
            isinstance(err.get("input"), dict) and "env_vars" in err["input"]
        )
        if echoes_env_vars:
            err = {k: v for k, v in err.items() if k not in ("input", "ctx")}
        errors.append(err)
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


@app.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource_metadata():
    """RFC 9728 Protected Resource Metadata.

    Advertises the resource audience, the authorization server(s), the supported
    scopes (``cao:read``/``cao:write``/``cao:admin``), and the supported bearer
    methods so OAuth clients can discover how to obtain access. Returns HTTP 404
    when auth is disabled (default-off), so the localhost-only posture is
    byte-for-byte unchanged.
    """
    if not is_auth_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="auth disabled")

    audience = (
        os.getenv("CAO_AUTH_AUDIENCE", "").strip()
        or os.getenv("AUTH0_AUDIENCE", "").strip()
        or API_BASE_URL
    )
    return {
        "resource": audience,
        "authorization_servers": get_authorization_servers(),
        "scopes_supported": SCOPES_SUPPORTED,
        "bearer_methods_supported": ["header"],
    }


@app.get("/health")
async def health_check():
    import shutil

    from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend

    def _probe(binary: str) -> str:
        return "ok" if shutil.which(binary) else "unavailable"

    backend = get_backend()
    backend_name = "herdr" if isinstance(backend, HerdrBackend) else "tmux"

    return {
        "status": "ok",
        "service": "cli-agent-orchestrator",
        "terminal_backend": backend_name,
        "components": {
            "cao": "ok",
            "herdr": _probe("herdr"),
            "claude": _probe("claude"),
        },
    }


def _mcp_apps_enabled() -> bool:
    """Whether the MCP Apps HTTP surface (event stream + widget) is enabled.

    Reads ``apps.enabled`` via ConfigService (``CAO_MCP_APPS_ENABLED`` env var
    or ``settings.json``), mirroring the gate used by the ``mcp_apps`` plugin,
    ``app_tools``, ``sep2133`` and the ``event_log_publisher`` observer so the
    whole surface is consistently default-off.
    """

    return bool(ConfigService.get("apps.enabled", default=False))


def _require_mcp_apps_enabled() -> None:
    """Raise 404 when the MCP Apps surface is disabled (default-off).

    The ``/events`` SSE stream and ``/events/history`` replay expose fleet
    metadata (terminal ids, session names, routing/launch/kill topology), so
    they must not be reachable unless an operator opts in via
    ``CAO_MCP_APPS_ENABLED`` — matching the default-off posture of the rest of
    the surface (tools, resources, widget, capability advertisement).
    """

    if not _mcp_apps_enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="MCP Apps surface disabled"
        )


def _agui_enabled() -> bool:
    """Whether the AG-UI SSE surface (``/agui/v1/stream``, ``emit_ui``) is enabled.

    Two enablement paths, both deliberate (documented in docs/agui.md):

    * ``CAO_AGUI_ENABLED`` — the dedicated flag, so AG-UI can be turned on
      independently of the MCP Apps iframe surface.
    * ``CAO_MCP_APPS_ENABLED`` (via ``_mcp_apps_enabled()``) — the pre-existing
      MCP Apps flag also enables AG-UI, because the two surfaces are read-outs
      of the same in-process event source (``EventLogPublisher`` → ``SseBus``)
      with the same privacy boundary; an operator who exposed that data to the
      iframe has already made the disclosure decision AG-UI relies on.

    With neither flag set the surface is absent (404s) and the server is
    byte-identical to a build without this feature.
    """

    if os.environ.get("CAO_AGUI_ENABLED", "").strip().lower() in ("1", "true", "yes"):
        return True
    # Shared with the EventLogPublisher observer so the route and the publisher
    # that feeds it can never disagree about whether the surface is live.
    from cli_agent_orchestrator.services.agui_enablement import agui_surface_enabled

    return agui_surface_enabled()


def _require_agui_enabled() -> None:
    """Raise 404 when the AG-UI surface is disabled (default-off)."""

    if not _agui_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AG-UI surface disabled")


@app.get("/events")
async def events_stream(
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
):
    """Stream live, normalized fleet events to the iframe as Server-Sent Events.

    Events come from the in-process ``SseBus`` (fed by the ``EventLogPublisher``
    plugin). The bus is drop-on-slow with a bounded per-subscriber queue, so one
    stalled iframe never applies back-pressure to the orchestration core; gaps are
    backfilled by the client via ``/events/history`` / ``cao_fetch_history``.

    Default-off: returns 404 unless ``CAO_MCP_APPS_ENABLED`` is set, so the fleet
    event timeline (terminal ids, session names, routing/topology metadata) is
    never exposed when the surface is disabled. When auth is enabled, any of
    ``cao:read`` / ``cao:write`` / ``cao:admin`` is required (read is the floor).
    """
    _require_mcp_apps_enabled()

    from fastapi.responses import StreamingResponse

    from cli_agent_orchestrator.services.sse_bus import get_bus

    async def event_generator():
        async for event in get_bus().subscribe():
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/events/history")
async def events_history(
    limit: int = Query(default=RING_CAPACITY, ge=0, le=RING_CAPACITY),
    since: Optional[str] = None,
    kinds: Optional[str] = None,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Replay recent fleet events from the ring buffer (JSON, newest-last).

    Events are already normalized to the six-primitive vocabulary at append time.
    ``kinds`` is an optional comma-separated filter; ``since`` is an ISO-8601
    timestamp lower bound (exclusive).

    Input hardening: ``limit`` is clamped to ``[0, RING_CAPACITY]`` (the buffer is
    bounded anyway, so a larger value can never return more) and each ``kinds``
    token is validated against the closed event vocabulary — an unknown kind is
    rejected with 400 rather than silently matching nothing.

    Default-off: returns 404 unless ``CAO_MCP_APPS_ENABLED`` is set; when auth is
    enabled, any of ``cao:read`` / ``cao:write`` / ``cao:admin`` is required.
    """
    _require_mcp_apps_enabled()

    from cli_agent_orchestrator.services.event_log_service import get_event_log

    kinds_filter = [k.strip() for k in kinds.split(",") if k.strip()] if kinds else None
    if kinds_filter:
        invalid = [k for k in kinds_filter if k not in EVENT_KINDS]
        if invalid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Invalid event kind(s): {', '.join(invalid)}. "
                    f"Valid kinds: {', '.join(EVENT_KINDS)}"
                ),
            )
    events = get_event_log().history(limit=limit, since=since, kinds=kinds_filter)
    return {"events": events}


@app.get("/agui/v1/stream")
async def agui_stream(
    since: Optional[str] = Query(
        default=None,
        description=(
            "ISO-8601 lower bound. When set, buffered events after this "
            "timestamp are replayed (as AG-UI frames) before the live stream; "
            "clients dedupe by event id."
        ),
    ),
    access_token: Optional[str] = Query(
        default=None,
        description=(
            "JWT for auth-enabled mode. Native EventSource cannot set an "
            "Authorization header, so the token travels as this query parameter."
        ),
    ),
    last_event_id: Optional[str] = Header(
        default=None,
        alias="Last-Event-ID",
        description=(
            "Native EventSource reconnect cursor. When set (and ``?since=`` is "
            "not), buffered events after this event id are replayed before the "
            "live stream, so no event is lost across a reconnect. ``?since=`` "
            "takes precedence when both are supplied."
        ),
    ),
):
    """Stream fleet events as AG-UI typed events (Server-Sent Events).

    This is the L2 standalone-dashboard surface (consumed by any AG-UI client). It
    shares the exact same source as ``/events`` — the in-process ``SseBus`` fed
    by the ``EventLogPublisher`` — but re-maps each normalized six-primitive
    record onto AG-UI typed events via ``agui_stream.to_agui_event`` before it
    hits the wire, so any AG-UI-compatible client renders CAO with no custom
    adapter code.

    Each SSE frame is a *named* AG-UI event: ``event: <AGUI_TYPE>`` +
    ``data: <json>``. Message bodies are never carried (the ring buffer stores
    metadata only and the mapping redacts by construction).

    Default-off: returns 404 unless the AG-UI surface is enabled via
    ``CAO_AGUI_ENABLED`` (or the MCP Apps surface is on). When auth is enabled,
    a ``cao:read``-bearing JWT must be supplied via ``?access_token=`` (native
    EventSource cannot send Authorization headers).
    """
    _require_agui_enabled()

    # Auth: query-parameter token (EventSource can't set headers). Default-off
    # (no AUTH0_DOMAIN / CAO_AUTH_JWKS_URI) grants the full scope set.
    if is_auth_enabled():
        if not access_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="access_token query parameter required when auth is enabled",
            )
        try:
            scopes = extract_scopes_from_token(access_token)
        except HTTPException:
            raise
        except Exception:
            # PyJWTError subclasses (malformed/expired/bad signature) or a JWKS
            # fetch failure. Fails closed either way; map to a clean 401 instead
            # of an opaque 500 so auth telemetry stays trustworthy.
            logger.info("agui_stream: token validation failed", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid or expired access_token",
            )
        if not any(s in scopes for s in (SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="insufficient scope (cao:read required)",
            )
    else:
        scopes = [SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN]

    from fastapi.responses import StreamingResponse

    from cli_agent_orchestrator.clients.database import list_terminals_by_session
    from cli_agent_orchestrator.services import session_service
    from cli_agent_orchestrator.services.agui_stream import (
        state_delta_frame,
        state_snapshot_frame,
        to_agui_event,
    )
    from cli_agent_orchestrator.services.event_log_service import get_event_log
    from cli_agent_orchestrator.services.sse_bus import get_bus
    from cli_agent_orchestrator.services.ui_state_service import build_dashboard_snapshot

    def _fleet_snapshot() -> Dict:
        """Build the current DashboardSnapshot from live session/terminal state.

        Failure-isolated: any backend hiccup yields an empty snapshot rather
        than tearing down the stream. ``list_sessions`` already returns ``[]``
        on error, so an unavailable tmux/herdr backend degrades gracefully.
        """
        sessions = session_service.list_sessions()
        terminals: List[Dict] = []
        for sess in sessions:
            try:
                # Through the projection, like every other dashboard
                # surface. Reading raw rows here would make this stream the
                # one view that still shows a dead terminal as live and
                # cannot see a managed v2 worker at all.
                terminals.extend(terminal_projection.project_session(sess["id"]))
            except Exception:
                logger.debug("agui_stream: terminal listing failed for %s", sess.get("id"))
        return build_dashboard_snapshot(sessions, terminals, list(scopes))

    def _sse(event_id: Optional[str], agui_type: str, data: Dict) -> str:
        """Format one SSE frame, with an ``id:`` cursor when the event has one."""

        prefix = f"id: {event_id}\n" if event_id is not None else ""
        return f"{prefix}event: {agui_type}\ndata: {json.dumps(data)}\n\n"

    async def event_generator():
        # Register the live subscription BEFORE replaying history / taking the
        # snapshot, so an event published during the replay->live handoff is
        # buffered in this queue rather than lost. The small replay/live overlap
        # is de-duplicated by event id below, so a ``?since=`` reconnect resumes
        # with neither a gap nor a duplicate. The queue is metadata-only, same
        # as the live path.
        bus = get_bus()
        # Opt into overflow-as-gap-signal: if this subscriber's bounded queue
        # fills, the drain loop closes the stream (instead of silently dropping
        # events on an open connection) so the client reconnects with
        # Last-Event-ID and replays the dropped records exactly once (F2).
        sub = bus.register(overflow_close=True)
        try:
            replayed_ids: set = set()

            # Optional replay. Precedence: an explicit ``?since=`` timestamp wins;
            # otherwise a native-EventSource ``Last-Event-ID`` reconnect replays
            # the records buffered after that id. Either way, re-emit the
            # buffered history as AG-UI frames and remember the ids so the live
            # drain skips the overlap. Failure-isolated: a log hiccup logs and
            # falls through to the live stream rather than 500-ing.
            try:
                replay_records = None
                if since:
                    replay_records = get_event_log().history(since=since)
                elif last_event_id:
                    replay_records = get_event_log().after_id(last_event_id)
                if replay_records is not None:
                    for record in replay_records:
                        rid = record.get("id")
                        if rid is not None:
                            replayed_ids.add(rid)
                        rtype, rdata = to_agui_event(record)
                        yield _sse(rid, rtype, rdata)
            except Exception:
                logger.warning("agui_stream: history replay failed", exc_info=True)

            # AG-UI shared-state: emit a full STATE_SNAPSHOT on connect so any
            # client hydrates its projection, then keep it current with minimal
            # RFC-6902 STATE_DELTA patches after each fleet event.
            prev_snapshot: Optional[Dict] = None
            try:
                prev_snapshot = _fleet_snapshot()
                agui_type, data = state_snapshot_frame(prev_snapshot)
                yield _sse(None, agui_type, data)
            except Exception:
                logger.warning("agui_stream: initial STATE_SNAPSHOT failed", exc_info=True)

            # Drain the subscriber registered above (buffered handoff events
            # first, then live), via the bus's drain seam so a fake can terminate
            # the stream cleanly in tests. On overflow the drain closes so the
            # client reconnects (F2); cancellation on client disconnect
            # propagates through the ``finally`` that unregisters the subscriber.
            async for event in bus.drain(sub):
                rid = event.get("id")
                # Skip the replay/live overlap so a reconnecting client that
                # passed ``?since=`` never sees an event twice.
                if rid is not None and rid in replayed_ids:
                    replayed_ids.discard(rid)
                    continue
                agui_type, data = to_agui_event(event)
                yield _sse(rid, agui_type, data)

                # Recompute the fleet snapshot and emit a STATE_DELTA when it
                # moved. NB: recomputes on every event; a debounce/cache is a
                # natural follow-up for high event rates (this is the opt-in L2
                # dashboard surface, not the orchestration hot path).
                try:
                    curr = _fleet_snapshot()
                    if prev_snapshot is not None:
                        delta = state_delta_frame(prev_snapshot, curr)
                        if delta is not None:
                            dtype, ddata = delta
                            yield _sse(None, dtype, ddata)
                    prev_snapshot = curr
                except Exception:
                    logger.warning("agui_stream: STATE_DELTA computation failed", exc_info=True)
        finally:
            bus.unregister(sub)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


class EmitUIRequest(BaseModel):
    """Body for POST /agui/v1/emit_ui — an agent-authored generative-UI intent."""

    component: str
    props: Dict[str, Any] = Field(default_factory=dict)
    terminal_id: Optional[str] = None
    session_name: Optional[str] = None


@app.post("/agui/v1/emit_ui")
async def agui_emit_ui(
    body: EmitUIRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Producer for agent-authored generative-UI intents (closes the AG-UI loop).

    An agent — via the ``emit_ui`` MCP tool — declares a component from the
    frozen allow-list; the intent is validated **server-side** here and
    published onto the fleet event bus, where ``agui_stream.to_agui_event`` maps
    it to a ``GENERATIVE_UI`` frame on ``/agui/v1/stream``. Off-list components
    and oversized/non-serializable props are rejected (400) so a bad intent
    never reaches the bus. Requires ``cao:write`` when auth is enabled.
    """
    _require_agui_enabled()

    from cli_agent_orchestrator.services.agui_stream import GENERATIVE_UI_COMPONENTS
    from cli_agent_orchestrator.services.event_log_service import get_event_log
    from cli_agent_orchestrator.services.sse_bus import get_bus

    if body.component not in GENERATIVE_UI_COMPONENTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unknown UI component '{body.component}'. "
                f"Allowed: {sorted(GENERATIVE_UI_COMPONENTS)}"
            ),
        )
    try:
        encoded = json.dumps(body.props)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="props must be JSON-serializable",
        )
    if len(encoded.encode("utf-8")) > 8 * 1024:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="props payload too large (>8KB)"
        )

    detail = {
        "event_type": "agent_ui",
        "ui": {"component": body.component, "props": body.props},
    }
    event = get_event_log().append("other", body.terminal_id, body.session_name, detail)
    get_bus().publish(event)
    return {"ok": True, "event_id": event.get("id"), "component": body.component}


# Topology widget static bundle at /widgets/topology/ — the vanilla SSE-driven
# view consumed alongside the /events stream above. The mount is default-off
# (no-op unless CAO_MCP_APPS_ENABLED is set) and idempotent, so re-importing this
# module under dev/reload is safe.
mount_widget_static(app)


@app.get("/agents/profiles")
async def list_agent_profiles_endpoint() -> List[Dict]:
    """List all available agent profiles from all configured directories."""
    try:
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        return list_agent_profiles()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list agent profiles: {str(e)}",
        )


@app.get("/agents/profiles/{name}")
async def get_agent_profile_endpoint(name: str) -> Dict:
    """Return the full parsed content of a named agent profile."""
    try:
        profile = load_agent_profile(name)
        return profile.model_dump(exclude_none=True)
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@app.post("/agents/profiles/install")
async def install_agent_profile_endpoint(
    request: InstallAgentProfileRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> InstallResult:
    """Install an agent profile for a target provider.

    HTTP (and transitively ``cao-ops-mcp``, which calls this endpoint) is an
    untrusted surface. ``install_agent()`` only accepts bare profile names or
    https:// URLs; local filesystem paths are handled by the CLI entry point
    alone. A remote caller therefore cannot coerce the server into reading
    arbitrary ``.md`` files from disk.
    """
    result = install_agent(
        source=request.source,
        provider=request.provider,
        env_vars=request.env_vars,
    )
    if not result.success:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result.message)

    return result


@app.get("/agents/providers")
async def list_providers_endpoint() -> List[Dict]:
    """List available providers with installation status."""
    import shutil

    provider_binaries = {
        "kiro_cli": "kiro-cli",
        "claude_code": "claude",
        "codex": "codex",
        "hermes": "hermes",
        "kimi_cli": "kimi",
        "copilot_cli": "copilot",
        "opencode_cli": "opencode",
        "cursor_cli": "agent",
        "antigravity_cli": "agy",
    }
    result = []
    for provider, binary in provider_binaries.items():
        installed = shutil.which(binary) is not None
        result.append({"name": provider, "binary": binary, "installed": installed})
    return result


@app.get("/settings/agent-dirs")
async def get_agent_dirs_endpoint(
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Get configured agent directories per provider.

    Read-scope gated when auth is enabled: the response discloses local
    filesystem layout (home paths), so it gets the same floor as other reads.
    """
    from cli_agent_orchestrator.services.settings_service import (
        get_agent_dirs,
        get_disabled_agent_dirs,
        get_extra_agent_dirs,
    )

    return {
        "agent_dirs": get_agent_dirs(),
        "extra_dirs": get_extra_agent_dirs(),
        "disabled_dirs": get_disabled_agent_dirs(),
    }


class AgentDirsUpdate(BaseModel):
    agent_dirs: Optional[Dict[str, str]] = None
    extra_dirs: Optional[List[str]] = None
    disabled_dirs: Optional[List[str]] = None


@app.get("/settings/memory")
async def get_memory_settings_endpoint() -> Dict:
    """Return whether the memory subsystem is enabled (for UI feature discovery)."""
    from cli_agent_orchestrator.services.settings_service import is_memory_enabled

    return {"enabled": is_memory_enabled()}


@app.post("/settings/agent-dirs")
async def set_agent_dirs_endpoint(
    body: AgentDirsUpdate,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Update agent directories per provider (paths, extras, and disabled set)."""
    from cli_agent_orchestrator.services.settings_service import (
        get_agent_dirs,
        get_disabled_agent_dirs,
        get_extra_agent_dirs,
        set_agent_dirs,
        set_disabled_agent_dirs,
        set_extra_agent_dirs,
    )

    if body.agent_dirs:
        set_agent_dirs(body.agent_dirs)
    if body.extra_dirs is not None:
        set_extra_agent_dirs(body.extra_dirs)
    # After extras are persisted, so a just-added extra can be disabled in the
    # same request; set_disabled validates against the current known dirs.
    if body.disabled_dirs is not None:
        set_disabled_agent_dirs(body.disabled_dirs)
    return {
        "agent_dirs": get_agent_dirs(),
        "extra_dirs": get_extra_agent_dirs(),
        "disabled_dirs": get_disabled_agent_dirs(),
    }


@app.get("/settings/skill-dirs")
async def get_skill_dirs_endpoint() -> Dict:
    """Get the global skill store path and user-added extra skill directories."""
    from cli_agent_orchestrator.constants import SKILLS_DIR
    from cli_agent_orchestrator.services.settings_service import get_extra_skill_dirs

    return {"skills_dir": str(SKILLS_DIR), "extra_dirs": get_extra_skill_dirs()}


class SkillDirsUpdate(BaseModel):
    extra_dirs: Optional[List[str]] = None


@app.post("/settings/skill-dirs")
async def set_skill_dirs_endpoint(
    body: SkillDirsUpdate,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Update user-added extra skill directories."""
    from cli_agent_orchestrator.constants import SKILLS_DIR
    from cli_agent_orchestrator.services.settings_service import (
        get_extra_skill_dirs,
        set_extra_skill_dirs,
    )

    result_extra: List[str] = []
    if body.extra_dirs is not None:
        result_extra = set_extra_skill_dirs(body.extra_dirs)
    return {
        "skills_dir": str(SKILLS_DIR),
        "extra_dirs": result_extra or get_extra_skill_dirs(),
    }


# ── Operator macro library (§5.4) ────────────────────────────────────────
#
# The durable macro store of §5: versioned JSON at CAO_HOME_DIR/macros.json,
# flock-serialized atomic writes, quarantine reporting on every list.  READ
# scope for list/parse, WRITE scope for mutations — the settings-route
# discipline.  Validation failures are 422 with an ``errors`` array of
# ``{offset, message}`` pairs (§5.3); built-in mutation attempts are 409;
# unknown ids are 404.  Sending a macro is deliberately NOT a store
# operation (§5.4): the client takes the resolved events and sends an
# ordinary v3 control-input request (D2) — the store never writes to panes.


class MacroScopeBody(BaseModel):
    kind: str
    provider: Optional[str] = None
    profile: Optional[str] = None


class MacroWriteBody(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    scope: Optional[MacroScopeBody] = None
    events: Optional[List[Dict[str, Any]]] = None
    notation: Optional[str] = None
    favorite: Optional[bool] = None


class MacroDuplicateBody(BaseModel):
    name: Optional[str] = None


def _macro_validation_response(exc: "macro_store.MacroValidationError") -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"errors": exc.errors},
    )


def _macro_write_kwargs(body: MacroWriteBody) -> Dict[str, Any]:
    return {
        "name": body.name,
        "description": body.description,
        "scope": body.scope.model_dump() if body.scope is not None else None,
        "events": body.events,
        "notation": body.notation,
        "favorite": body.favorite,
    }


@app.get("/macros")
async def list_macros_endpoint(
    provider: Optional[str] = None,
    profile: Optional[str] = None,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    """The visible macro set: registry built-ins for ``provider`` (synthesized,
    D6) plus user records whose scope is global, provider-matching, or
    profile-matching, in the pinned server-side order (§5.4).  Reports
    ``quarantine`` while a quarantine file exists (§5.2)."""
    from cli_agent_orchestrator.services import macro_store

    return await asyncio.to_thread(macro_store.list_macros, provider, profile)


@app.post("/macros", status_code=status.HTTP_201_CREATED)
async def create_macro_endpoint(
    body: MacroWriteBody,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    """Create a user macro from ``events`` or ``notation`` (exactly one)."""
    from cli_agent_orchestrator.services import macro_store

    try:
        return await asyncio.to_thread(macro_store.create_macro, **_macro_write_kwargs(body))
    except macro_store.MacroValidationError as exc:
        return _macro_validation_response(exc)


@app.put("/macros/{macro_id}")
async def update_macro_endpoint(
    macro_id: str,
    body: MacroWriteBody,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    """Full replace of a user record's mutable fields; built-in ids 409."""
    from cli_agent_orchestrator.services import macro_store

    try:
        return await asyncio.to_thread(
            macro_store.update_macro, macro_id, **_macro_write_kwargs(body)
        )
    except macro_store.MacroValidationError as exc:
        return _macro_validation_response(exc)
    except macro_store.BuiltinMacroConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except macro_store.MacroNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no macro with id {macro_id!r}",
        ) from exc


@app.delete("/macros/{macro_id}")
async def delete_macro_endpoint(
    macro_id: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    """Delete a user record; built-in ids 409."""
    from cli_agent_orchestrator.services import macro_store

    try:
        return await asyncio.to_thread(macro_store.delete_macro, macro_id)
    except macro_store.BuiltinMacroConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except macro_store.MacroNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no macro with id {macro_id!r}",
        ) from exc


@app.post("/macros/{macro_id}/duplicate", status_code=status.HTTP_201_CREATED)
async def duplicate_macro_endpoint(
    macro_id: str,
    body: MacroDuplicateBody,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    """Mint a user record from any source — the only way to "edit" a built-in."""
    from cli_agent_orchestrator.services import macro_store

    try:
        return await asyncio.to_thread(macro_store.duplicate_macro, macro_id, name=body.name)
    except macro_store.MacroValidationError as exc:
        return _macro_validation_response(exc)
    except macro_store.MacroNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no macro with id {macro_id!r}",
        ) from exc


@app.get("/skills/{name}", response_model=SkillContentResponse)
async def get_skill_content(name: str) -> SkillContentResponse:
    """Return the full Markdown body for an installed skill."""
    try:
        skill_name = validate_skill_name(name)
        content = load_skill_content(skill_name)
        return SkillContentResponse(name=name, content=content)
    except SkillNameError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid skill name: {name}",
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load skill: {str(e)}",
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill not found: {name}",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load skill: {str(e)}",
        )


@app.get("/annotations", response_model=AnnotationsResponse)
async def list_annotations(
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
) -> AnnotationsResponse:
    """Conductor-published annotations for the dashboard's chips (design §9.5).

    THE ONE ADDITIVE FORK SEAM, and it is meant to be the last one. It reads a
    fixed, non-configurable, conductor-owned location and passes what it finds
    through verbatim, bounded and confined. It has no vocabulary of kinds,
    roles, subjects or facets, so the conductor can evolve the status model
    indefinitely without another fork release — see
    ``services/annotations.py`` for why each of those omissions is deliberate.

    **The signature takes no parameters, and that is the security property.**
    Not a project, not a path, not a filter. There is therefore no caller input
    that can reach a filesystem operation — path confinement here is a
    consequence of the route's shape rather than of sanitising a string, and
    symlink escape is refused explicitly on top of that.

    **It cannot fail.** A missing conductor state root, an unreadable one, a
    malformed or oversized document, an item the fork cannot represent, and a
    non-regular file where a document should be all degrade to a shorter list
    with a typed reason. No single source can unwind the fan-out: each is read
    inside its own handler, so one producer's bad document costs that producer
    and nothing else. ``coverage: "unavailable"`` with an empty list is the
    ordinary answer on a machine with no conductor, and it renders exactly as
    the dashboard did before this route existed: no chips, no error, no empty
    state.

    Off the event loop: the fan-out is a bounded number of small blocking reads
    and this route is polled by the dashboard. Every open is ``O_NONBLOCK``, so
    a FIFO left in the state root cannot park a worker from the shared default
    executor — the failure that would take the whole API's blocking work down
    with it.
    """
    payload = await asyncio.to_thread(annotations.read_annotations)
    return AnnotationsResponse(**payload)


@app.post("/sessions", response_model=Terminal, status_code=status.HTTP_201_CREATED)
async def create_session(
    request: Request,
    background_tasks: BackgroundTasks,
    agent_profile: str,
    provider: Optional[str] = None,
    session_name: Optional[str] = None,
    working_directory: Optional[str] = None,
    allowed_tools: Optional[str] = None,
    memory_manager: Optional[str] = None,
    env_vars: Optional[Dict[str, str]] = Body(default=None, embed=True),
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Terminal:
    """Create a new session with exactly one terminal.

    When ``memory_manager`` is truthy, a sidecar ``memory_manager`` terminal is
    spawned asynchronously in the same tmux session — provider initialization
    can take 15-30s and would otherwise block the HTTP response past the
    client's request timeout. The worker's first message may arrive before
    the curator reaches IDLE; ``get_curated_memory_context`` falls back to
    Phase 1 in that window.

    ``env_vars`` (request body, optional) is the operator-forwarded env map
    from ``cao launch --env``. It travels in the JSON body — not the query
    string — so values potentially containing secrets do not land in
    cao-server's HTTP access log. See issue #248.
    """
    try:
        if session_name is not None:
            # terminal_service.create_terminal prepends SESSION_PREFIX
            # ("cao-") if missing, so an API caller's 64-char valid name
            # would become 68 chars and fail downstream validation. Check
            # the *effective* prefixed value here so the rejection happens
            # at the boundary with a clear message.
            from cli_agent_orchestrator.constants import SESSION_PREFIX

            effective = (
                session_name
                if session_name.startswith(SESSION_PREFIX)
                else f"{SESSION_PREFIX}{session_name}"
            )
            validate_tmux_name(effective, "session_name")
        # Parse comma-separated allowed_tools string into list
        allowed_tools_list = allowed_tools.split(",") if allowed_tools else None

        result = await session_service.create_session(
            provider=provider,
            agent_profile=agent_profile,
            session_name=session_name,
            working_directory=working_directory,
            allowed_tools=allowed_tools_list,
            registry=get_plugin_registry(request),
            env_vars=env_vars,
        )

        if memory_manager and str(memory_manager).lower() in ("true", "1", "yes"):
            registry = get_plugin_registry(request)
            sidecar_provider = provider or DEFAULT_PROVIDER
            sidecar_session = result.session_name

            async def _spawn_sidecar() -> None:
                try:
                    from cli_agent_orchestrator.services import terminal_service

                    await terminal_service.create_terminal(
                        provider=sidecar_provider,
                        agent_profile="memory_manager",
                        session_name=sidecar_session,
                        working_directory=working_directory,
                        registry=registry,
                    )
                except Exception as e:
                    logger.warning(f"Failed to spawn memory_manager sidecar: {e}")

            background_tasks.add_task(_spawn_sidecar)

        return result

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create session: {str(e)}",
        )


@app.put("/sessions/{session_name}/env")
async def rebind_session_env(
    session_name: str,
    body: SessionEnvRebindRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, str]:
    """Replace env inherited by panes created after this point.

    A tmux pane already owns the environment it was launched with.  This
    endpoint therefore updates only the durable session map and cache; it
    never rewrites, restarts, or re-environments an existing pane.
    """
    try:
        validate_tmux_name(session_name, "session_name")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    try:
        exists = await asyncio.to_thread(get_backend().session_exists, session_name)
    except Exception as exc:  # noqa: BLE001 - an unreadable session is not absent
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="could not determine whether the session exists",
        ) from exc
    if not exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session not found")
    canonical = json.dumps(body.env_vars, sort_keys=True, separators=(",", ":"))
    observed_fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if observed_fingerprint != body.fingerprint:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="env fingerprint mismatch")
    try:
        await asyncio.to_thread(session_env.set_session_env, session_name, body.env_vars)
    except session_env.SessionEnvStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="session environment could not be persisted",
        ) from exc
    return {"fingerprint": observed_fingerprint}


@app.get("/sessions")
async def list_sessions() -> List[Dict]:
    try:
        return session_service.list_sessions()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list sessions: {str(e)}",
        )


@app.get("/sessions/{session_name}")
async def get_session(session_name: str) -> Dict:
    # Validate before entering the try block so a malformed name surfaces
    # as 400 instead of being mapped to 404 by the not-found handler below.
    try:
        validate_tmux_name(session_name, "session_name")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    try:
        return session_service.get_session(session_name)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get session: {str(e)}",
        )


@app.delete("/sessions/{session_name}")
async def delete_session(
    request: Request,
    session_name: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_ADMIN)),
) -> Dict:
    try:
        validate_tmux_name(session_name, "session_name")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    try:
        # Off the event loop: teardown is fully synchronous (tmux kills, FIFO
        # cleanup, DB writes) and has wedged the whole server — /health
        # included — when a FIFO operation stalled in the kernel (issue #382).
        # A worker thread bounds the blast radius of any future stall to this
        # one request.
        result = await asyncio.to_thread(
            session_service.delete_session, session_name, registry=get_plugin_registry(request)
        )
        return {"success": True, **result}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete session: {str(e)}",
        )


def _managed_launch_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, managed_launch.ManagedLaunchNotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    # Checked before the conflict branch, and 425 is used for nothing else
    # on this surface. A consumer keys "retry the same attempt" on exactly
    # this status plus a reason it recognises; sharing a code with any
    # other outcome would reintroduce the ambiguity that made a permanent
    # identity conflict look like a slow start.
    if isinstance(exc, managed_launch.ManagedLaunchNotReady):
        return HTTPException(
            status_code=status.HTTP_425_TOO_EARLY,
            detail={"reason": exc.reason, "message": str(exc)},
        )
    if isinstance(exc, managed_launch.ManagedLaunchConflict):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


@app.get("/managed-launch/capabilities")
async def managed_launch_capabilities(
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    """Return the exact versioned companion surface; absence means fail closed."""
    return {
        "protocol_version": MANAGED_LAUNCH_PROTOCOL_VERSION,
        "reservation_query": True,
        "reservation_reconcile": True,
        "no_task_launch": True,
        "generation_bound_readiness": True,
        "idempotent_task_admission": True,
        "generation_bound_negative": True,
        "generation_bound_cancel": True,
        "generation_bound_cleanup": True,
        "provider_submission_receipt": True,
        "provider_native_exact_session_receipts": True,
        "zero_task_route_attestation": True,
        "pinned_provider_executable": True,
        "reservation_bound_delivery_id": True,
        "provider_bound_bridge_environment": True,
        # Native GLM is a closed wrapper/inner route envelope.  A conductor
        # must negotiate this exact capability before sending route fields;
        # an older fork would otherwise ignore them and run Anthropic.
        "glm_route_envelope": True,
        "bridge_environment_inventory": "names-only-sha256",
        "post_allocation_bridge_failure_finalization": True,
        "launch_failure_evidence_schema": "cao-managed-bridge-launch-failure-v1",
        # The one transient bind refusal, advertised so a caller can
        # negotiate it instead of discovering it. Without this, a new
        # consumer that treats 425 as "retry the same attempt" would send
        # that behaviour at an old peer which uses 425 for nothing and
        # answers every not-yet-ready bind with a permanent 409 — the
        # retry never happens, and the launch fails on a contract neither
        # side agreed to. Reading these two keys lets it fail closed
        # before a reservation exists rather than mid-launch.
        #
        # Both values come from the *same constants the endpoint raises
        # and maps*, never restated here: an advertisement that could
        # drift from the behaviour it describes is worse than none, since
        # a consumer would have negotiated against a promise nothing
        # keeps. Absent keys mean an old peer with no typed refusal.
        #
        # Deliberately additive: no protocol-version bump, because an
        # existing consumer that ignores these keys behaves exactly as it
        # does today.
        "native_bind_not_ready_status": status.HTTP_425_TOO_EARLY,
        "native_bind_not_ready_reason": managed_launch.REASON_BIND_BRIDGE_NOT_DURABLY_READY,
        "trusted_project_root_providers": ["codex"],
        # The providers *this v1 bridged surface* has a readiness adapter
        # for, read from that adapter map rather than written out again.
        # The hand-written pair here was a second source of truth and it
        # drifted; deriving it means the list can only ever name providers
        # whose receipt kind exists.
        #
        # Deliberately NOT widened to include native-only providers. A
        # consumer gates a *bridged* launch on this key, so a provider
        # listed here without a v1 adapter would pass that gate on the
        # strength of this advertisement and be refused at preflight —
        # after a reservation exists. Native readiness is a different
        # question with its own answer below: `native_tui.providers`,
        # ANDed with `v2_launchable_execution_modes`. Two lists, two
        # questions; merging them would make one list wrong for whichever
        # caller read it next.
        "readiness_providers": sorted(managed_launch.READINESS_PROVIDERS),
        # The caller may name an execution mode on a reservation, and a
        # mode this surface cannot run is refused rather than silently
        # substituted.  A caller that omits the field is unaffected.
        "execution_mode_selection": True,
        # Read from the surface's own support set rather than restated
        # here, so the advertisement cannot drift from what actually
        # runs.  A mode appears the moment its launch branch exists and
        # not before — a consumer gating a native claim on this list is
        # therefore fail-closed for free, with no second source of truth
        # to keep in sync.
        "execution_modes": list(managed_launch.SUPPORTED_EXECUTION_MODES),
        # The v2 surface separates the two questions this key pair
        # answers.  It *reserves* any resolvable mode, because a
        # reservation is a durable statement of intent about a session
        # whose process the caller may start itself; it *launches* only
        # modes it has a real branch for.  A consumer that gated a native
        # launch on the reserve side would be reading the wrong
        # permission, so the launchable set is published separately and
        # read from the surface's own tuple for the same
        # no-second-source-of-truth reason as above.
        "v2_launchable_execution_modes": list(managed_launch_v2.LAUNCHABLE_EXECUTION_MODES),
        # Native-TUI support, per provider. Additive: the mode gate above
        # is unchanged and still decides whether native TUI can be
        # launched at all. This block answers the narrower question of
        # *which providers* have a real native adapter, which the flat
        # mode list cannot express — and a flat relaxation of that list
        # would have advertised native launch for providers with no
        # branch to run it.
        #
        # Both gates are required, and both come from this one response
        # so they cannot be read at two different moments: a caller may
        # route native TUI to a provider only when `native_tui` appears
        # in `v2_launchable_execution_modes` AND
        # `native_tui.providers[<provider>].supported` is true. An older
        # peer returns no `native_tui` key at all, which is the same
        # answer as unsupported and must be treated as a typed refusal
        # before any reservation is created.
        #
        # Keyed by canonical provider. `claude` is the executable name
        # and appears only as the `executable` field.
        "native_tui": managed_launch_v2.native_tui_capabilities(),
    }


@app.post("/managed-launch/reservations", status_code=status.HTTP_201_CREATED)
async def reserve_managed_launch(
    body: ManagedLaunchReserveRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    try:
        record, created = await asyncio.to_thread(managed_launch.reserve, body)
        return {"created": created, **record}
    except managed_launch.ManagedLaunchError as exc:
        raise _managed_launch_http_error(exc)


@app.post("/managed-launch/attest-route")
async def attest_managed_launch_route(
    body: ManagedLaunchRouteAttestRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    try:
        return await asyncio.to_thread(managed_launch.attest_route, body)
    except managed_launch.ManagedLaunchError as exc:
        raise _managed_launch_http_error(exc)


@app.get("/managed-launch/reservations/{reservation_id}")
async def get_managed_launch(
    reservation_id: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    try:
        return await asyncio.to_thread(managed_launch.get, reservation_id)
    except managed_launch.ManagedLaunchError as exc:
        raise _managed_launch_http_error(exc)


@app.post("/managed-launch/reservations/{reservation_id}/reconcile")
async def reconcile_managed_launch(
    reservation_id: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    try:
        return await asyncio.to_thread(managed_launch.reconcile, reservation_id)
    except managed_launch.ManagedLaunchError as exc:
        raise _managed_launch_http_error(exc)


@app.post("/managed-launch/reservations/{reservation_id}/launch")
async def launch_managed_generation(
    request: Request,
    reservation_id: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    try:
        return await managed_launch.launch_reserved(
            reservation_id, registry=get_plugin_registry(request)
        )
    except managed_launch.ManagedLaunchError as exc:
        raise _managed_launch_http_error(exc)


@app.post("/managed-launch/reservations/{reservation_id}/observations")
async def append_managed_launch_observation(
    reservation_id: str,
    body: ManagedLaunchObservationRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    try:
        return await asyncio.to_thread(managed_launch.append_observation, reservation_id, body)
    except managed_launch.ManagedLaunchError as exc:
        raise _managed_launch_http_error(exc)


async def _append_managed_terminal_evidence(
    reservation_id: str,
    body: ManagedLaunchObservationRequest,
    expected_kind: str,
) -> Dict[str, Any]:
    if body.kind != expected_kind:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"endpoint requires kind={expected_kind}",
        )
    try:
        return await asyncio.to_thread(managed_launch.append_observation, reservation_id, body)
    except managed_launch.ManagedLaunchError as exc:
        raise _managed_launch_http_error(exc)


@app.post("/managed-launch/reservations/{reservation_id}/negative")
async def record_managed_launch_negative(
    reservation_id: str,
    body: ManagedLaunchObservationRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    """Record generation-bound proof that task delivery never started."""
    return await _append_managed_terminal_evidence(reservation_id, body, "negative")


@app.post("/managed-launch/reservations/{reservation_id}/cancel")
async def cancel_managed_launch(
    reservation_id: str,
    body: ManagedLaunchObservationRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    """Cancel a pre-admission generation by immutable reservation identity."""
    return await _append_managed_terminal_evidence(reservation_id, body, "cancelled")


@app.post("/managed-launch/reservations/{reservation_id}/admit")
async def admit_managed_task(
    request: Request,
    reservation_id: str,
    body: ManagedLaunchAdmitRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    try:
        return await managed_launch.admit_reserved(
            reservation_id, body, registry=get_plugin_registry(request)
        )
    except managed_launch.ManagedLaunchError as exc:
        raise _managed_launch_http_error(exc)


@app.post("/managed-launch/reservations/{reservation_id}/cleanup")
async def cleanup_managed_generation(
    request: Request,
    reservation_id: str,
    body: ManagedLaunchCleanupRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    try:
        return await asyncio.to_thread(
            managed_launch.cleanup_reserved,
            reservation_id,
            body,
            registry=get_plugin_registry(request),
        )
    except managed_launch.ManagedLaunchError as exc:
        raise _managed_launch_http_error(exc)


# ---------------------------------------------------------------------------
# Recovery control-plane surfaces (Lane B foundation)
#
# These endpoints expose the v2 managed-launch seam, the W13 fence RPC, the
# conditional destructive endpoint, and the truthful capability negotiation
# surface.  Every containment-dependent path stays fail-closed while the
# deployed composition reports unproven — these surfaces advertise that
# honestly rather than claiming recovery authority that does not exist.
# ---------------------------------------------------------------------------


def _provider_version_output(executable_name: str) -> Optional[str]:
    """Live ``--version`` fact for one provider binary (None = unverified)."""
    import shutil
    import subprocess

    binary = shutil.which(executable_name)
    if not binary:
        return None
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.strip()


def _load_route_proofs() -> "tuple[Dict[str, Any], Dict[str, Any]]":
    """Provider-generated route receipts bound to live v2 reservations.

    The capability surface's ONLY route-receipt provenance: expectations
    come from the authority boundary (each live v2 reservation's journaled
    request — pinned generation/model/effort — and its delivery journal's
    journaled request digests); the loader authenticates each durable
    receipt (content address + generation-key HMAC + pinned provider
    version) and validates the typed contract against those expectations.
    Any failure yields no receipts (fail closed, no automated authority).
    """
    import json as _json

    from cli_agent_orchestrator.clients.database import (
        ManagedLaunchV2ReservationModel,
        SessionLocal,
    )
    from cli_agent_orchestrator.constants import CAO_HOME_DIR
    from cli_agent_orchestrator.services import route_receipts

    expected_routes: Dict[str, Dict[str, Any]] = {}
    expected_digests: Dict[str, Any] = {}
    with SessionLocal() as db:
        rows = (
            db.query(ManagedLaunchV2ReservationModel)
            .filter(ManagedLaunchV2ReservationModel.state.in_(("bound", "admitting", "admitted")))
            .order_by(ManagedLaunchV2ReservationModel.updated_at.desc())
            .all()
        )
    for row in rows:
        provider = route_receipts.capability_provider(str(row.provider))
        if provider is None or provider in expected_routes:
            continue
        try:
            request = _json.loads(str(row.request_json))
        except ValueError:
            continue
        model = request.get("expected_model")
        effort = request.get("expected_effort")
        if not isinstance(model, str) or not model or not isinstance(effort, str) or not effort:
            continue
        generation = str(row.generation)
        digests = route_receipts.journaled_request_digests(
            CAO_HOME_DIR / "managed-provider-sessions" / str(row.reservation_id),
            str(row.obligation_generation),
        )
        if not digests:
            continue
        expected_routes[provider] = {
            "generation": generation,
            "model": model,
            "effort": effort,
        }
        expected_digests[provider] = digests
    proofs = route_receipts.load_valid_route_proofs(
        state_dir=CAO_HOME_DIR / "recovery",
        expected_routes=expected_routes,
        expected_input_digests=expected_digests,
    )
    expectations = {
        provider: {"model": route["model"], "effort": route["effort"]}
        for provider, route in expected_routes.items()
    }
    return proofs, expectations


@app.get("/managed/recovery-capabilities")
async def managed_recovery_capabilities(
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    """The truthful negotiation surface: absence/unknown means fail closed."""
    from pathlib import Path

    from cli_agent_orchestrator.constants import CAO_HOME_DIR
    from cli_agent_orchestrator.services import kimi_acp_proof, recovery_capabilities

    # Version facts come from the live binaries — never hardcoded strings;
    # runtime drift removes the capability.  The Kimi identity claim
    # additionally requires the validated durable ACP new→kill→load proof.
    versions = {
        "codex": await asyncio.to_thread(_provider_version_output, "codex"),
        "claude": await asyncio.to_thread(_provider_version_output, "claude"),
        "kimi": await asyncio.to_thread(_provider_version_output, "kimi"),
    }
    kimi_proof = None
    import shutil

    kimi_bin = shutil.which("kimi")
    if kimi_bin and versions["kimi"]:
        try:
            # The proof binds the canonical absolute path recorded by
            # run_identity_proof; a PATH entry may spell the same binary
            # through a symlink (Homebrew: /opt/homebrew/bin/kimi), so
            # resolve to that same canonical identity before loading.  A
            # dangling symlink resolves to a nonexistent path and the load
            # fails closed below.
            kimi_proof = await asyncio.to_thread(
                kimi_acp_proof.load_valid_proof,
                state_dir=CAO_HOME_DIR / "recovery",
                kimi_binary=Path(os.path.realpath(kimi_bin)),
                version_output=versions["kimi"],
            )
        except Exception:  # noqa: BLE001 - absence of proof means disabled
            kimi_proof = None
    # Route authority derives only from provider-generated, authenticated,
    # durable route receipts bound to live v2 reservations — never from
    # caller-shaped dictionaries (cond-0069 closure).
    try:
        route_proofs, route_expectations = await asyncio.to_thread(_load_route_proofs)
    except Exception:  # noqa: BLE001 - absence of proof means disabled
        route_proofs, route_expectations = {}, {}
    return await asyncio.to_thread(
        recovery_capabilities.build_capabilities,
        provider_versions=versions,
        kimi_acp_proof=kimi_proof,
        route_proofs=route_proofs,
        route_expectations=route_expectations,
    )


@app.post("/managed-launch/v2/reservations", status_code=status.HTTP_201_CREATED)
async def reserve_managed_launch_v2(
    body: ManagedLaunchV2ReserveRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    try:
        record, created = await asyncio.to_thread(managed_launch_v2.reserve, body)
        return {"created": created, **record}
    except managed_launch.ManagedLaunchError as exc:
        raise _managed_launch_http_error(exc)


@app.get("/managed-launch/v2/reservations/{reservation_id}")
async def get_managed_launch_v2(
    reservation_id: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    try:
        return await asyncio.to_thread(managed_launch_v2.get, reservation_id)
    except managed_launch.ManagedLaunchError as exc:
        raise _managed_launch_http_error(exc)


@app.post("/managed-launch/v2/reservations/{reservation_id}/launch")
async def launch_managed_launch_v2(
    request: Request,
    reservation_id: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    try:
        return await managed_launch_v2.launch_reserved(
            reservation_id, registry=get_plugin_registry(request)
        )
    except managed_launch.ManagedLaunchError as exc:
        raise _managed_launch_http_error(exc)


@app.post("/managed-launch/v2/reservations/{reservation_id}/bind")
async def bind_managed_launch_v2(
    reservation_id: str,
    body: ManagedLaunchV2BindRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    try:
        return await asyncio.to_thread(managed_launch_v2.bind_native, reservation_id, body)
    except managed_launch.ManagedLaunchError as exc:
        raise _managed_launch_http_error(exc)


@app.post("/managed-launch/v2/reservations/{reservation_id}/admit")
async def admit_managed_launch_v2(
    request: Request,
    reservation_id: str,
    body: ManagedLaunchV2AdmitRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    from cli_agent_orchestrator.services import generation_fence

    try:
        return await managed_launch_v2.admit_reserved(
            reservation_id, body, registry=get_plugin_registry(request)
        )
    except generation_fence.FencedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except managed_launch.ManagedLaunchError as exc:
        raise _managed_launch_http_error(exc)


@app.post("/managed-launch/v2/reservations/{reservation_id}/resume")
async def resume_managed_launch_v2(
    reservation_id: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    # Resume admission stays fail-closed while the containment composition is
    # unproven; the fork seam journals the refusal fact.
    try:
        return await asyncio.to_thread(
            managed_launch_v2.attempt_resume, reservation_id, containment_proven=False
        )
    except managed_launch.ManagedLaunchError as exc:
        raise _managed_launch_http_error(exc)


@app.post("/managed-launch/v2/reservations/{reservation_id}/negative")
async def finalize_managed_launch_v2_negative(
    reservation_id: str,
    body: ManagedLaunchV2NegativeRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    """Finalize a v2 reservation whose failure is proven to have sent no bytes.

    Idempotent and re-drivable with zero task/provider I/O. The presence of
    this route is itself the recovery-supported signal: a peer old enough to
    lack it returns 404, which the caller reads as typed
    ``recovery-unsupported`` (preserve the run and its breaker) — never a v1
    fallback or a faked finalization.
    """
    try:
        return await asyncio.to_thread(managed_launch_v2.finalize_negative, reservation_id, body)
    except managed_launch.ManagedLaunchError as exc:
        raise _managed_launch_http_error(exc)


@app.post("/managed-launch/v2/reservations/{reservation_id}/reconcile")
async def reconcile_managed_launch_v2(
    reservation_id: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    """Read-only adoption of durable v2 facts; never launches, sends, or deletes."""
    try:
        return await asyncio.to_thread(managed_launch_v2.reconcile, reservation_id)
    except managed_launch.ManagedLaunchError as exc:
        raise _managed_launch_http_error(exc)


@app.post("/managed-launch/v2/reservations/{reservation_id}/cleanup")
async def cleanup_managed_launch_v2(
    request: Request,
    reservation_id: str,
    body: ManagedLaunchV2CleanupRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    """Tear down a finalized zero-byte v2 generation and record the cleanup proof.

    Drives the generation/session-bound terminal teardown (pane, provider
    process, fork-owned resources, v2 terminal row) before persisting the
    absorbing ``cleaned`` proof, using the live plugin registry so teardown
    events and resource cleanup stay coherent.
    """
    try:
        return await asyncio.to_thread(
            managed_launch_v2.cleanup,
            reservation_id,
            body,
            registry=get_plugin_registry(request),
        )
    except managed_launch.ManagedLaunchError as exc:
        raise _managed_launch_http_error(exc)


@app.post("/managed-launch/v2/fence")
async def install_managed_fence(
    body: ManagedV2FenceInstallRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    """The W13 fence-install RPC (conductor → fork).

    The fork resolves the generation's vintage and current fencing token
    from its own durable state — never from the caller — so a request
    naming a superseded or v1 generation gets the truthful outcome.
    """
    from cli_agent_orchestrator.constants import COMPANION_DIR
    from cli_agent_orchestrator.services import generation_fence, heartbeat_store

    def _install() -> Dict[str, Any]:
        import json as _json

        from cli_agent_orchestrator.clients import database

        vintage: Optional[str] = None
        superseded = False
        fencing_token_id = "unissued"
        row_terminal_id: Optional[str] = None
        with database.SessionLocal() as db:
            row = (
                db.query(database.ManagedLaunchV2ReservationModel)
                .filter(
                    database.ManagedLaunchV2ReservationModel.generation == body.terminal_generation
                )
                .first()
            )
            if row is not None:
                # Identity binding: the body's terminal, generation,
                # obligation, attempt, and the current fencing token must
                # all match the fork-owned reservation row BEFORE any
                # acknowledgement, and the row's terminal — never the
                # caller's — drives the state path.
                if body.terminal_id != row.terminal_id:
                    return {
                        "schema": "cao-w13-fence-resp-v1",
                        "outcome": "unknown-generation",
                        "fence_receipt_sha256": None,
                    }
                if body.obligation_generation != row.obligation_generation:
                    raise generation_fence.FenceRequestError(
                        "fence obligation_generation does not match the reservation row"
                    )
                binding = _json.loads(str(row.binding_json)) if row.binding_json else None
                if binding is not None and body.attempt_id != binding.get("attempt_id"):
                    raise generation_fence.FenceRequestError(
                        "fence attempt_id does not match the journaled native binding"
                    )
                vintage = "v2"
                row_terminal_id = str(row.terminal_id)
                fencing_record = heartbeat_store.current_fencing_record(
                    COMPANION_DIR, row_terminal_id
                )
                if fencing_record is not None:
                    token = (fencing_record.get("current_token") or {}).get("id")
                    if token:
                        fencing_token_id = token
                    # A fencing token issued for a *different* generation of
                    # the same terminal means this generation was superseded.
                    if fencing_record.get("generation") != body.terminal_generation:
                        superseded = True
            else:
                legacy = (
                    db.query(database.TerminalModel)
                    .filter(database.TerminalModel.generation == body.terminal_generation)
                    .first()
                )
                if legacy is not None:
                    vintage = "v1"
        if vintage is None:
            # The fork has no such generation in any store it owns.
            return {
                "schema": "cao-w13-fence-resp-v1",
                "outcome": "unknown-generation",
                "fence_receipt_sha256": None,
            }
        return generation_fence.install_fence(
            COMPANION_DIR,
            terminal_id=row_terminal_id if row_terminal_id is not None else body.terminal_id,
            generation=body.terminal_generation,
            vintage=vintage,
            superseded=superseded,
            request=body.model_dump(mode="json", by_alias=True),
            fencing_token_id=fencing_token_id,
        )

    try:
        return await asyncio.to_thread(_install)
    except generation_fence.FenceError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@app.post("/managed-launch/v2/park")
async def install_managed_park(
    body: ManagedV2ParkRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    """Install or reconcile the exact M3 parked-generation receipt.

    The durable receipt is written by the fork under the provider-byte fence
    lock.  This route never infers a successor from the caller: it resolves
    the v2 reservation and current fencing token from fork-owned records.
    """
    from cli_agent_orchestrator.constants import COMPANION_DIR
    from cli_agent_orchestrator.services import generation_fence, heartbeat_store

    def _install() -> Dict[str, Any]:
        from cli_agent_orchestrator.clients import database

        with database.SessionLocal() as db:
            row = (
                db.query(database.ManagedLaunchV2ReservationModel)
                .filter(
                    database.ManagedLaunchV2ReservationModel.reservation_id == body.reservation_id
                )
                .first()
            )
            if (
                row is None
                or str(row.terminal_id) != body.terminal_id
                or str(row.generation) != body.terminal_generation
            ):
                return {
                    "schema": generation_fence.PARK_RESPONSE_SCHEMA,
                    "outcome": generation_fence.OUTCOME_UNKNOWN_GENERATION,
                    "park_receipt": None,
                    "park_receipt_sha256": None,
                }
            if str(row.obligation_generation) != body.obligation_generation:
                raise generation_fence.ParkRequestError(
                    "park obligation_generation does not match the reservation row"
                )
            if str(row.task_id) != body.logical_task_id:
                raise generation_fence.ParkRequestError(
                    "park logical_task_id does not match the reservation row"
                )
            try:
                binding = json.loads(str(row.binding_json)) if row.binding_json else None
            except (TypeError, json.JSONDecodeError) as exc:
                raise generation_fence.ParkRequestError(
                    "park requires a readable immutable native binding"
                ) from exc
            if not isinstance(binding, dict):
                raise generation_fence.ParkRequestError(
                    "park requires an immutable bound reservation; unbound generations cannot park"
                )
            attempt_id = binding.get("attempt_id")
            fencing_token_id = binding.get("fencing_token_id")
            if not isinstance(attempt_id, str) or not attempt_id:
                raise generation_fence.ParkRequestError("park binding omitted immutable attempt_id")
            if not isinstance(fencing_token_id, str) or not fencing_token_id:
                raise generation_fence.ParkRequestError(
                    "park binding omitted immutable fencing_token_id"
                )
            if body.attempt_id != attempt_id:
                raise generation_fence.ParkRequestError(
                    "park attempt_id does not match the journaled native binding"
                )
        request = body.model_dump(mode="json", by_alias=True)

        def _terminalize_generationless_pending() -> None:
            # Pre-M3 generic rows have no exact-generation binding. They may
            # not drift onto a successor after this park receipt; preserve a
            # visible terminal state instead of silently retrying forever.
            with database.SessionLocal() as db:
                (
                    db.query(database.InboxModel)
                    .filter(
                        database.InboxModel.receiver_id == body.terminal_id,
                        database.InboxModel.status == MessageStatus.PENDING.value,
                        database.InboxModel.expected_receiver_generation.is_(None),
                    )
                    .update({database.InboxModel.status: MessageStatus.FAILED.value})
                )
                db.commit()

        # The terminal successor lock precedes the exact-generation lock in
        # every managed effect. Check an existing operation first so an exact
        # lost-response retry still adopts its receipt after a successor.
        with heartbeat_store.successor_critical_section(COMPANION_DIR, body.terminal_id):
            # Re-read the immutable reservation under the terminal lock. The
            # preflight above makes ordinary malformed calls cheap, but only
            # this locked read is the authority for a durable park receipt.
            with database.SessionLocal() as db:
                locked_row = (
                    db.query(database.ManagedLaunchV2ReservationModel)
                    .filter(
                        database.ManagedLaunchV2ReservationModel.reservation_id
                        == body.reservation_id
                    )
                    .first()
                )
                if (
                    locked_row is None
                    or str(locked_row.terminal_id) != body.terminal_id
                    or str(locked_row.generation) != body.terminal_generation
                ):
                    return {
                        "schema": generation_fence.PARK_RESPONSE_SCHEMA,
                        "outcome": generation_fence.OUTCOME_UNKNOWN_GENERATION,
                        "park_receipt": None,
                        "park_receipt_sha256": None,
                    }
                if (
                    str(locked_row.obligation_generation) != body.obligation_generation
                    or str(locked_row.task_id) != body.logical_task_id
                ):
                    raise generation_fence.ParkRequestError(
                        "park immutable reservation identity does not match"
                    )
                try:
                    locked_binding = (
                        json.loads(str(locked_row.binding_json))
                        if locked_row.binding_json
                        else None
                    )
                except (TypeError, json.JSONDecodeError) as exc:
                    raise generation_fence.ParkRequestError(
                        "park requires a readable immutable native binding"
                    ) from exc
                if not isinstance(locked_binding, dict):
                    raise generation_fence.ParkRequestError(
                        "park requires an immutable bound reservation; unbound generations cannot park"
                    )
                attempt_id = locked_binding.get("attempt_id")
                fencing_token_id = locked_binding.get("fencing_token_id")
                if (
                    not isinstance(attempt_id, str)
                    or not attempt_id
                    or not isinstance(fencing_token_id, str)
                    or not fencing_token_id
                    or body.attempt_id != attempt_id
                ):
                    raise generation_fence.ParkRequestError(
                        "park attempt/token does not match the immutable native binding"
                    )
            adopted = generation_fence.query_park(
                COMPANION_DIR,
                terminal_id=body.terminal_id,
                generation=body.terminal_generation,
                operation_id=body.operation_id,
            )
            if adopted["outcome"] != "not-found":
                receipt = adopted.get("park_receipt") or {}
                immutable = (
                    "operation_id",
                    "reservation_id",
                    "terminal_id",
                    "terminal_generation",
                    "logical_task_id",
                    "retained_round",
                    "obligation_generation",
                    "attempt_id",
                    "report_sha256",
                )
                if any(receipt.get(field) != request.get(field) for field in immutable):
                    raise generation_fence.ParkRequestError(
                        "park operation already exists with a different immutable intent"
                    )
                _terminalize_generationless_pending()
                return adopted
            try:
                heartbeat_store.assert_current_fencing_binding(
                    COMPANION_DIR,
                    terminal_id=body.terminal_id,
                    generation=body.terminal_generation,
                    attempt_id=attempt_id,
                    fencing_token_id=fencing_token_id,
                )
            except heartbeat_store.FencingRefused:
                return generation_fence.install_park(
                    COMPANION_DIR,
                    request=request,
                    fencing_token_id=fencing_token_id,
                    superseded=True,
                )
            result = generation_fence.install_park(
                COMPANION_DIR,
                request=request,
                fencing_token_id=fencing_token_id,
            )
            if result["outcome"] in {
                generation_fence.OUTCOME_FENCED,
                generation_fence.OUTCOME_ALREADY_FENCED,
            }:
                _terminalize_generationless_pending()
            return result

    try:
        return await asyncio.to_thread(_install)
    except generation_fence.FenceError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@app.get("/managed-launch/v2/park/{terminal_id}/{generation}/{operation_id}")
async def reconcile_managed_park(
    terminal_id: str,
    generation: str,
    operation_id: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    """Read-only exact M3 park reconciliation after response loss/restart."""
    from cli_agent_orchestrator.constants import COMPANION_DIR
    from cli_agent_orchestrator.services import generation_fence

    try:
        return await asyncio.to_thread(
            generation_fence.query_park,
            COMPANION_DIR,
            terminal_id=terminal_id,
            generation=generation,
            operation_id=operation_id,
        )
    except generation_fence.FenceError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@app.post("/managed/destructive")
async def execute_conditional_destructive(
    request: Request,
    body: ManagedDestructiveRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    """The single conditional endpoint for fork destructive effects."""
    from cli_agent_orchestrator.constants import COMPANION_DIR
    from cli_agent_orchestrator.services.destructive_endpoint import (
        DestructiveEndpoint,
        DestructiveError,
        DestructiveIntent,
        DestructiveRefused,
    )

    def _execute() -> Dict[str, Any]:
        from cli_agent_orchestrator.services.containment import ContainmentComposition

        record = managed_launch_v2.get(body.reservation_id)

        def _effect() -> str:
            deleted = terminal_service.delete_terminal(
                body.terminal_id,
                registry=get_plugin_registry(request),
                expected_generation=body.generation,
                expected_session=record["session_name"],
                # This teardown is the endpoint's effect: the heartbeat,
                # binding/fence, dual-exit, and containment decisions were
                # made and the single-use intent consumed by
                # DestructiveEndpoint.execute before this call.
                via_destructive_endpoint=True,
            )
            if not deleted:
                raise DestructiveError("teardown returned without a no-survivor proof")
            return "terminal-torn-down"

        # Containment truth comes from the live composition (unproven by
        # default), never from the request; the containment requirement is
        # derived server-side from the effect class, and the endpoint
        # fails closed on any non-ACTIVE heartbeat reading absent a
        # durable dual-exit proof.
        endpoint = DestructiveEndpoint(
            companion_dir=COMPANION_DIR,
            containment_proven=lambda: ContainmentComposition().status() == "proven",
        )
        return endpoint.execute(
            DestructiveIntent(
                intent_id=body.intent_id,
                kind=body.kind,
                terminal_id=body.terminal_id,
                generation=body.generation,
                reservation_id=body.reservation_id,
                attempt_id=body.attempt_id,
                fencing_token_id=body.fencing_token_id,
            ),
            _effect,
        )

    try:
        return await asyncio.to_thread(_execute)
    except DestructiveRefused as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except DestructiveError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    except managed_launch.ManagedLaunchError as exc:
        raise _managed_launch_http_error(exc)


@app.post(
    "/sessions/{session_name}/terminals",
    response_model=Terminal,
    status_code=status.HTTP_201_CREATED,
)
async def create_terminal_in_session(
    request: Request,
    session_name: str,
    agent_profile: str,
    provider: Optional[str] = None,
    working_directory: Optional[str] = None,
    allowed_tools: Optional[str] = None,
    caller_id: Optional[TerminalId] = None,
    defer_init: bool = False,
    body: Optional[CreateTerminalBody] = None,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Terminal:
    """Create additional terminal in existing session.

    ``defer_init=true``: return as soon as the tmux window is created and the
    terminal is registered in the DB, without waiting for the CLI provider to
    reach IDLE. Provider initialization runs as a background task; when
    ``body.initial_message`` is also provided it is sent to the terminal via
    the same task once init completes. Used by the MCP `assign` tool to keep
    tool-call latency well under kiro-cli 2.11's ~60s per-tool client
    timeout, and to allow multiple concurrent assigns to run their init
    phases in parallel.

    The message payload lives in the JSON body (``initial_message``,
    ``initial_message_orchestration_type``) rather than query params so prompt
    content isn't exposed in HTTP access logs and isn't subject to URL-length
    limits.
    """
    try:
        validate_tmux_name(session_name, "session_name")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    try:
        if provider is None:
            resolved_provider = resolve_provider(agent_profile, fallback_provider="kiro_cli")
        else:
            resolved_provider = provider

        # Parse comma-separated allowed_tools string into list
        allowed_tools_list = allowed_tools.split(",") if allowed_tools else None

        initial_message = body.initial_message if body else None

        # The initial-message payload is only delivered on the deferred-init
        # path; create_terminal() ignores it otherwise. Reject it explicitly
        # when defer_init is false rather than silently dropping it, which would
        # surface later as a "worker never received task" mystery.
        if (
            not defer_init
            and body
            and (
                body.initial_message is not None
                or body.initial_message_orchestration_type is not None
            )
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "initial_message / initial_message_orchestration_type require "
                    "defer_init=true; they are not delivered on the synchronous path"
                ),
            )

        # Deferred init only makes sense when a message will follow — we
        # still accept the flag alone (no message) for future non-assign uses.
        orch_type = None
        if body and body.initial_message_orchestration_type:
            try:
                orch_type = OrchestrationType(body.initial_message_orchestration_type)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"invalid initial_message_orchestration_type: "
                        f"{body.initial_message_orchestration_type!r}"
                    ),
                )

        # Forward managed-launch model/effort overrides so native-CLI providers
        # (e.g. muse_cli, kimi_cli) launch the caller-selected model — one
        # profile can then target either tier (muse-spark-1.2 vs
        # muse-spark-1.2-contributor) at spawn time.
        expected_model = body.expected_model if body else None
        expected_effort = body.expected_effort if body else None

        result = await terminal_service.create_terminal(
            provider=resolved_provider,
            agent_profile=agent_profile,
            session_name=session_name,
            new_session=False,
            working_directory=working_directory,
            allowed_tools=allowed_tools_list,
            registry=get_plugin_registry(request),
            caller_id=caller_id,
            defer_init=defer_init,
            initial_message=initial_message,
            initial_message_orchestration_type=orch_type,
            expected_model=expected_model,
            expected_effort=expected_effort,
        )
        return result
    except HTTPException:
        # Deliberate 4xx (e.g. the initial_message/defer_init guard, invalid
        # orchestration_type) — propagate as-is instead of masking as a 500.
        raise
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create terminal: {str(e)}",
        )


@app.get("/sessions/{session_name}/terminals")
async def list_terminals_in_session(session_name: str) -> List[Dict]:
    """List all terminals in a session, as the one shared projection.

    The CLI and the dashboard read this same shape, which is the point:
    they used to answer the question from different queries and disagree
    about which terminals existed and what state they were in. Each row
    carries its ``protocol_vintage`` and an observed ``lifecycle_state``,
    so a card can say "superseded" instead of showing a deleted window as
    a worker in an unknown state.
    """
    try:
        validate_tmux_name(session_name, "session_name")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    try:
        from cli_agent_orchestrator.services import terminal_projection

        # Off-loop: the projection enumerates tmux once, which is a
        # blocking subprocess, and this endpoint is polled by both views.
        return await asyncio.to_thread(terminal_projection.project_session, session_name)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list terminals: {str(e)}",
        )


def _identity_for_verification(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """One identity shape, whichever store the row came from.

    The v2 terminal store prefixes its newer identity columns ``v2_``
    because the vintage receipt requires unique bare column names across
    the two tables. Every checker below this point speaks the canonical
    names, so the prefix is stripped here rather than teaching each of them
    about the storage detail — a checker that silently found no
    ``session_id`` on a v2 row would grade a complete identity as partial
    and refuse a healthy worker.
    """
    normalized = dict(metadata)
    for field in ("session_id", "pane_pid", "native_session_id"):
        prefixed = f"v2_{field}"
        if normalized.get(field) is None and prefixed in normalized:
            normalized[field] = normalized[prefixed]
    return normalized


def _projected_terminal(terminal_id: str) -> Dict[str, Any]:
    """One terminal, from the same projection both human views read.

    Sourced here rather than from ``terminal_service.get_terminal`` because
    this route is the per-terminal answer for almost everything — the
    conductor's report, steer and control-input commands, supervisor
    resolution, and the card ``cao session status`` prints after selecting
    the conductor through the projection. While it returned the raw row,
    a dead terminal read ``dead`` in the listing and ``unknown`` on its own
    card: the phantom-unknown card, one endpoint over.

    ``status`` keeps its provider meaning for a live pane, because machine
    orchestration polls this route waiting on provider status and would
    otherwise never see the value it waits for. Only a row whose identity
    does not resolve reports its lifecycle there, which is exactly the case
    where there is no provider state to report.

    Falls back to the unprojected row when the projection has no answer, so
    a terminal that exists but cannot be observed is still readable rather
    than becoming a 404.
    """
    projected = terminal_projection.project_terminal(terminal_id)
    if projected is None:
        return terminal_service.get_terminal(terminal_id)
    # ``id``/``name``/``session_name`` are the shape this response model has
    # always used; the projection carries the canonical spellings alongside
    # its back-compat keys, so map rather than rename either side.
    return {
        **projected,
        "id": projected.get("terminal_id") or terminal_id,
        "name": projected.get("tmux_window"),
        "session_name": projected.get("tmux_session"),
    }


@app.get("/terminals/{terminal_id}", response_model=Terminal)
async def get_terminal(terminal_id: TerminalId) -> Terminal:
    try:
        # get_terminal reads status_monitor.get_status(), which for a
        # PROCESSING terminal does a fresh detection that can shell out to
        # tmux (blocking subprocess). This endpoint is polled heavily by
        # wait_until_terminal_status, so run it off the loop to keep the
        # server responsive under concurrent orchestration.
        terminal = await asyncio.to_thread(_projected_terminal, terminal_id)
        return Terminal(**terminal)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except TerminalNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get terminal: {str(e)}",
        )


# --- Structured companion surfaces (final conformance §20.2f P1-7/P1-10) ------
#
# Observation/receipt only: the §17.2 user-prompt lifecycle, the §18.2
# refusal receipt, the §18.9 per-turn route identity, and the §19.5
# message-turn acknowledgement. Every surface is bound to the terminal's
# exact live generation; a stale/wrong-generation or absent record is a 204
# (no observation), never stale data. Unknown/unsupported providers simply
# never produce records, so they fail closed to 204 as well.


def _live_terminal_generation(terminal_id: str) -> Optional[str]:
    metadata = terminal_service.get_terminal_metadata(terminal_id)
    if not isinstance(metadata, dict):
        return None
    return metadata.get("generation")


@app.get("/terminals/{terminal_id}/user-prompt")
async def get_terminal_user_prompt(terminal_id: TerminalId):
    """The pending provider-native structured user prompt ``{prompt_id, text,
    choices[]}`` for the terminal's exact live generation, or 204."""
    prompt = companion_receipts.get_prompt(terminal_id, _live_terminal_generation(terminal_id))
    if prompt is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return prompt


@app.get("/terminals/{terminal_id}/refusal")
async def get_terminal_refusal(terminal_id: TerminalId):
    """The pending provider-native structured refusal receipt ``{refusal_id,
    identity, turn_id, generation}`` for the exact live generation, or 204."""
    refusal = companion_receipts.get_refusal(terminal_id, _live_terminal_generation(terminal_id))
    if refusal is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return refusal


@app.get("/terminals/{terminal_id}/route")
async def get_terminal_route(terminal_id: TerminalId):
    """The provider-native per-turn route receipt ``{provider, model, effort,
    generation, receipt_id, turn_id}`` for the exact live generation, or 204."""
    route = companion_receipts.get_route(terminal_id, _live_terminal_generation(terminal_id))
    if route is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return route


@app.get("/terminals/{terminal_id}/inbox/messages/{message_id}/turn-receipt")
async def get_inbox_message_turn_receipt(terminal_id: TerminalId, message_id: str):
    """The provider-native ``terminal_queued → submitted`` acknowledgement for
    one exact inbox message, bound to message id, the receiver's exact live
    generation, and the provider session/turn — or 204 when no provider-native
    submission has been recorded (an ordinary inbox ``delivered``/terminal
    paste is never an acknowledgement).

    For an unmanaged receiver with no provider-native ack, the durable wake
    receipt (``source: status-transition``) is served once terminal — a wake
    receipt, not a model-consumption proof.  The managed companion ack is
    checked first and preferred; a still-``watching`` wake receipt answers 204
    until it finalizes, so an open obligation stays observable rather than
    falsely closed."""
    ack = companion_receipts.get_message_ack(
        terminal_id, _live_terminal_generation(terminal_id), message_id
    )
    if ack is not None:
        return ack
    wake = wake_receipts.get(terminal_id, message_id)
    if wake is not None and wake.get("state") in (
        wake_receipts.WAKE_CONFIRMED,
        wake_receipts.WAKE_UNCONFIRMED,
    ):
        return wake
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/terminals/{terminal_id}/memory-context")
async def get_terminal_memory_context(terminal_id: TerminalId):
    """Return the CAO memory context block for a terminal as plain text.

    Used by the Kiro AgentSpawn hook to inject memory into agent context.
    Returns empty 200 if no memories exist for this terminal.
    """
    from fastapi.responses import PlainTextResponse

    try:
        from cli_agent_orchestrator.services.memory_service import MemoryService

        svc = MemoryService()
        context = svc.get_memory_context_for_terminal(terminal_id)
        return PlainTextResponse(content=context)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get memory context: {str(e)}",
        )


@app.get("/terminals/{terminal_id}/working-directory", response_model=WorkingDirectoryResponse)
async def get_terminal_working_directory(terminal_id: TerminalId) -> WorkingDirectoryResponse:
    """Get the current working directory of a terminal's pane."""
    try:
        working_directory = terminal_service.get_working_directory(terminal_id)
        return WorkingDirectoryResponse(working_directory=working_directory)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get working directory: {str(e)}",
        )


@app.get("/terminals/{terminal_id}/managed-control")
async def get_managed_terminal_control(
    terminal_id: TerminalId,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    """Identify the exact managed generation backing a human control surface."""
    identity = await asyncio.to_thread(managed_launch.managed_control_identity, terminal_id)
    return {"managed": identity is not None, **(identity or {})}


@app.post("/terminals/{terminal_id}/managed-operations")
async def begin_managed_terminal_operation(
    terminal_id: TerminalId,
    body: ManagedSessionOperationRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    allowed = {
        "follow-up",
        "cancel",
        "route-query",
        "route-set",
        "compact",
        "resume",
        "resume-status",
    }
    if body.action not in allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unsupported managed-session action {body.action!r}",
        )
    payload = {
        key: value
        for key, value in {
            "message": body.message,
            "config_id": body.config_id,
            "value": body.value,
            "instruction": body.instruction,
        }.items()
        if value is not None
    }
    try:
        receipt = await asyncio.to_thread(
            managed_launch.begin_managed_session_operation,
            terminal_id,
            operation_id=body.operation_id,
            action=body.action,
            generation=body.generation,
            timeout=16 * 60 if body.action == "compact" else 45.0,
            **payload,
        )
    except managed_launch.ManagedLaunchNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except managed_launch.ManagedLaunchConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except managed_launch.ManagedLaunchError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    return {"success": receipt.get("state") in {"accepted", "completed"}, "receipt": receipt}


@app.get("/terminals/{terminal_id}/managed-operations/{operation_id}")
async def query_managed_terminal_operation(
    terminal_id: TerminalId,
    operation_id: str,
    generation: Optional[str] = None,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    try:
        receipt = await asyncio.to_thread(
            managed_launch.query_managed_session_operation,
            terminal_id,
            operation_id=operation_id,
            generation=generation,
        )
    except managed_launch.ManagedLaunchNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except managed_launch.ManagedLaunchConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except managed_launch.ManagedLaunchError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    return {"receipt": receipt}


# --- Identity-bound control input --------------------------------------------
#
# A control is a literal line plus one explicit Enter, typed into exactly
# one pane by exactly one writer, or refused with a typed reason that says
# what is provable.  It is a separate surface from ordinary delivery
# because it makes a promise ordinary delivery cannot: no bracketed-paste
# framing under any condition, identity re-verified under the write lease,
# and at-most-once across a lost response.
#
# There is deliberately no fallback anywhere in this surface.  A caller
# that cannot get a control delivered here is told so; it is never quietly
# downgraded to a paste or to raw keys, because a control the operator
# believes was delivered once must not arrive twice or as different bytes.


@app.get("/control-input/capabilities")
async def get_control_input_capabilities() -> Dict[str, Any]:
    """What this server's control-input surface implements.

    Exists because support cannot be discovered by trying: a probe that
    succeeds has already typed into somebody's composer.  A server that
    predates this protocol has no such route, and the resulting 404 is
    the signal a client resolves to a typed ``unsupported``.
    """
    return control_input_service.control_input_capabilities()


@app.get("/control-input/{control_id}")
async def get_control_input_result(
    control_id: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
) -> JSONResponse:
    """What happened to one control, for a caller whose reply was lost.

    Keyed by control id alone rather than by terminal: the id is the
    journal's key, and a terminal-scoped lookup would answer "nothing was
    written" to a caller that named the wrong terminal for a control that
    was in fact written.
    """
    try:
        result = await asyncio.to_thread(control_input_service.lookup_control_input, control_id)
    except control_input_service.ControlInputRequestInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return JSONResponse(status_code=result.http_status, content=result.as_response())


@app.get("/terminals/{terminal_id}/control-identity")
async def get_terminal_control_identity(
    terminal_id: TerminalId,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    """This server's own view of a terminal's control identity.

    A caller cannot bind a control to a pane it has never been told
    about.  This is where it learns the declarable identity — including
    the pane birth id — so its ``expected_identity`` can name the target
    it actually means rather than trusting the server to pick.
    """
    resolved = await asyncio.to_thread(control_input_service.resolve_control_identity, terminal_id)
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no terminal {terminal_id!r} is known to this server",
        )
    body = resolved.as_dict()
    # The discovery block (§3): a conductor that needs v2 reads this before
    # sending a chord, so a v2 request against a v1-only server fails closed
    # with typed ``unsupported`` and zero bytes rather than silently
    # delivering text without the chord.  The resolved identity is passed so
    # the block carries this terminal's build-exact provider controls (the
    # §3.5 send authority) and its composer-guard availability (§4.1).
    body["control_input"] = control_input_service.control_input_capability_block(resolved)
    return body


class ComposerObservationRequest(BaseModel):
    """Expected composer content for a read-only observation."""

    expected_text_sha256: str = Field(
        ...,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
        description="SHA-256 of the expected composer text, 64 lowercase hex chars",
    )
    expected_text_bytes: int = Field(
        ...,
        gt=0,
        description="Byte length of the expected composer text, positive integer",
    )


@app.get("/terminals/{terminal_id}/composer-observation")
async def get_terminal_composer_observation(
    terminal_id: TerminalId,
    params: Annotated[ComposerObservationRequest, Query()],
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
) -> JSONResponse:
    """Read whether the exact expected text is resting in the provider composer.

    A read-only, identity-bound observation of the pinned composer region.
    Returns ``observed=true`` only when the exact expected digest and byte
    length are proven in the pinned composer region and submission is not
    proven to have occurred.  Never returns raw composer text.
    """
    try:
        result = await asyncio.to_thread(
            control_input_service.observe_composer,
            terminal_id,
            params.expected_text_sha256,
            params.expected_text_bytes,
        )
    except control_input_service.ComposerObservationRequestInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except Exception as exc:
        logger.exception("composer-observation failed unexpectedly")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "protocol": control_input_service.COMPOSER_OBSERVATION_PROTOCOL,
                "observed": False,
                "error": f"unexpected failure: {type(exc).__name__}",
            },
        )
    return JSONResponse(status_code=result.http_status, content=result.as_response())


class ParseNotationRequest(BaseModel):
    """One macro notation string to resolve through the pinned grammar."""

    notation: str


@app.post("/macros/parse-notation")
async def parse_macro_notation(
    body: ParseNotationRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
) -> JSONResponse:
    """The server-authoritative macro-notation parse (§5.3).

    The one authority for the notation grammar — Lane B's TypeScript
    preview is tested against the same golden vectors so the two cannot
    drift into spelling the same macro two ways.  Answers the resolved v3
    event array and its canonical preview, or ``422`` with the parse
    errors (each carrying a 0-based offset and a message).  This route
    only parses: it persists nothing and writes nothing to any pane.
    """
    result = macro_notation.parse_notation(body.notation)
    if result.errors:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "errors": [
                    {"offset": error.offset, "message": error.message} for error in result.errors
                ]
            },
        )
    return JSONResponse(content={"events": result.events, "preview": result.preview})


def _stated_enter(body: ControlInputRequest) -> Any:
    """The ``enter`` argument for the service, with one JSON distinction restored.

    Pydantic parses an omitted ``enter`` and an explicit ``"enter": null``
    to the same ``None``, but they are different requests on the v1/v2
    wire: the omission carries the v1 default (submit), while the explicit
    null failed validation at F1 (``enter`` was a non-Optional bool) and
    must keep failing rather than silently becoming ``enter=true``.  Raw
    field presence is the only place the two can still be told apart, so
    the edge translates the stated-null case to a marker the service
    refuses as a shape error.  Beside ``events`` a stated ``enter`` — null
    included — is handed through the same marker, so the v3 either/or rule
    refuses it as the ambiguous intent it is.
    """
    if body.enter is None and "enter" in body.model_fields_set:
        return control_input_service.ENTER_EXPLICIT_NULL
    return body.enter


@app.post("/terminals/{terminal_id}/control-input")
async def send_terminal_control_input(
    terminal_id: TerminalId,
    body: ControlInputRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> JSONResponse:
    """Type one control into one pane, once, or say truthfully why not.

    Answers 200 with a typed outcome for every terminal-level failure,
    including an unknown terminal.  A 404 here is reserved for the route
    being absent altogether, which is what an older server returns and
    what a client must read as ``unsupported`` rather than as "wrong
    terminal" — the two demand opposite actions.
    """
    try:
        result = await asyncio.to_thread(
            partial(
                control_input_service.deliver_control_input,
                terminal_id,
                control_id=body.control_id,
                text=body.text,
                enter=_stated_enter(body),
                expected_identity=body.expected_identity,
                request_digest=body.request_digest,
                protocol=body.protocol,
                chord=body.chord,
                events=body.events,
                payload_class=body.payload_class,
                lease_timeout=body.lease_timeout,
            )
        )
    except control_input_service.ControlInputRequestInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return JSONResponse(status_code=result.http_status, content=result.as_response())


@app.post("/terminals/{terminal_id}/operator-message")
async def send_terminal_operator_message(
    terminal_id: TerminalId,
    body: OperatorMessageRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> JSONResponse:
    """Submit one text+image operator message, at most once, or say why not.

    The Lane C typed operation (design §8.3): the same 200-with-typed-
    outcome discipline as control-input — a 404 here is reserved for the
    route being absent altogether (an older server), which a client reads
    as ``unsupported``.  Every terminal-level failure — unknown terminal,
    identity drift, busy pane, unproven build, attachment not ready — is
    a typed outcome.  A lost response is resolved by one exact-id GET,
    never by resending.
    """
    try:
        result = await asyncio.to_thread(
            partial(
                operator_message_service.submit_operator_message,
                terminal_id,
                operation_id=body.operation_id,
                text=body.text,
                attachments=body.attachments,
                token_map=body.token_map,
                expected_identity=body.expected_identity,
                lease_timeout=body.lease_timeout,
            )
        )
    except operator_message_service.OperatorMessageRequestInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return JSONResponse(status_code=result.http_status, content=result.as_response())


@app.get("/operator-message/{operation_id}")
async def get_operator_message_result(
    operation_id: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
) -> JSONResponse:
    """What happened to one operator message, for a lost reply.

    Keyed by operation id alone, mirroring the control-input reconcile:
    the journaled record is the answer, and a message is never re-sent
    automatically.  The id spans the two per-provider operation stores
    (OD6); an unreadable store is answered as the unknown it is, never as
    "proven absent".
    """
    try:
        result = await asyncio.to_thread(
            operator_message_service.reconcile_operator_message, operation_id
        )
    except operator_message_service.OperatorMessageRequestInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return JSONResponse(status_code=result.http_status, content=result.as_response())


@app.post("/terminals/{terminal_id}/attachments", status_code=201)
async def upload_terminal_attachment(
    terminal_id: TerminalId,
    file: UploadFile = File(...),
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> JSONResponse:
    """Stage one image attachment for one terminal (design §8.4).

    Content is validated, never the filename: magic-byte sniff plus
    structure/dimension decode, then the provider's advertised
    ``image.formats`` allowlist.  Over-limit, corrupt, or unproven-format
    uploads answer 422 with a typed refusal body and leave a durable
    ``failed`` record; nothing is ever half-written.
    """
    content = await file.read(image_attachments.MAX_IMAGE_BYTES + 1)
    try:
        record = await asyncio.to_thread(
            partial(
                operator_message_service.upload_attachment,
                terminal_id,
                display_filename=file.filename,
                content=content,
            )
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except operator_message_service.AttachmentRefusal as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.as_response())
    return JSONResponse(status_code=201, content={"attachment": record})


@app.get("/terminals/{terminal_id}/attachments")
async def list_terminal_attachments(
    terminal_id: TerminalId,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    """The terminal's live image-attachment records (``removed`` are gone)."""
    try:
        records = await asyncio.to_thread(
            operator_message_service.list_terminal_attachments, terminal_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {"attachments": records}


@app.delete("/terminals/{terminal_id}/attachments/{attachment_id}")
async def delete_terminal_attachment(
    terminal_id: TerminalId,
    attachment_id: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> JSONResponse:
    """Remove one attachment and its staged file.

    A ``submitted`` attachment is retained read-only for its TTL so the
    provider can still read the staged path mid-turn (§8.4); deleting one
    is a 409 with the typed explanation, not a silent delete.
    """
    try:
        record = await asyncio.to_thread(
            operator_message_service.delete_terminal_attachment,
            terminal_id,
            attachment_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except operator_message_service.AttachmentRefusal as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.as_response())
    return JSONResponse(content={"deleted": True, "attachment": record})


@app.post("/terminals/{terminal_id}/input")
async def send_terminal_input(
    request: Request,
    terminal_id: TerminalId,
    message: str,
    operation_id: Optional[str] = None,
    sender_id: Optional[str] = None,
    orchestration_type: Optional[OrchestrationType] = None,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    try:
        managed_identity = await asyncio.to_thread(
            managed_launch.managed_control_identity, terminal_id
        )
        if managed_identity is not None:
            if not operation_id:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        "managed input requires a caller-retained operation_id; "
                        "use the managed-operations endpoint"
                    ),
                )
            receipt = await asyncio.to_thread(
                managed_launch.begin_managed_session_operation,
                terminal_id,
                operation_id=operation_id,
                action="follow-up",
                generation=managed_identity["generation"],
                message=message,
            )
            if receipt.get("state") not in {"accepted", "completed"}:
                raise TerminalInputBlockedError(
                    f"managed follow-up {receipt.get('state')}: "
                    f"{receipt.get('reason_code') or receipt.get('reason_detail') or 'not accepted'}"
                )
            return {"success": True, "managed": True, "receipt": receipt}
        # send_input is blocking tmux I/O (bracketed paste + key sends). Run it
        # off the event loop so a slow tmux call can't freeze every other
        # request — including /health and concurrent assign/handoff. Same
        # hazard class as issue #382 (only fixed for DELETE /sessions there).
        success = await asyncio.to_thread(
            terminal_service.send_input,
            terminal_id,
            message,
            registry=get_plugin_registry(request),
            sender_id=sender_id,
            orchestration_type=orchestration_type,
        )
        return {"success": success}
    except HTTPException:
        raise
    except TerminalInputBlockedError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to send input: {str(e)}",
        )


@app.post("/terminals/{terminal_id}/key")
async def send_terminal_key(
    terminal_id: TerminalId,
    key: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Send a tmux special key to a terminal."""
    if not TMUX_KEY_PATTERN.fullmatch(key):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Invalid tmux key name. Allowed keys are arrow keys, Enter, Tab, "
                "Escape, Space, single alphanumeric keys, and C-/M-/S- modifier combos."
            ),
        )

    try:
        managed_identity = await asyncio.to_thread(
            managed_launch.managed_control_identity, terminal_id
        )
        if managed_identity is not None:
            raise TerminalInputBlockedError(
                "raw tmux keys are disabled for managed provider sessions; "
                "use the generation-bound managed controls"
            )
        # Blocking tmux send-keys — off the loop.
        success = await asyncio.to_thread(terminal_service.send_special_key, terminal_id, key)
        return {"success": success}
    except TerminalInputBlockedError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to send key: {str(e)}",
        )


@app.get("/terminals/{terminal_id}/output", response_model=TerminalOutputResponse)
async def get_terminal_output(
    terminal_id: TerminalId, mode: OutputMode = OutputMode.FULL
) -> TerminalOutputResponse:
    try:
        # get_output does a blocking tmux capture-pane plus provider regex
        # extraction over the scrollback — run it off the loop so a large
        # transcript can't stall the whole server.
        output = await asyncio.to_thread(terminal_service.get_output, terminal_id, mode)
        return TerminalOutputResponse(output=output, mode=mode)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get output: {str(e)}",
        )


@app.post("/terminals/{terminal_id}/exit")
async def exit_terminal(
    terminal_id: TerminalId,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Send provider-specific exit command to terminal."""
    try:
        identity = await asyncio.to_thread(managed_launch.managed_control_identity, terminal_id)
        if identity is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "raw CLI exit is disabled for managed provider sessions; "
                    "use exact-generation managed cancel or lifecycle cleanup"
                ),
            )
        # Blocking tmux I/O — off the loop.
        await asyncio.to_thread(terminal_service.exit_terminal_cli, terminal_id)
        return {"success": True}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to exit terminal: {str(e)}",
        )


@app.post(
    TERMINALS_RUN_STEP_ROUTE,
    response_model=RunStepResponse,
    summary="Run one agent step (shared substrate)",
    description=(
        "Failure contract: a non-2xx body is a structured object "
        "`{message, kind, terminal_id}`. **`kind` is authoritative** — "
        '`kind="error"` means the worker CRASHED (terminal reached ERROR), '
        '`kind="timeout"` means it RAN LONG. The HTTP status mirrors `kind` '
        "(502 = crashed, 504 = ran long) for transport-layer consumers, but a "
        "caller MUST branch on `kind`, not the status code. `terminal_id` names "
        "the live terminal (read it as a field; never regex-scrape `message`)."
    ),
)
async def run_step(
    request: Request,
    body: RunStepRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> RunStepResponse:
    """Run a single agent step through the shared substrate (N0, #312).

    This is the combined server-side endpoint both step callers converge on:
    the handoff MCP client reaches it over HTTP (one call replacing its former
    six granular round-trips); the run engine (N5) calls ``run_agent_step``
    directly in-process and never round-trips here (single-seam rule, ADR-3).

    The handler body is ``await run_agent_step(...)``. Domain failures from the
    substrate are mapped to ``HTTPException`` at this boundary (project Mandated
    boundary-map rule).

    Failure contract (the future engine caller depends on this, so it is spelled
    out, not just inferable from the handler):

    - A failed step returns a STRUCTURED detail object
      ``{"message": str, "kind": "timeout"|"error", "terminal_id": str|None}``.
    - ``kind`` is the AUTHORITATIVE discriminator. ``kind="error"`` => the worker
      CRASHED (the terminal reached ``TerminalStatus.ERROR``); ``kind="timeout"``
      => the worker RAN LONG (readiness/completion wait elapsed). The HTTP status
      is derived FROM ``kind`` (``error`` -> 502 Bad Gateway, ``timeout`` -> 504
      Gateway Timeout) as a convenience for transport-layer consumers — a client
      that can read the body MUST branch on ``kind``, not the status code.
    - ``terminal_id`` names the live terminal the step ran on (when known) so a
      caller can report/clean it up without regex-scraping ``message``.
    - A bad terminal reference -> 404; any other failure -> 500 (plain-string
      detail, no ``kind`` — these are not step-execution outcomes).

    The plugin registry is threaded so teardown's ``post_kill_terminal`` hooks
    fire (parity with the DELETE endpoint).
    """
    # BR-31: for a script-tier run-step call, record the created terminal into the
    # shared ScriptRunRecord's step_states AT creation time, so U4's orphan sweep
    # can tear it down if the subprocess dies mid-call. No-op for YAML/handoff
    # callers (no run/step env or no script record in the registry).
    from cli_agent_orchestrator.services import workflow_service
    from cli_agent_orchestrator.services.script_runner import (
        make_step_terminal_recorder,
        record_step_completion,
    )
    from cli_agent_orchestrator.services.workflow_service import StaleGenerationError

    on_terminal_created = make_step_terminal_recorder(body.env_vars)
    # BR-31 companion: the recorder above seeds a step RUNNING at terminal
    # creation, but nothing transitions it — so a completed script run reports
    # every step frozen at running/attempts=0/output=null. ``on_step_settled``
    # transitions the shared ScriptRunRecord's step RUNNING->COMPLETED on success
    # (or ->FAILED on a StepExecutionError), matching the YAML tier. No-op for
    # YAML/handoff callers (same guard as the recorder). Settling is best-effort:
    # it must never turn a successful step into an HTTP error, so ``_settle_step``
    # swallows + logs any bookkeeping failure.
    on_step_settled = record_step_completion(
        body.env_vars, provider=body.provider, agent=body.agent, prompt=body.prompt
    )

    def _settle_step(terminal_id: Optional[str], error: Optional[str]) -> None:
        if on_step_settled is None:
            return
        try:
            on_step_settled(terminal_id, error)
        except Exception:  # noqa: BLE001 — step bookkeeping is best-effort; never fail the step
            logger.warning("run_step: script step completion bookkeeping failed", exc_info=True)

    # The generation fence (ADR-9 anti-double-drive, DR-5): a script run-step call
    # carrying BOTH CAO_WORKFLOW_RUN_ID and CAO_WORKFLOW_GENERATION must be checked
    # against the run's current journaled generation BEFORE dispatch — a resume or
    # cancel bumps the generation, and a reparented predecessor subprocess's late
    # calls must be fenced out rather than allowed to run.
    env_vars = body.env_vars or {}
    fence_run_id = env_vars.get("CAO_WORKFLOW_RUN_ID")
    fence_generation = env_vars.get("CAO_WORKFLOW_GENERATION")
    if fence_run_id is not None and fence_generation is not None:
        try:
            workflow_service.check_generation(fence_run_id, fence_generation)
        except StaleGenerationError as e:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"run '{fence_run_id}': {e}",
            )
        except KeyError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    try:
        result = await run_agent_step(
            provider=body.provider,
            agent=body.agent,
            prompt=body.prompt,
            session_name=body.session_name,
            reuse_terminal_id=body.reuse_terminal_id,
            teardown=body.teardown,
            timeout=body.timeout,
            working_directory=body.working_directory,
            caller_id=body.caller_id,
            allowed_tools=body.allowed_tools,
            registry=get_plugin_registry(request),
            env_vars=body.env_vars,
            on_terminal_created=on_terminal_created,
        )
        # Success -> transition the script step RUNNING->COMPLETED (no-op for
        # non-script callers). Before building the response so a settle failure
        # is logged, not raised.
        _settle_step(result.terminal_id, None)
        return RunStepResponse(
            terminal_id=result.terminal_id,
            last_message=result.last_message,
            status=(result.status.value if hasattr(result.status, "value") else str(result.status)),
        )
    except StepExecutionError as e:
        # The step did not complete successfully. Distinguish a worker that
        # CRASHED (kind="error" -> 502 Bad Gateway) from one that RAN LONG
        # (kind="timeout" -> 504 Gateway Timeout) so the caller can tell them
        # apart instead of reporting every failure as a timeout. The detail is a
        # structured object carrying terminal_id, so callers read it as a field
        # rather than regex-scraping the message (the future engine reads it too).
        # Transition the script step RUNNING->FAILED (no-op for non-script callers).
        _settle_step(e.terminal_id, str(e))
        code = status.HTTP_502_BAD_GATEWAY if e.kind == "error" else status.HTTP_504_GATEWAY_TIMEOUT
        raise HTTPException(
            status_code=code,
            detail={"message": str(e), "kind": e.kind, "terminal_id": e.terminal_id},
        )
    except TimeoutError as e:
        _settle_step(None, str(e))
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={"message": str(e), "kind": "timeout", "terminal_id": None},
        )
    except ValueError as e:
        # Unknown terminal / bad input surfaced by the terminal layer.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        _settle_step(None, str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to run step: {str(e)}",
        )


# =============================================================================
# Workflow authoring + structured-return endpoints (issue #312, Bolt 2)
# =============================================================================
# Single integration seam for the `cao workflow` CLI verbs and the
# `workflow_return` MCP tool (B2-BR-10). Core services raise narrow exceptions;
# this boundary maps them to HTTPException (B2-BR-9): ValueError -> 400,
# FileNotFoundError/KeyError -> 404. The run/cancel/status endpoints are Bolt 3.


@app.post("/workflows/validate")
async def validate_workflow_endpoint(body: WorkflowValidateRequest) -> Dict:
    """Validate a workflow spec without running it (FR-1.3/A1a). Returns ValidationResult.

    Extension-based dispatch (U5, A1a, BR-23a): ``.yaml``/``.yml`` calls
    ``validate_only`` UNCHANGED (FR-5.1); ``.py`` calls ``lint_script``
    DIRECTLY — NOT via ``get_workflow``/``ScriptSpec`` — staying read-only,
    side-effect-free, and collision-check-free like the YAML arm (BR-23b).
    The complete ``ScriptValidationResult`` is returned with ``model_dump()``.
    """
    import os as _os

    from cli_agent_orchestrator.services import workflow_spec_service

    ext = _os.path.splitext(body.path)[1].lower()
    if ext in (".yaml", ".yml"):
        try:
            result = workflow_spec_service.validate_only(body.path)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        return result.model_dump()
    if ext == ".py":
        from cli_agent_orchestrator.constants import WORKFLOW_MAX_SPEC_BYTES
        from cli_agent_orchestrator.models.workflow import ScriptValidationResult
        from cli_agent_orchestrator.services.script_lint import lint_script

        try:
            # ``_safe_spec_path`` returns the resolved, contained path; every
            # filesystem op below MUST use THIS value (not ``body.path``) so the
            # resolve-then-contain check dominates the sink (CodeQL sanitizer
            # requirement — it does not track taint through a re-derived path).
            real_path = workflow_spec_service._safe_spec_path(body.path)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        try:
            with open(real_path, "rb") as fh:
                # Capped read: an oversized file is rejected without ever
                # being fully read into memory.
                raw = fh.read(WORKFLOW_MAX_SPEC_BYTES + 1)
        except OSError as e:
            return ScriptValidationResult(
                status="fail", errors=[f"could not read spec: {e}"]
            ).model_dump()
        if len(raw) > WORKFLOW_MAX_SPEC_BYTES:
            return ScriptValidationResult(
                status="fail",
                errors=[f"spec exceeds {WORKFLOW_MAX_SPEC_BYTES} bytes (max)"],
            ).model_dump()
        source = raw.decode("utf-8", errors="replace")
        result = lint_script(source, real_path)
        return result.model_dump()
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST, detail=f"unrecognized spec extension: {ext}"
    )


@app.get("/workflows")
async def list_workflows_endpoint(dir: Optional[str] = Query(default=None)) -> List[Dict]:
    """List indexed workflows, rebuilt from the spec files on disk (FR-2.1)."""
    from cli_agent_orchestrator.services import workflow_spec_service

    try:
        rows = workflow_spec_service.list_workflows(scan_dir=dir)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return [row.model_dump() for row in rows]


@app.get("/workflows/{name}")
async def get_workflow_endpoint(name: str) -> Dict:
    """Return the parsed/validated spec for a workflow name (FR-2.1, A1).

    Widened return: ``get_workflow`` may now resolve a ``.py`` name to a
    ``ScriptSpec`` (U5, C4) — ``.model_dump()`` is unconditional on either
    return type (BR-7a), so no branch is needed here. ``TierCollisionError``
    (a same-stem cross-tier sibling, BR-2/BR-3) maps to 409, checked BEFORE
    the bare ``ValueError`` arm (it is a ``ValueError`` subclass).
    """
    from cli_agent_orchestrator.models.workflow import TierCollisionError
    from cli_agent_orchestrator.services import workflow_spec_service

    try:
        spec = workflow_spec_service.get_workflow(name)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown workflow '{name}'"
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except TierCollisionError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return spec.model_dump()


@app.delete("/workflows/{name}")
async def delete_workflow_endpoint(
    name: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_ADMIN)),
) -> Dict:
    """Delete a workflow's spec file and its index row (FR-2.4)."""
    from cli_agent_orchestrator.services import workflow_spec_service

    try:
        workflow_spec_service.delete_workflow(name)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown workflow '{name}'"
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return {"success": True, "name": name}


@app.post(
    "/workflows/runs/{run_id}/steps/{step_id}/output",
    response_model=StepOutputResponse,
)
async def record_step_output_endpoint(
    run_id: str,
    step_id: str,
    body: StepOutputRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> StepOutputResponse:
    """Record a worker's structured output for a step (FR-4.1, C5).

    Validation lives at this seam (ADR-4). A schema-invalid output does NOT 500 —
    it is stored with ``validated=False`` / state ``COMPLETED_UNVALIDATED`` and
    returned as a 200 (the engine acts on the flag in Bolt 3). A malformed
    ``run_id`` / ``step_id`` (failing the name regex) maps to 400.
    """
    from cli_agent_orchestrator.services.step_output_store import record_step_output

    try:
        record = record_step_output(
            run_id=run_id,
            step_id=step_id,
            output=body.output,
            output_schema=body.output_schema,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return StepOutputResponse(
        validated=record.validated,
        errors=record.errors,
        state=record.state.value,
    )


# Run-engine endpoints (Bolt 3, N5). ``start_run`` is awaited INLINE (Q1=A): the
# HTTP request is the blocking wait, matching the synchronous ``workflow_run`` MCP
# tool. Error mapping (C5 / B3-BR-14): unknown run/spec -> 404, invalid spec/inputs
# -> 400, cancel-of-finished -> 409, NotBuiltYetError (reserved seam) -> 501,
# WorkflowEngineError -> 500. Narrow exceptions in the service; mapped here.


@app.post("/workflows/runs")
async def start_workflow_run_endpoint(
    body: WorkflowRunRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Resolve a spec, run it to completion inline, return the WorkflowRunResult.

    Tier dispatch (U5, A3, BR-8): ONE ``isinstance(spec, ScriptSpec)`` check,
    immediately after ``get_workflow`` resolves the spec — no downstream code
    re-derives the tier. The YAML arm (``start_run``) is called UNCHANGED
    (FR-5.1). The script arm pre-checks run_id availability itself (BR-9a —
    ``run_script_workflow`` has no admission gate of its own) before calling
    ``run_script_workflow``; a lint failure maps to 422 with a findings body
    (BR-10), via the shared ``render_findings`` helper.
    """
    import uuid

    from cli_agent_orchestrator.models.workflow import (
        NotBuiltYetError,
        ScriptSpec,
        TierCollisionError,
    )
    from cli_agent_orchestrator.services import (
        script_runner,
        workflow_service,
        workflow_spec_service,
    )

    try:
        spec = workflow_spec_service.get_workflow(body.name_or_path)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown workflow '{body.name_or_path}'",
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except TierCollisionError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    run_id = body.run_id or f"run-{uuid.uuid4().hex[:16]}"

    if isinstance(spec, ScriptSpec):
        # Unit A (ADR-6 / blocker #2): validate + cap the inputs BEFORE any
        # journal row or registry entry is created — no orphan RUNNING row can
        # result from bad/oversized input (BR-A3). The RESOLVED map (defaults
        # filled, types checked, undeclared rejected) is what gets journaled and
        # delivered, never the raw request body.
        from cli_agent_orchestrator.constants import WORKFLOW_INPUTS_MAX_BYTES

        try:
            resolved = workflow_service._validate_inputs(spec, body.inputs)
            payload = json.dumps(resolved, separators=(",", ":"))
            if len(payload.encode("utf-8")) > WORKFLOW_INPUTS_MAX_BYTES:
                raise ValueError(f"workflow inputs exceed {WORKFLOW_INPUTS_MAX_BYTES} bytes")
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        try:
            workflow_service._check_run_id_available(run_id)
        except KeyError as e:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
        try:
            result = await script_runner.run_script_workflow(spec, resolved, run_id)
        except script_runner.ScriptLintError as e:
            raise HTTPException(
                status_code=422,
                detail={"findings": workflow_spec_service.render_findings(e.findings)},
            )
        except KeyError as e:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        return result.model_dump()

    try:
        result = await workflow_service.start_run(spec, body.inputs, run_id)
    except NotBuiltYetError as e:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(e))
    except KeyError as e:
        # Duplicate run_id is a conflict, not a 404.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except workflow_service.WorkflowEngineError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    return result.model_dump()


@app.get("/workflows/runs/{run_id}")
async def get_workflow_run_endpoint(run_id: str) -> Dict:
    """Return a point-in-time status snapshot for a run (FR-5.5)."""
    from cli_agent_orchestrator.services import workflow_service

    try:
        status_snapshot = workflow_service.get_run_status(run_id)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown run '{run_id}'")
    return status_snapshot.model_dump()


@app.post("/workflows/runs/{run_id}/cancel")
async def cancel_workflow_run_endpoint(
    run_id: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Cooperatively cancel a running workflow (FR-5.4, U5 A5).

    Tier dispatch reads the LIVE ``run_registry`` record FIRST (BR-15) —
    ``getattr(record, "tier", "yaml")`` — because cancel's async/sync split is
    a property of which function to call on a live process. If absent
    (crash remnant or already-finalized), falls back to the durable journal
    (BR-16): absent row -> 404; terminal state -> 409; otherwise the row is a
    JOURNALED-BUT-NOT-LIVE run — no in-memory record for ``cancel_run`` (which
    only ever consults ``run_registry``) to flip, so this arm marks the journal
    row CANCELLED directly rather than calling ``cancel_run`` (which would
    unconditionally raise ``KeyError`` here and mask every crash-remnant cancel
    as a 404).
    """
    from cli_agent_orchestrator.models.workflow_runtime import RunState
    from cli_agent_orchestrator.services import script_runner, workflow_journal, workflow_service

    record = workflow_service.run_registry.get(run_id)
    if record is None:
        row = workflow_journal.get_run(run_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown run '{run_id}'"
            )
        try:
            row_state = RunState(row.state)
        except ValueError:
            row_state = None
        if row_state in (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"run '{run_id}' is already {row.state}; cannot cancel",
            )
        finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        workflow_journal.update_run_state(run_id, RunState.CANCELLED.value, finished_at)
        return {"success": True, "run_id": run_id}

    if getattr(record, "tier", "yaml") == "script":
        record_state = getattr(record, "state", None)
        if record_state in (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"run '{run_id}' is already "
                    f"{getattr(record_state, 'value', record_state)}; cannot cancel"
                ),
            )
        await script_runner.cancel_script_run(
            cast(script_runner.ScriptRunRecord, record)
        )  # NEVER raises (BR-19)
        return {"success": True, "run_id": run_id}

    try:
        workflow_service.cancel_run(run_id)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown run '{run_id}'")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    return {"success": True, "run_id": run_id}


@app.post("/workflows/runs/{run_id}/resume")
async def resume_workflow_run_endpoint(
    run_id: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Resume a crashed/failed run from its durable journal (FR-6.2, N6, U5 A4).

    Tier dispatch reads the run's **journaled** tier (``RunRow.tier``), NEVER
    by re-resolving a spec (BR-11) — the spec file may have moved/changed
    since the run started. Any ``tier`` value other than the literal string
    ``"script"`` routes to the YAML arm (U5-Q2=A, default-to-YAML). The YAML
    arm (``resume_from_last_completed``) is called UNCHANGED (FR-5.1). The
    script arm's typed-error catch order matches the boundary table: narrower
    ``ResumeNotAllowedError``/``ResumeCorruptError`` (both ``ValueError``
    subclasses) are caught BEFORE the bare ``ValueError`` arm.
    """
    from cli_agent_orchestrator.services import script_runner, workflow_journal, workflow_service

    row = workflow_journal.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown run '{run_id}'")

    if row.tier == "script":
        try:
            result = await script_runner.resume_script_run(run_id)
        except KeyError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown run '{run_id}'"
            )
        except workflow_service.ResumeNotAllowedError as e:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
        except workflow_service.ResumeCorruptError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        return result.model_dump()

    try:
        result = await workflow_service.resume_from_last_completed(run_id)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown run '{run_id}'")
    except workflow_service.ResumeNotAllowedError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except workflow_service.ResumeCorruptError as e:
        # 422 by literal code: the ``status`` alias name differs across Starlette
        # versions in the CI matrix; the integer is stable and warning-free.
        raise HTTPException(status_code=422, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except workflow_service.WorkflowEngineError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    return result.model_dump()


# ── graph layer (U4, Issue #348) ────────────────────────────────────────
#
# Two routes over the provider/sink seams. There is ZERO branching over the
# provider or sink NAME (NFR-5): the only conditionals are try/except on
# registry-resolution outcome. Names resolve through get_provider/get_sink,
# which raise KeyError for an unregistered name (mapped to 404 here).


@app.get("/graph/{provider}")
async def get_graph_endpoint(
    provider: str,
    request: Request,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Project a provider's GraphView and return its wire shape.

    Scope-gated (D5 posture): when auth is enabled, any of
    ``cao:read`` / ``cao:write`` / ``cao:admin`` is required (read is the
    floor) — identical to ``/events``. This SUPERSEDES the original FR-12
    "UNGATED by design" wording: the graph carries private-scope
    structure, including contradiction-edge summaries of memory CONTENT, so
    an unauthenticated caller must not be able to read it (PR #424 review).

    Private tiers are REFUSED outright: a ``scope`` of ``session`` or
    ``agent`` is rejected with 400 even for an authed ``cao:read`` caller,
    mirroring ``/memory/export`` — the API surface never exposes private
    tiers (D5). All other query params (``scope_id`` and any extras) are
    forwarded to the provider as ``**filters``.

    Error taxonomy: unregistered provider -> 404; private-scope request or
    provider ValueError (e.g. a bad filter value) -> 400.
    """
    filters = dict(request.query_params)

    # Private-scope gate (D5): the graph route takes ``scope`` as a query
    # string, so compare its value against the private MemoryScope values.
    # Mirrors /memory/export's MemoryScope.SESSION/AGENT refusal. The check is
    # case-insensitive so ``scope=Session`` / ``scope=AGENT`` can't slip past;
    # only this local comparison is normalized — the raw value is still
    # forwarded to the provider in ``filters`` unchanged.
    requested_scope = filters.get("scope")
    if requested_scope is not None and requested_scope.lower() in (
        MemoryScope.SESSION.value,
        MemoryScope.AGENT.value,
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"scope '{requested_scope}' is private and cannot be read via the graph API",
        )

    try:
        inst = get_provider(provider)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown graph provider '{provider}'",
        )
    try:
        view = await inst.project(**filters)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return view.to_dict()


@app.post("/graph/{provider}/export")
async def export_graph_endpoint(
    provider: str,
    body: GraphExportRequest,
    request: Request,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Project a provider's view and export it through a named sink (FR-12).

    Scope-gated (401 no/invalid token, 403 valid-but-insufficient). The
    serialized view is scanned by ``secret_gate`` BEFORE the sink is
    invoked; a hit rejects the export with 422 and the sink's ``export`` is
    never called. The 422 detail names only the matched PATTERN, never the
    matched bytes.

    Error taxonomy: unregistered provider or sink -> 404; secret hit -> 422;
    provider/sink ValueError -> 400; sink OSError (e.g. dest is an existing
    directory, permission denied, ENOSPC) -> 400 — a bad-dest-shape failure
    kept consistent with the ValueError mapping rather than leaking a 500.
    """
    filters = dict(request.query_params)
    try:
        prov = get_provider(provider)
        sink = get_sink(body.sink)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown graph provider '{provider}' or sink '{body.sink}'",
        )

    try:
        view = await prov.project(**filters)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    # Credential gate (ADR-5): scan the serialized view; on a hit, reject
    # before the sink writes anything. secret_gate returns the pattern NAME,
    # never the matched bytes, so the detail is safe to surface.
    serialized = json.dumps(view.to_dict())
    hit = secret_gate.scan_for_secrets(serialized)
    if hit is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"export rejected: secret pattern '{hit}' detected",
        )

    try:
        written_files = sink.export(view, body.dest, **body.options)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except OSError as e:
        # dest is an existing directory (IsADirectoryError), permission
        # denied, ENOSPC, etc. — a bad destination, mapped to 400 for
        # consistency with the ValueError branch rather than a bare 500.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"export failed writing to destination: {e}",
        )

    return {"written_files": written_files, "sink": body.sink, "dest": body.dest}


@app.delete("/terminals/{terminal_id}")
async def delete_terminal(
    request: Request,
    terminal_id: TerminalId,
    expected_generation: Optional[str] = None,
    expected_session: Optional[str] = None,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_ADMIN)),
) -> Dict:
    """Delete a terminal, optionally only its exact reserved generation.

    With ``expected_generation``/``expected_session`` the delete is a
    compare-and-delete: a mismatched or replacement incarnation is preserved
    and reported as 409 ambiguity, never deleted (spec §20.2d(2))."""
    try:
        # delete_terminal is fully synchronous: blocking tmux kills, a
        # full-history scrollback snapshot capture, and DB writes. Off the
        # loop so a stalled tmux/FIFO op bounds its blast radius to this one
        # request instead of wedging the whole server (issue #382 fixed this
        # for DELETE /sessions; the per-terminal path had the same hazard).
        conditional: Dict[str, Any] = {}
        if expected_generation is not None:
            conditional["expected_generation"] = expected_generation
        if expected_session is not None:
            conditional["expected_session"] = expected_session
        success = await asyncio.to_thread(
            terminal_service.delete_terminal,
            terminal_id,
            registry=get_plugin_registry(request),
            **conditional,
        )
        return {"success": success}
    except TerminalGenerationMismatchError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete terminal: {str(e)}",
        )


@app.post("/terminals/{receiver_id}/inbox/messages")
async def create_inbox_message_endpoint(
    request: Request,
    receiver_id: TerminalId,
    sender_id: str,
    message: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Create inbox message and attempt immediate delivery."""
    try:
        # Managed-v2 creation takes the terminal successor flock to bind an
        # exact receiver generation.  Never wait for that OS lock on the
        # ASGI loop: a native admission can own it while awaiting tmux/readiness
        # work on this same loop.
        inbox_msg = await asyncio.to_thread(
            create_inbox_message,
            sender_id,
            receiver_id,
            message,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create inbox message: {str(e)}",
        )

    # Attempt immediate delivery if terminal is already IDLE.
    # If not, InboxService will deliver on next IDLE status event.
    try:
        # This path includes synchronous DB, successor/fence, bridge, and
        # native-pane work.  Keeping it off-loop gives the admission that
        # owns a predecessor lock a chance to complete and release it.
        await asyncio.to_thread(
            inbox_service.deliver_pending,
            receiver_id,
            registry=get_plugin_registry(request),
        )
    except Exception as e:
        logger.warning(f"Immediate delivery attempt failed for {receiver_id}: {e}")

    return {
        "success": True,
        "message_id": inbox_msg.id,
        "sender_id": inbox_msg.sender_id,
        "receiver_id": inbox_msg.receiver_id,
        "created_at": inbox_msg.created_at.isoformat(),
    }


def _callback_recovery_response(
    result: callback_recovery.RecoveryAdmission,
) -> Dict[str, Any]:
    message, operation = result.message, result.operation
    if message is None:
        stored = operation.get("admission_response")
        if not isinstance(stored, dict):
            raise callback_recovery.CallbackRecoveryConflict(
                "terminal recovery lost its immutable admission response"
            )
        return {
            **stored,
            "outcome": operation["state"],
            "replayed": True,
            "proven_zero_bytes": operation["proven_zero_bytes"],
        }
    return {
        "outcome": operation["state"],
        "operation_key": operation["operation_key"],
        "operation_id": operation["operation_id"],
        "message_id": message.id,
        "message_sha256": message.message_sha256,
        "sender_id": message.sender_id,
        "sender_generation": message.sender_generation,
        "receiver_id": message.receiver_id,
        "receiver_generation": message.expected_receiver_generation,
        "provider": message.expected_provider,
        "provider_session_id": message.expected_provider_session_id,
        "execution_mode": message.expected_execution_mode,
        "callback_occurrence_id": operation["callback_occurrence_id"],
        "report_sha256": operation["report_sha256"],
        "source_head": operation["source_head"],
        "created_at": message.created_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "status": message.status.value,
        "replayed": result.replayed,
    }


@app.post("/terminals/{source_terminal_id}/callback-recoveries")
async def create_callback_recovery_endpoint(
    request: Request,
    source_terminal_id: TerminalId,
    body: CallbackRecoveryRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Any:
    """Consume one exact ACP refusal to recover one stranded callback."""
    if body.source_terminal_id != source_terminal_id:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "outcome": "refused",
                "reason_code": "source-path-mismatch",
                "proven_zero_bytes": True,
            },
        )
    operation_key, request_sha256 = callback_recovery.operation_identity(body)
    if not recovery_capabilities.callback_recovery_admission_allowed(body.expected_provider):
        # This strict request-bound response is intentionally before any
        # operation/inbox mutation or provider bridge attempt.
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "schema": "cao-callback-recovery-lifecycle-disabled-v1",
                "outcome": "callback-recovery-disabled",
                "reason_code": "lifecycle-capability-disabled",
                "operation_key": operation_key,
                "request_sha256": request_sha256,
                "proven_zero_bytes": True,
            },
        )
    try:
        result = await asyncio.to_thread(callback_recovery.admit, body)
    except callback_recovery.CallbackRecoveryRefused as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "outcome": "refused",
                "reason_code": exc.reason_code,
                "source_terminal_id": source_terminal_id,
                "operation_id": body.operation_id,
                "proven_zero_bytes": True,
                "detail": str(exc),
            },
        )
    except callback_recovery.CallbackRecoveryAmbiguous as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "outcome": "ambiguous",
                "reason_code": exc.reason_code,
                "source_terminal_id": source_terminal_id,
                "operation_id": body.operation_id,
                "proven_zero_bytes": False,
                "detail": str(exc),
            },
        )
    except callback_recovery.CallbackRecoveryIdentityConflict as exc:
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content=exc.response)
    except callback_recovery.CallbackRecoveryConflict as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "outcome": "conflict",
                "reason_code": exc.reason_code,
                "source_terminal_id": source_terminal_id,
                "operation_id": body.operation_id,
                "proven_zero_bytes": False,
                "detail": str(exc),
            },
        )

    # The operation and row are durable before delivery. Select this exact row
    # so an unrelated backlog can never starve the just-admitted recovery.
    if result.message is not None:
        try:
            await asyncio.to_thread(
                inbox_service.deliver_pending,
                source_terminal_id,
                0,
                get_plugin_registry(request),
                required_message_id=result.message.id,
            )
        except Exception as exc:
            logger.warning(
                "Immediate callback recovery delivery failed for %s: %s",
                source_terminal_id,
                exc,
            )
    return _callback_recovery_response(result)


@app.get("/control-input/{control_id}/callback-recovery-refusal")
async def get_callback_recovery_refusal_endpoint(
    control_id: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    try:
        return await asyncio.to_thread(callback_recovery.refusal_occurrence, control_id)
    except callback_recovery.CallbackRecoveryNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@app.get("/callback-recoveries/{operation_key}")
async def get_callback_recovery_endpoint(
    operation_key: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    try:
        return await asyncio.to_thread(callback_recovery.get, operation_key)
    except callback_recovery.CallbackRecoveryNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except callback_recovery.CallbackRecoveryConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@app.get("/callback-recoveries/{operation_key}/turn-receipt")
async def get_callback_recovery_turn_receipt(
    operation_key: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_ADMIN)),
):
    try:
        receipt = await asyncio.to_thread(callback_recovery.turn_receipt, operation_key)
    except callback_recovery.CallbackRecoveryNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except (
        callback_recovery.CallbackRecoveryConflict,
        companion_receipts.CompanionReceiptInvalid,
        model_turn_receipt_contract.ReceiptValidationError,
    ) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if receipt is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return receipt


@app.post("/callback-recoveries/{operation_key}/complete")
async def complete_callback_recovery_endpoint(
    request: Request,
    operation_key: str,
    body: CallbackRecoveryCompletionRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    try:
        result = await asyncio.to_thread(callback_recovery.complete, operation_key, body)
    except callback_recovery.CallbackRecoveryNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except callback_recovery.CallbackRecoveryPending as exc:
        raise HTTPException(status_code=status.HTTP_425_TOO_EARLY, detail=str(exc))
    except callback_recovery.CallbackRecoveryConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    # Completion is the sole delivery arm.  A worker callback POST only
    # registers immutable callback bytes; it cannot race finalization by
    # causing a supervisor effect before this exact intent is durable.
    if (
        result["state"] == callback_recovery.STATE_SUBMITTED
        and result["callback_attempt_state"] == callback_recovery.CALLBACK_ATTEMPT_REGISTERED
        and result["callback_message_id"] is not None
    ):
        try:
            await asyncio.to_thread(
                inbox_service.deliver_pending,
                result["supervisor_id"],
                0,
                get_plugin_registry(request),
                required_message_id=result["callback_message_id"],
            )
        except Exception as exc:
            logger.warning(
                "Completion-armed recovery callback delivery failed for %s/%s: %s",
                result["supervisor_id"],
                result["callback_message_id"],
                exc,
            )
    # Synchronous delivery can legally commit the effect before this endpoint
    # returns.  Return one authoritative post-attempt read, never the stale
    # pre-delivery completion-intent snapshot that would make a completed
    # one-shot recovery look failed to its paired conductor.
    return await asyncio.to_thread(callback_recovery.get, operation_key)


@app.post("/callback-recoveries/{operation_key}/callback")
async def create_callback_recovery_callback_endpoint(
    request: Request,
    operation_key: str,
    body: CallbackRecoveryCallbackRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict[str, Any]:
    """Create the callback through its server-authenticated recovery producer."""
    try:
        result = await asyncio.to_thread(
            callback_recovery.create_callback,
            operation_key,
            body,
        )
    except callback_recovery.CallbackRecoveryNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except callback_recovery.CallbackRecoveryPending as exc:
        raise HTTPException(status_code=status.HTTP_425_TOO_EARLY, detail=str(exc))
    except (
        callback_recovery.CallbackRecoveryConflict,
        callback_recovery.CallbackRecoveryRefused,
    ) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return result


@app.get("/callback-recoveries/{operation_key}/callback")
async def get_callback_recovery_callback_endpoint(
    operation_key: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_ADMIN)),
) -> Any:
    """Reconcile a dedicated callback POST by immutable completion key."""
    try:
        result = await asyncio.to_thread(callback_recovery.callback_lookup, operation_key)
    except callback_recovery.CallbackRecoveryNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except callback_recovery.CallbackRecoveryConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return result


@app.post("/callback-recoveries/{operation_key}/resolve")
async def resolve_callback_recovery_endpoint(
    operation_key: str,
    body: CallbackRecoveryResolutionRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_ADMIN)),
) -> Dict[str, Any]:
    """Apply an evidence-bound governed disposition to an ambiguity."""
    try:
        return await asyncio.to_thread(callback_recovery.resolve_ambiguity, operation_key, body)
    except callback_recovery.CallbackRecoveryNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except callback_recovery.CallbackRecoveryConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@app.post("/callback-recoveries/{operation_key}/dispose-callback-undeliverable")
async def dispose_callback_recovery_undeliverable_endpoint(
    operation_key: str,
    body: CallbackRecoveryDispositionRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_ADMIN)),
) -> Dict[str, Any]:
    """Apply the evidence-bound ADMIN terminal for an undeliverable callback."""
    try:
        return await asyncio.to_thread(
            callback_recovery.dispose_callback_undeliverable,
            operation_key,
            body,
        )
    except callback_recovery.CallbackRecoveryNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except callback_recovery.CallbackRecoveryConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@app.get("/terminals/{terminal_id}/inbox/messages")
async def get_inbox_messages_endpoint(
    terminal_id: TerminalId,
    limit: int = Query(default=10, le=100, description="Maximum number of messages to retrieve"),
    status_param: Optional[str] = Query(
        default=None, alias="status", description="Filter by message status"
    ),
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_ADMIN)),
) -> List[Dict]:
    """Get inbox messages for a terminal.

    Args:
        terminal_id: Terminal ID to get messages for
        limit: Maximum number of messages to return (default: 10, max: 100)
        status_param: Optional filter by message status ('pending', 'delivered', 'failed')

    Returns:
        List of inbox messages with sender_id, message, created_at, status
    """
    try:
        # Convert status filter if provided
        status_filter = None
        if status_param:
            try:
                status_filter = MessageStatus(status_param)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid status: {status_param}. Valid values: pending, delivered, failed",
                )

        # Get messages using existing database function
        # Dedicated recovery rows contain a bearer token and local report path
        # in their provider prompt. They are managed protocol artifacts, not
        # ordinary user-readable inbox messages, and are exposed only through
        # the scoped callback-recovery surfaces.
        messages = [
            message
            for message in get_inbox_messages(terminal_id, limit=100, status=status_filter)
            if not message.is_identity_bound
        ][:limit]

        # Convert to response format
        result = []
        for msg in messages:
            result.append(
                {
                    "id": msg.id,
                    "sender_id": msg.sender_id,
                    "receiver_id": msg.receiver_id,
                    "message": msg.message,
                    "status": msg.status.value,
                    "created_at": msg.created_at.isoformat() if msg.created_at else None,
                }
            )

        return result

    except HTTPException:
        # Re-raise HTTPException (validation errors)
        raise
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve inbox messages: {str(e)}",
        )


_SGR_WHEEL_REPORT = re.compile(r"^\x1b\[<([0-9]+);[0-9]+;[0-9]+[Mm]$")


def _is_wheel_mouse_report(data: str) -> bool:
    """Return whether *data* is one complete terminal wheel report.

    Managed browser terminals are transcripts, not alternate provider input
    channels.  The server therefore repeats the client's narrow wheel check
    before any bytes reach the attached tmux client.  Printable input, paste,
    malformed reports, and non-wheel mouse buttons remain blocked even if a
    modified or stale dashboard tries to send them.
    """
    match = _SGR_WHEEL_REPORT.fullmatch(data)
    if match is not None:
        return (int(match.group(1)) & 64) == 64

    if len(data) == 6 and data.startswith("\x1b[M"):
        button = ord(data[3]) - 32
        return 0 <= button <= 223 and (button & 64) == 64
    return False


def _web_terminal_input_bytes(
    payload: dict[str, Any],
    *,
    managed_terminal: bool,
) -> bytes | None:
    """Resolve one browser input frame to the only bytes it may write."""
    data = payload.get("data")
    if not isinstance(data, str):
        return None
    if managed_terminal and not _is_wheel_mouse_report(data):
        return None
    return data.encode()


# The resize frame is accepted viewer geometry, not pane input (§6.6): it
# reflows the bound TUI and carries no keystroke content.  Dimensions are
# clamped to a sane bound (positive, at most 500 columns by 200 rows) so a
# wild value cannot balloon the pty, and a non-integer dimension rejects
# the frame with a typed close reason instead of tearing the viewer
# websocket down on a struct.pack TypeError.
_WS_RESIZE_MAX_COLS = 500
_WS_RESIZE_MAX_ROWS = 200
_WS_CLOSE_RESIZE_MALFORMED = 4000


def _web_resize_dimensions(payload: dict[str, Any]) -> tuple[int, int] | None:
    """The clamped ``(rows, cols)`` of one resize frame, or None if malformed.

    Absent dimensions keep the deployed defaults (24×80); a stated
    dimension must be an integer (booleans are rejected: ``True`` is not
    a size), and integers are clamped into the bound rather than
    rejected, because an over-large viewer is a reasonable thing to
    satisfy at the bound.
    """
    rows = payload.get("rows", 24)
    cols = payload.get("cols", 80)
    for dimension in (rows, cols):
        if isinstance(dimension, bool) or not isinstance(dimension, int):
            return None
    return (
        max(1, min(rows, _WS_RESIZE_MAX_ROWS)),
        max(1, min(cols, _WS_RESIZE_MAX_COLS)),
    )


@app.websocket("/terminals/{terminal_id}/ws")
async def terminal_ws(websocket: WebSocket, terminal_id: str):
    """WebSocket endpoint for live terminal streaming via tmux attach.

    Security: This endpoint provides full PTY access with no authentication.
    It is intended for localhost-only use. Do NOT expose the server to
    untrusted networks (e.g. --host 0.0.0.0) without adding authentication.
    """
    # Reject connections from clients outside the configured allowlist.
    # Defaults to loopback; operators running cao-server inside a container can
    # extend the allowlist with the ``CAO_WS_ALLOWED_CLIENTS`` env var so the
    # host browser (reaching the container via a bridge IP) can attach.
    # A literal ``*`` in the allowlist disables the IP check (Codespaces /
    # devcontainers / remote setups where the WS client originates from an
    # IP the operator cannot enumerate ahead of time).
    client_host = websocket.client.host if websocket.client else None
    if (
        "*" not in WS_ALLOWED_CLIENTS
        and client_host is not None
        and client_host not in WS_ALLOWED_CLIENTS
    ):
        await websocket.close(code=4003, reason="WebSocket access is restricted to allowed clients")
        return

    await websocket.accept()

    managed_identity = await asyncio.to_thread(managed_launch.managed_control_identity, terminal_id)
    metadata = (
        get_terminal_metadata_v2(terminal_id)
        if managed_identity is not None and managed_identity.get("vintage") == "v2"
        else get_terminal_metadata(terminal_id)
    )
    if not metadata:
        await websocket.close(code=4004, reason="Terminal not found")
        return
    managed_terminal = managed_identity is not None

    # Defence-in-depth: re-validate the names from the DB before they
    # flow into a tmux subprocess argument. The POST /sessions handler
    # now validates user-supplied session_name, but pre-existing rows
    # or future code paths could still bypass that, and tmux parses
    # ':' / '.' as target delimiters. Bind the validator return values
    # so the sanitization is explicit at the actual sink below.
    # This tmux-shaped validation is deliberately applied to every backend.
    try:
        session_name = validate_tmux_name(metadata["tmux_session"], "session_name")
        window_name = validate_tmux_name(metadata["tmux_window"], "window_name")
    except ValueError:
        await websocket.close(code=4003, reason="Invalid tmux target name")
        return

    # Before a single byte is streamed, prove the registered pane is still
    # the pane that was registered. A row whose window was deleted, or
    # whose name a later window has taken, must fail closed with a reason
    # that names what happened — not the generic "Failed to attach
    # terminal", and never by falling back to resolving the name.
    from cli_agent_orchestrator.services import terminal_projection

    # Managed terminals are verified too, and they are the ones this
    # matters most for. Skipping them left every v2 native worker attaching
    # by mutable name on the *default* tmux server — discarding the pane,
    # window and socket its own row records — so opening the card of a
    # worker whose window had been deleted and its name reused gave an
    # operator an interactive PTY into a stranger's live pane, which is
    # worse than a one-shot key because they then type into it.
    verified_pane: str | None = None
    try:
        verified = await asyncio.to_thread(
            terminal_service.verified_pane_target,
            terminal_id,
            _identity_for_verification(metadata),
            operation="web-attach",
        )
    except terminal_service.TerminalIdentityMismatchError as exc:
        projected = await asyncio.to_thread(terminal_projection.project_terminal, terminal_id)
        state = (projected or {}).get("lifecycle_state") or "identity-mismatch"
        successor = (projected or {}).get("superseded_by_terminal_id")
        reason = f"terminal-{state}"
        if successor:
            reason = f"{reason}; replaced by {successor}"
        logger.info("Refused web attach for terminal %s: %s", terminal_id, exc)
        await websocket.close(code=4004, reason=reason[:120])
        return
    if verified is not None:
        verified_pane = verified.pane_id
        # The pane may have been relabelled since the row was written;
        # the verification returns the names it answers to now.
        session_name = verified.session_name
        window_name = verified.window_name

    try:
        attach_command = await asyncio.to_thread(
            partial(
                get_backend().prepare_web_attach,
                session_name,
                window_name,
                pane_id=verified_pane,
                server_socket_path=metadata.get("server_socket_path"),
            )
        )
    except TypeError:
        # A backend that predates identity-addressed attach. It is handed
        # the names only, which is exactly what it did before.
        attach_command = await asyncio.to_thread(
            get_backend().prepare_web_attach, session_name, window_name
        )
    except TerminalBackendError as e:
        # Includes the backend's own refusal of an identity it cannot
        # address safely. Reported as unbound rather than as a generic
        # failure: nothing is wrong with the pane, the row does not say
        # enough about it to be attached to.
        logger.error(f"Web attach failed for terminal {terminal_id}: {e}")
        await websocket.close(code=4004, reason="terminal-unbound-identity")
        return

    # Create PTY pair for backend attach
    master_fd, slave_fd = pty.openpty()

    # Set initial terminal size
    winsize = struct.pack("HHHH", 24, 80, 0, 0)
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

    # Start the configured backend's interactive client inside the PTY.
    # Container/devcontainer environments often leave TERM unset or set to
    # ``dumb``, which strips colours, breaks cursor positioning and corrupts
    # the Ink-based TUIs that agent CLIs render. Force a sane default so the
    # browser-side xterm.js renderer sees the escape sequences it expects.
    # Any explicit non-dumb TERM the operator set is preserved.
    pty_env = _build_pty_env()
    proc = subprocess.Popen(
        attach_command,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
        preexec_fn=os.setsid,
        env=pty_env,
    )
    os.close(slave_fd)

    # Make master_fd non-blocking for event-driven reads
    flag = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flag | os.O_NONBLOCK)

    loop = asyncio.get_event_loop()
    output_queue: asyncio.Queue[bytes] = asyncio.Queue()
    done = asyncio.Event()

    def _on_pty_data():
        """Callback when PTY has data available."""
        try:
            data = os.read(master_fd, 65536)
            if data:
                output_queue.put_nowait(data)
            else:
                done.set()
        except BlockingIOError:
            pass
        except OSError:
            done.set()

    loop.add_reader(master_fd, _on_pty_data)

    async def _forward_output():
        """Read from PTY queue and send to WebSocket."""
        while not done.is_set():
            try:
                data = await asyncio.wait_for(output_queue.get(), timeout=1.0)
                # Drain any additional pending data for batching
                while not output_queue.empty():
                    try:
                        data += output_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                await websocket.send_bytes(data)
            except asyncio.TimeoutError:
                if proc.poll() is not None:
                    break
            except (Exception, asyncio.CancelledError):
                break

    async def _forward_input():
        """Receive from WebSocket and write to PTY."""
        try:
            while not done.is_set():
                msg = await websocket.receive_text()
                payload = json.loads(msg)
                if payload.get("type") == "input":
                    raw = _web_terminal_input_bytes(
                        payload,
                        managed_terminal=managed_terminal,
                    )
                    if raw is None:
                        continue
                    # Write in chunks to avoid overflowing the PTY buffer
                    chunk_size = 1024
                    for i in range(0, len(raw), chunk_size):
                        os.write(master_fd, raw[i : i + chunk_size])
                        if i + chunk_size < len(raw):
                            await asyncio.sleep(0.01)
                elif payload.get("type") == "resize":
                    dimensions = _web_resize_dimensions(payload)
                    if dimensions is None:
                        # Fail closed with a typed reason: a malformed
                        # resize is a client bug the operator should see,
                        # not an untyped teardown.
                        await websocket.close(
                            code=_WS_CLOSE_RESIZE_MALFORMED,
                            reason="resize-malformed: rows/cols must be integers",
                        )
                        return
                    rows, cols = dimensions
                    winsize_data = struct.pack("HHHH", rows, cols, 0, 0)
                    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize_data)
                    # Explicitly notify tmux of the size change —
                    # TIOCSWINSZ on the master doesn't always deliver
                    # SIGWINCH to the child process group.
                    try:
                        os.kill(proc.pid, signal.SIGWINCH)
                    except OSError:
                        pass
        except WebSocketDisconnect:
            pass
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            done.set()

    try:
        await asyncio.gather(_forward_output(), _forward_input())
    except (Exception, asyncio.CancelledError):
        pass
    finally:
        done.set()
        try:
            loop.remove_reader(master_fd)
        except Exception:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        # Terminate tmux attach (just detaches, doesn't kill the session)
        proc.terminate()
        try:
            await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=3.0)
        except asyncio.TimeoutError:
            proc.kill()
            await asyncio.to_thread(proc.wait)


# ── Flow management endpoints ────────────────────────────────────────


@app.get("/flows", response_model=List[Flow])
async def list_flows() -> List[Flow]:
    """List all flows."""
    try:
        return flow_service.list_flows()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list flows: {str(e)}",
        )


@app.get("/flows/{name}", response_model=Flow)
async def get_flow(name: str) -> Flow:
    """Get a specific flow by name."""
    try:
        return flow_service.get_flow(name)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get flow: {str(e)}",
        )


@app.post("/flows", response_model=Flow, status_code=status.HTTP_201_CREATED)
async def create_flow(
    body: CreateFlowRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Flow:
    """Create a new flow.

    Writes a .flow.md file with YAML frontmatter and prompt body, then
    registers it via flow_service.add_flow().
    """
    try:
        flows_dir = CAO_HOME_DIR / "flows"
        flows_dir.mkdir(parents=True, exist_ok=True)

        file_path = flows_dir / f"{body.name}.flow.md"

        # Build YAML frontmatter content
        frontmatter_lines = [
            "---",
            f"name: {body.name}",
            f'schedule: "{body.schedule}"',
            f"agent_profile: {body.agent_profile}",
            f"provider: {body.provider}",
            "---",
        ]
        file_content = "\n".join(frontmatter_lines) + "\n" + body.prompt_template

        file_path.write_text(file_content)

        return flow_service.add_flow(str(file_path))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create flow: {str(e)}",
        )


@app.delete("/flows/{name}")
async def remove_flow(
    name: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_ADMIN)),
) -> Dict:
    """Remove a flow."""
    try:
        flow_service.remove_flow(name)
        return {"success": True}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to remove flow: {str(e)}",
        )


@app.post("/flows/{name}/enable")
async def enable_flow(
    name: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Enable a flow."""
    try:
        flow_service.enable_flow(name)
        return {"success": True}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to enable flow: {str(e)}",
        )


@app.post("/flows/{name}/disable")
async def disable_flow(
    name: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Disable a flow."""
    try:
        flow_service.disable_flow(name)
        return {"success": True}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to disable flow: {str(e)}",
        )


@app.post("/flows/{name}/run")
async def run_flow(
    name: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Manually execute a flow."""
    try:
        executed = await flow_service.execute_flow(name)
        return {"executed": executed}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to execute flow: {str(e)}",
        )


# ── Memory endpoints ─────────────────────────────────────────────────
# REST mirror of `cao memory list/show/delete/clear` (issue #286). The server
# has no meaningful cwd, so project scope is addressed by an explicit scope_id
# query param instead of terminal_context — passing a client cwd would be
# routed through resolve_project_id(), whose CAO_PROJECT_ID override applies
# unconditionally and could silently target the wrong project.


def _get_memory_service():
    """Build a MemoryService (lazy import mirrors the circular-import guard
    in memory_service._is_memory_enabled; module-level factory so tests can
    patch it like the CLI's _get_memory_service)."""
    from cli_agent_orchestrator.services.memory_service import MemoryService

    return MemoryService()


def _require_memory_enabled() -> None:
    """Raise 404 when the memory subsystem is disabled.

    recall() silently returns [] when disabled, so the gate must be explicit
    rather than inferred from empty results.
    """
    from cli_agent_orchestrator.services.settings_service import is_memory_enabled

    if not is_memory_enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Memory system is disabled"
        )


def _memory_scope_id(mem, base_dir: Path) -> Optional[str]:
    """Resolve the response scope_id for a recalled memory.

    session/agent results carry scope_id natively; project membership is only
    recoverable from the storage path (base_dir/<project_id>/wiki/project/...);
    global has none.
    """
    if mem.scope_id:
        return str(mem.scope_id)
    if mem.scope != MemoryScope.PROJECT.value:
        return None
    try:
        relative = Path(mem.file_path).resolve().relative_to(base_dir.resolve())
        return relative.parts[0]
    except (ValueError, IndexError):
        return None


def _memory_matches_scope_id(mem, scope_id: str, base_dir: Path) -> bool:
    """True when a recalled memory belongs to the given scope_id.

    Global memories have no scope_id (resolved as None), so they never match —
    scope_id strictly narrows to one project/session/agent.
    """
    return _memory_scope_id(mem, base_dir) == scope_id


def _to_memory_summary(mem, base_dir: Path) -> MemorySummary:
    return MemorySummary(
        key=mem.key,
        scope=mem.scope,
        scope_id=_memory_scope_id(mem, base_dir),
        memory_type=mem.memory_type,
        tags=mem.tags,
        created_at=mem.created_at,
        updated_at=mem.updated_at,
    )


@app.get("/memory", response_model=List[MemorySummary])
async def list_memories_endpoint(
    scope: Optional[MemoryScope] = None,
    memory_type: Optional[MemoryType] = Query(default=None, alias="type"),
    scope_id: Optional[MemoryScopeId] = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> List[MemorySummary]:
    """List stored memories across all projects (mirrors `cao memory list --all`)."""
    _require_memory_enabled()
    svc = _get_memory_service()
    try:
        # Internal limit 1000: recall truncates BEFORE the scope_id filter
        # below, so filtering a small page could return an under-filled result.
        # metadata mode: no query to rank, and it avoids the BM25 path.
        memories = await svc.recall(
            scope=scope.value if scope else None,
            memory_type=memory_type.value if memory_type else None,
            limit=1000,
            scan_all=True,
            search_mode="metadata",
        )
        if scope_id is not None:
            memories = [m for m in memories if _memory_matches_scope_id(m, scope_id, svc.base_dir)]
        return [_to_memory_summary(m, svc.base_dir) for m in memories[:limit]]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list memories: {str(e)}",
        )


@app.get("/memory/export")
async def export_memories_endpoint(
    scope: MemoryScope,
    format: str = Query(default="okf"),
    scope_id: Optional[MemoryScopeId] = None,
    include_history: bool = False,
    redact: bool = False,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
):
    """Stream one scope as an archive tarball (#345 D6, read-only mirror).

    Declared BEFORE /memory/{key} so "export" is not captured as a key.
    Private scopes (session/agent) are refused outright — there is no
    include-private escape hatch over HTTP (D5). The bundle is built by
    the same directory writer into a temp dir, tar'd, and streamed.
    """
    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask

    _require_memory_enabled()
    # Private-scope gate: the CLI's --include-private is a local-operator
    # affordance; the API surface never exports private tiers.
    if scope in (MemoryScope.SESSION, MemoryScope.AGENT):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"scope '{scope.value}' is private and cannot be exported via the API",
        )
    if scope == MemoryScope.PROJECT and scope_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="scope 'project' requires scope_id",
        )

    import tempfile

    from cli_agent_orchestrator.services.memory_archive import get_backend
    from cli_agent_orchestrator.services.memory_archive.okf import export_bundle_to_tar

    svc = _get_memory_service()
    try:
        backend = get_backend(format)(svc)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    tmp_dir = tempfile.mkdtemp(prefix="cao-memory-export-")
    tar_path = Path(tmp_dir) / f"cao-memory-{scope.value}.tar.gz"

    def _cleanup() -> None:
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)

    try:
        export_bundle_to_tar(
            backend,
            scope.value,
            scope_id,
            tar_path,
            include_history=include_history,
            redact=redact,
        )
    except ValueError as e:
        _cleanup()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        _cleanup()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to export memories: {str(e)}",
        )

    return FileResponse(
        path=str(tar_path),
        media_type="application/gzip",
        filename=tar_path.name,
        background=BackgroundTask(_cleanup),
    )


@app.get("/memory/{key}", response_model=MemoryDetail)
async def get_memory_endpoint(
    key: MemoryKey,
    scope: Optional[MemoryScope] = None,
    scope_id: Optional[MemoryScopeId] = None,
) -> MemoryDetail:
    """Show a memory by key (mirrors `cao memory show`; first match wins)."""
    _require_memory_enabled()
    svc = _get_memory_service()
    try:
        memories = await svc.recall(
            query=key,
            scope=scope.value if scope else None,
            limit=1000,
            scan_all=True,
            search_mode="metadata",
        )
        for mem in memories:
            if mem.key != key:
                continue
            if scope_id is not None and not _memory_matches_scope_id(mem, scope_id, svc.base_dir):
                continue
            return MemoryDetail(
                content=mem.content,
                **_to_memory_summary(mem, svc.base_dir).model_dump(),
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Memory '{key}' not found"
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get memory: {str(e)}",
        )


@app.delete("/memory/{key}")
async def delete_memory_endpoint(
    key: MemoryKey,
    scope: MemoryScope = MemoryScope.PROJECT,
    scope_id: Optional[MemoryScopeId] = None,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_ADMIN)),
) -> Dict:
    """Delete a memory by key (mirrors `cao memory delete`).

    Unlike the MCP memory_forget tool (which resolves context from
    CAO_TERMINAL_ID), non-global scopes require an explicit scope_id.
    """
    from cli_agent_orchestrator.services.memory_service import MemoryDisabledError

    _require_memory_enabled()
    if scope != MemoryScope.GLOBAL and scope_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"scope '{scope.value}' requires scope_id",
        )
    svc = _get_memory_service()
    try:
        deleted = await svc.forget(key=key, scope=scope.value, scope_id=scope_id)
    except MemoryDisabledError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Memory system is disabled"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete memory: {str(e)}",
        )
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Memory '{key}' not found in scope '{scope.value}'",
        )
    return {"success": True}


@app.delete("/memory")
async def clear_memories_endpoint(
    scope: MemoryScope,
    scope_id: Optional[MemoryScopeId] = None,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_ADMIN)),
) -> Dict:
    """Clear all memories in a scope (mirrors `cao memory clear`).

    Best-effort per-item loop (warn-and-continue), reporting deleted_count —
    deliberately not all-or-nothing.
    """
    from cli_agent_orchestrator.services.memory_service import MemoryDisabledError

    _require_memory_enabled()
    if scope != MemoryScope.GLOBAL and scope_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"scope '{scope.value}' requires scope_id",
        )
    svc = _get_memory_service()
    try:
        memories = await svc.recall(
            scope=scope.value, limit=1000, scan_all=True, search_mode="metadata"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to clear memories: {str(e)}",
        )
    if scope_id is not None:
        memories = [m for m in memories if _memory_matches_scope_id(m, scope_id, svc.base_dir)]

    deleted_count = 0
    for mem in memories:
        try:
            # session/agent results carry scope_id natively; project results
            # need the query param (their recalled scope_id is None).
            if await svc.forget(key=mem.key, scope=scope.value, scope_id=mem.scope_id or scope_id):
                deleted_count += 1
        except MemoryDisabledError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Memory system is disabled"
            )
        except Exception as e:
            logger.warning("Failed to delete memory %r during clear: %s", mem.key, e)
    return {"success": True, "deleted_count": deleted_count}


# Static file serving for built web UI.
# Anchored to the package via importlib.resources so it works for both
# editable installs (uv sync) and wheel installs (uv tool install, pip install).
from importlib.resources import files as _pkg_files

WEB_DIST = Path(str(_pkg_files("cli_agent_orchestrator") / "web_ui"))
if (WEB_DIST / "index.html").exists():
    from starlette.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(WEB_DIST), html=True), name="web")


def main():
    """Entry point for cao-server command."""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="CLI Agent Orchestrator Server")
    parser.add_argument(
        "--agents-dir",
        type=str,
        default=None,
        help="Path to agents directory (overrides CAO_AGENTS_DIR env var)",
    )
    parser.add_argument("--host", type=str, default=None, help="Server host")
    parser.add_argument("--port", type=int, default=None, help="Server port")
    parser.add_argument(
        "--terminal",
        type=str,
        choices=["tmux", "herdr"],
        default=None,
        help="Terminal backend to use, overriding terminal_backend in config.json",
    )
    args = parser.parse_args()

    if args.agents_dir:
        os.environ["CAO_AGENTS_DIR"] = args.agents_dir
        import cli_agent_orchestrator.constants as constants

        constants.KIRO_AGENTS_DIR = Path(args.agents_dir)
        logger.info(f"Using agents directory: {args.agents_dir}")

    # Resolve the backend before the server starts so the lifespan (and every
    # get_backend() consumer) sees the CLI-selected backend. Without --terminal,
    # the singleton stays lazy and BackendFactory reads config.json on first use.
    if args.terminal:
        from cli_agent_orchestrator.backends.factory import BackendFactory
        from cli_agent_orchestrator.backends.registry import set_backend

        set_backend(BackendFactory.create(backend_override=args.terminal))
        logger.info(f"Terminal backend overridden via --terminal: {args.terminal}")

    host = args.host or SERVER_HOST
    port = args.port or SERVER_PORT
    # Extend the CORS allowlist so a custom --host/--port still permits
    # same-host browser access without requiring CAO_CORS_ORIGINS. The
    # already-installed CORSMiddleware reads the list by reference, so
    # mutating it before uvicorn starts is sufficient. See issue #151.
    add_local_cors_origins(host, port)
    # --proxy-headers: trust X-Forwarded-Proto / X-Forwarded-For from
    # an upstream reverse proxy (Codespaces / devcontainers / nginx in
    # front of cao-server). Required for the WebSocket terminal viewer
    # over an HTTPS tunnel — without it uvicorn sees the raw HTTP
    # request and the browser's WSS upgrade fails. See issue #149.
    #
    # The forwarded-allow-ips list defaults to loopback (see
    # constants.TRUSTED_FORWARDER_IPS); operators behind a reverse
    # proxy opt into a wider range with CAO_FORWARDED_ALLOW_IPS. A
    # literal ``*`` is honoured and disables the check (matches the
    # existing CAO_WS_ALLOWED_CLIENTS="*" semantics).
    forwarded_ips = "*" if "*" in TRUSTED_FORWARDER_IPS else ",".join(TRUSTED_FORWARDER_IPS)
    # Credential query params (``?access_token=``) are scrubbed from uvicorn's
    # access log by ``install_access_log_redaction()``, installed in the app
    # lifespan so both ``cao-server`` and ``uvicorn ...:app`` are covered.
    uvicorn.run(
        app,
        host=host,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips=forwarded_ips,
    )


if __name__ == "__main__":
    main()
