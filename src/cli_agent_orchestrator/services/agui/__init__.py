"""AG-UI L2 construct library.

Re-exports the foundation layer: the base construct ABC, the emitter family,
the stream reader, the apply_json_patch_strict helper, the lifecycle tracker,
and the L2 fold-based constructs (supervisor dashboard, session timeline,
cross-provider sync, handoff approval), plus the run plane.
"""

from __future__ import annotations

from cli_agent_orchestrator.services.agui.base import (
    AguiConstruct,
    HttpUiEmitter,
    InProcessUiEmitter,
    RecordingUiEmitter,
    UiEmitter,
    apply_json_patch_strict,
)
from cli_agent_orchestrator.services.agui.cross_provider_sync import CrossProviderStateSync
from cli_agent_orchestrator.services.agui.handoff_approval import (
    AgentHandoffWithApproval,
    ApprovalDecision,
    Interrupt,
    classify_reason,
)
from cli_agent_orchestrator.services.agui.lifecycle_tracker import ToolCallLifecycleTracker
from cli_agent_orchestrator.services.agui.run_plane import AG_UI_AVAILABLE, run_plane_stream
from cli_agent_orchestrator.services.agui.session_timeline import (
    MultiAgentSessionTimeline,
    TimelineEntry,
)
from cli_agent_orchestrator.services.agui.stream_reader import AguiStreamReader
from cli_agent_orchestrator.services.agui.supervisor_dashboard import (
    SupervisorDashboardStream,
)

__all__ = [
    "AG_UI_AVAILABLE",
    "AgentHandoffWithApproval",
    "AguiConstruct",
    "AguiStreamReader",
    "ApprovalDecision",
    "CrossProviderStateSync",
    "HttpUiEmitter",
    "InProcessUiEmitter",
    "Interrupt",
    "MultiAgentSessionTimeline",
    "RecordingUiEmitter",
    "SupervisorDashboardStream",
    "TimelineEntry",
    "ToolCallLifecycleTracker",
    "UiEmitter",
    "apply_json_patch_strict",
    "classify_reason",
    "run_plane_stream",
]
