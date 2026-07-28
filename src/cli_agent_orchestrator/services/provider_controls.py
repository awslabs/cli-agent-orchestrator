"""The provider-control registry: the single source for Compact/Stop/Steer.

Design D4 of the native-TUI-console track (§4): before this module the
provider-control facts lived scattered — the adapters' ``CONTROL_COMPACT``
pins, Kimi's build-pinned ``_PROVEN_STEER_CHORDS``, the wire key set, and
the sink allowlists — with no single place a client could read them.  This
module is that place.  It *consumes* the adapters' pins (it imports
``CONTROL_COMPACT``; it does not retype ``"/compact"``), so the registry
can never drift from the version-pinned evidence the adapters hold.

Two read surfaces, deliberately different:

- :func:`controls_for` is the SEND AUTHORITY.  It resolves a provider at
  an exact build: steer chords come from the adapter's build-pinned table,
  so a provider on an unpinned build gets the entry with an *empty* chord
  set — never the union of all builds.
- :func:`advertised_provider_controls` is DISCOVERY ONLY (§3.5): the
  top-level capabilities block, which unions builds so a client learns
  that chord events exist.  It never licenses a send; the per-terminal
  block on the control-identity route (build-exact) is the send authority,
  and a chord absent from it is refused locally at capture time with zero
  POSTs (D9).

Providers with no entry (codex and all others on this base) advertise
nothing: there is no native control adapter and no native-TUI launch
binder for them, so their Compact/Stop cannot be delivered through the
managed path (§13 OD3).  Adding a provider is adding one row plus its
evidence — no wire-schema change.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict

from cli_agent_orchestrator.services import provider_contracts


class ProviderControls(TypedDict):
    """One provider's control facts (the registry's internal shape).

    ``compact``/``stop`` are v3 event sequences (the exact shape a client
    sends as an ordinary control-input request); ``None`` means the
    control does not exist for this provider.  ``steer_chords`` is the
    chord set for the build the entry was resolved at.  ``evidence`` is
    the source pointers behind every fact, so a reviewer can check the
    entry without re-walking the tree.

    Lane C adds ``operator_message`` and ``image`` blocks to this shape
    (§8.6) when it lands; they are absent until then.
    """

    compact: Optional[List[Dict[str, Any]]]
    stop: Optional[List[Dict[str, Any]]]
    steer_chords: tuple
    dispatch_grace_ms: Optional[int]
    evidence: Dict[str, Any]


def _text(value: str) -> Dict[str, Any]:
    return {"type": "text", "text": value}


def _key(name: str) -> Dict[str, Any]:
    return {"type": "key", "key": name}


def _kimi_entry() -> ProviderControls:
    """The kimi_cli row, re-shaped from the adapter's own pins.

    The compact command text is imported from the adapter — restating the
    literal here would fork the one fact both sides must hold.  Stop is
    ``Escape`` per the official Kimi keyboard reference (Esc interrupts
    streaming output / context compaction); ``C-c`` remains available as
    the provider-agnostic key event.  Live acceptance on the pinned
    0.29.x builds is the verification (§10.3, OD2).
    """
    from cli_agent_orchestrator.services import control_input_service, kimi_native_control

    return ProviderControls(
        compact=[_text(kimi_native_control.CONTROL_COMPACT), _key("Enter")],
        stop=[_key("Escape")],
        # Resolved per exact build by controls_for / unioned for discovery
        # by advertised_provider_controls; never restated here.
        steer_chords=(),
        dispatch_grace_ms=int(control_input_service.NATIVE_KIMI_DISPATCH_GRACE_SECONDS * 1000),
        evidence={
            "compact": "kimi_native_control.CONTROL_COMPACT (adapter pin, imported)",
            "stop": (
                "Kimi Code keyboard reference (design Appendix A.6): Esc closes a "
                "popup / cancels completion / interrupts streaming output or "
                "context compaction; verified live per §10.3 (OD2)"
            ),
            "steer_chords": "kimi_native_control._PROVEN_STEER_CHORDS (consumed, not copied)",
            "dispatch_grace_ms": "control_input_service.NATIVE_KIMI_DISPATCH_GRACE_SECONDS",
        },
    )


def _claude_entry() -> ProviderControls:
    """The claude_code row, re-shaped from the adapter's own pins."""
    from cli_agent_orchestrator.services import claude_native_control

    return ProviderControls(
        compact=[_text(claude_native_control.CONTROL_COMPACT), _key("Enter")],
        stop=[_key("Escape")],
        steer_chords=(),
        dispatch_grace_ms=None,
        evidence={
            "compact": "claude_native_control.CONTROL_COMPACT (adapter pin, imported)",
            "stop": 'providers/claude_code.py: the TUI shows "esc to interrupt"',
            "steer_chords": "no steer chord is pinned for any claude_code build",
        },
    )


