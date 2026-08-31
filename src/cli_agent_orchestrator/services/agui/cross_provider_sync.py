"""CrossProviderStateSync: fold-based L2 construct for cross-provider state convergence.

Maintains a shared state view by folding STATE_SNAPSHOT and STATE_DELTA frames,
and provides convergence checking against an authoritative snapshot. Extracts
provider information from terminal entries in the state.

Design constraints:
- Snapshot deep-copy replace on STATE_SNAPSHOT.
- Strict apply-else-drop on STATE_DELTA (via apply_json_patch_strict).
- Seen_Set_Dedup: id-bearing frames already processed are skipped;
  state frames (event_id=None) are always folded.
- providers_seen() extracts unique provider values from terminals.
- converges_with(authoritative) performs deep-equal comparison.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Set

from cli_agent_orchestrator.services.agui.base import (
    AguiConstruct,
    BoundedSeen,
    UiEmitter,
    apply_json_patch_strict,
)
from cli_agent_orchestrator.services.agui_stream import (
    AGUI_STATE_DELTA,
    AGUI_STATE_SNAPSHOT,
)


class CrossProviderStateSync(AguiConstruct):
    """Fold-based cross-provider state synchronization construct.

    Consumes STATE_SNAPSHOT/STATE_DELTA frames to maintain a shared state,
    then provides convergence checking against an authoritative source.

    Usage::

        sync = CrossProviderStateSync(emitter)
        for agui_type, data, event_id in frames:
            sync.handle_frame(agui_type, data, event_id)
        if sync.converges_with(authoritative_snapshot):
            print("State is converged")
    """

    def __init__(self, emitter: UiEmitter) -> None:
        super().__init__(emitter)
        self._state: Optional[Dict[str, Any]] = None
        # Bounded seen set for deduplication of id-bearing frames.
        self._seen = BoundedSeen()

    def handle_frame(
        self, agui_type: str, data: Dict[str, Any], event_id: Optional[str] = None
    ) -> None:
        """Process one AG-UI frame.

        STATE_SNAPSHOT: deep-copy replace self._state.
        STATE_DELTA: apply patch via apply_json_patch_strict (drop on failure).
        Frames with event_id already seen: skip (dedup).
        State frames (event_id=None): always folded.
        """
        # Seen_Set_Dedup: skip id-bearing frames already processed.
        if event_id is not None:
            if event_id in self._seen:
                return
            self._seen.add(event_id)

        if agui_type == AGUI_STATE_SNAPSHOT:
            self._state = copy.deepcopy(data.get("snapshot", data))
            return

        if agui_type == AGUI_STATE_DELTA:
            if self._state is None:
                # Delta before snapshot is a no-op.
                return
            delta = data.get("delta", [])
            result = apply_json_patch_strict(self._state, delta)
            if result is not None:
                self._state = result
            # Failed patch: drop silently.
            return

    def shared_state(self) -> Optional[Dict[str, Any]]:
        """Return the current shared state (or None if no snapshot received)."""
        return self._state

    def providers_seen(self) -> List[str]:
        """Extract unique provider values from terminals in the state.

        Returns a sorted list of provider strings observed across all terminals.
        """
        if self._state is None:
            return []

        terminals: List[Dict[str, Any]] = self._state.get("terminals", [])
        providers: Set[str] = set()
        for terminal in terminals:
            provider = terminal.get("provider")
            if provider:
                providers.add(provider)
        return sorted(providers)

    def converges_with(self, authoritative: Dict[str, Any]) -> bool:
        """Check if local state deep-equals the authoritative snapshot.

        Returns True if the local state is not None and equals the authoritative
        snapshot. Returns False if no snapshot has been received yet.
        """
        if self._state is None:
            return False
        return self._state == authoritative

    def projection(self) -> Dict[str, Any]:
        """Return the shared_state as the projection (empty dict if None)."""
        return self._state if self._state is not None else {}