#: The registry rows.  Compact travels as ordinary composer text through
#: the v3 path — identical to the deployed Compact button — and the kimi
#: adapter's ``control()`` gating on provider-advertised commands applies
#: to the adapter operation path, not this composer-text path.
_REGISTRY = {
    provider_contracts.PROVIDER_KIMI_CLI: _kimi_entry,
    provider_contracts.PROVIDER_CLAUDE_CODE: _claude_entry,
}


def _adapter_steer_chords(provider: str, provider_version: Optional[str]) -> tuple:
    """The exact build's proven steer chords, from the adapter's own table."""
    try:
        from cli_agent_orchestrator.services import managed_launch_v2

        adapter = managed_launch_v2.native_control_adapter(provider)
        steer = getattr(adapter, "steer_chords", None)
        chords = steer(provider_version) if steer is not None else frozenset()
    except Exception:
        chords = frozenset()
    return tuple(sorted(chords))


def _wire_shape(entry: ProviderControls) -> Dict[str, Any]:
    """The §3.5 capabilities shape of one entry.

    Sequences travel wrapped (``{"events": [...]}``) so the block can
    grow per-control facts without reshaping; keys whose value is absent
    for the provider (no dispatch grace, no compact) are omitted rather
    than nulled, matching the deployed additive-advertisement discipline.
    """
    block: Dict[str, Any] = {}
    if entry["compact"] is not None:
        block["compact"] = {"events": entry["compact"]}
    if entry["stop"] is not None:
        block["stop"] = {"events": entry["stop"]}
    block["steer_chords"] = list(entry["steer_chords"])
    if entry["dispatch_grace_ms"] is not None:
        block["dispatch_grace_ms"] = entry["dispatch_grace_ms"]
    return block


def controls_for(provider: str, provider_version: Optional[str]) -> Optional[ProviderControls]:
    """The send-authority entry for ``provider`` at an exact build.

    Build-exact (F11): steer chords resolve through the adapter's
    build-pinned table with the normalized version, so a provider on an
    unpinned build gets the entry with an empty chord set — never the
    union of all builds.  ``None`` means the provider has no registry
    entry at all (no Compact/Stop/Steer is deliverable to it through the
    managed path).
    """
    builder = _REGISTRY.get(provider)
    if builder is None:
        return None
    # The builders return a fresh entry per call, so resolving the chords
    # into it here cannot leak a per-build set into a later call.
    entry = builder()
    entry["steer_chords"] = _adapter_steer_chords(provider, provider_version)
    return entry


def controls_block_for(provider: str, provider_version: Optional[str]) -> Optional[Dict[str, Any]]:
    """The wire shape of :func:`controls_for`, or None for no entry.

    The per-terminal send authority on the control-identity route: this
    terminal's provider resolved at this terminal's build, whose
    ``steer_chords`` is the exact set the server would admit for this
    pane (§3.5).
    """
    entry = controls_for(provider, provider_version)
    if entry is None:
        return None
    return _wire_shape(entry)


def advertised_provider_controls() -> Dict[str, Dict[str, Any]]:
    """The discovery-only union over builds, for the capabilities block.

    §3.5: the top-level union tells a client that chord events exist; it
    never licenses a send.  The per-terminal block (build-exact
    :func:`controls_for`) is the send authority.
    """
    advertised: Dict[str, Dict[str, Any]] = {}
    for provider, builder in _REGISTRY.items():
        entry = builder()
        chords: set = set()
        try:
            from cli_agent_orchestrator.services import managed_launch_v2

            adapter = managed_launch_v2.native_control_adapter(provider)
            advertised_fn = getattr(adapter, "advertised_steer_chords", None)
            if advertised_fn is not None:
                chords = set(advertised_fn().get(provider, ()))
        except Exception:
            chords = set()
        entry["steer_chords"] = tuple(sorted(chords))
        advertised[provider] = _wire_shape(entry)
    return advertised
