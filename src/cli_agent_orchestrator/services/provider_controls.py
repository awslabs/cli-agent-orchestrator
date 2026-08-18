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
  an exact build: steer chords come from the adapter's build-pinned table
  plus its runtime bundle read of the installed build, so a build whose
  bundle yields no chord fact gets the entry with an *empty* chord set —
  never the union of all builds.
- :func:`advertised_provider_controls` is DISCOVERY ONLY (§3.5): the
  top-level capabilities block, which unions builds so a client learns
  that chord events exist.  It never licenses a send; the per-terminal
  block on the control-identity route (build-exact) is the send authority,
  and a chord absent from it is refused locally at capture time with zero
  POSTs (D9).

Providers with no entry advertise nothing: there is no native control
adapter for them, so their Compact/Stop cannot be delivered through the
managed path (§13 OD3). Adding a provider is adding one row plus its
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

    Lane C's ``operator_message`` and ``image`` blocks (§8.6) follow the
    version observation: an unlisted-but-observed build advertises both as
    the conservative default (image delivery is staged-path text riding
    the composer plan), with the image block's ``build_proven`` flag
    recording whether the exact build holds the live acceptance
    (``IMAGE_PROVEN_BUILDS``).  Only a failed observation — an absent or
    unparseable version — withholds the blocks.
    """

    compact: Optional[List[Dict[str, Any]]]
    stop: Optional[List[Dict[str, Any]]]
    steer_chords: tuple
    dispatch_grace_ms: Optional[int]
    operator_message: Optional[Dict[str, Any]]
    image: Optional[Dict[str, Any]]
    interactive_streaming: Optional[Dict[str, Any]]
    evidence: Dict[str, Any]


def _text(value: str) -> Dict[str, Any]:
    return {"type": "text", "text": value}


def _key(name: str) -> Dict[str, Any]:
    return {"type": "key", "key": name}


#: §8.3 pinned operation limits.  8192 is a spec pin (OD8: not a measured
#: provider limit — Lane C live acceptance may lower it per evidence;
#: raising it requires re-review).
OPERATOR_MESSAGE_MAX_TEXT_BYTES = 8192
OPERATOR_MESSAGE_MAX_ATTACHMENTS = 4

#: §8.4 staging limits (the tightest documented downstream limit, Appendix
#: A.9); consumed by the image-attachment store and advertised here.
IMAGE_MAX_BYTES = 5 * 1024 * 1024
IMAGE_MAX_WIDTH = 8000
IMAGE_MAX_HEIGHT = 8000

#: Kimi's pinned reference template (§8.6): the explicit ``ReadMediaFile``
#: directive form, because that is the trigger form the pinned 0.29.2 live
#: acceptance exercised (§10.6, round 3).  Bare-path substitution is
#: unproven for kimi and is not claimed.
KIMI_IMAGE_REFERENCE_TEMPLATE = (
    "Use the ReadMediaFile tool to read the image file at {path} and "
    "analyze it in the context of this message."
)

#: Claude's reference template: the documented path-in-prompt flow
#: ("Provide an image path to Claude", Appendix A.9) — the path itself,
#: inserted at the token position.
CLAUDE_IMAGE_REFERENCE_TEMPLATE = "{path}"


def _operator_message_block() -> Dict[str, Any]:
    """The §8.6 operator-message advertisement (same limits for both)."""
    return {
        "supported": True,
        "max_text_bytes": OPERATOR_MESSAGE_MAX_TEXT_BYTES,
        "multiline": True,
        "max_attachments": OPERATOR_MESSAGE_MAX_ATTACHMENTS,
    }


def _interactive_streaming_block() -> Dict[str, Any]:
    """The §6.7 (r15) interactive-streaming advertisement.

    Advertised for any build whose version was observed; the per-terminal
    block is the send authority for a ``payload_class: "interactive"``
    declaration, the top-level union discovery only.
    """
    return {"supported": True}


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
        operator_message=_operator_message_block(),
        image={
            "supported": True,
            # PNG only: the pinned live proof (§10.6, round 3) covers PNG;
            # every other format is refused as unproven rather than assumed
            # (F9).
            "formats": ["png"],
            "max_bytes": IMAGE_MAX_BYTES,
            "max_width": IMAGE_MAX_WIDTH,
            "max_height": IMAGE_MAX_HEIGHT,
            "mechanism": "staged-path-text",
            "reference_template": KIMI_IMAGE_REFERENCE_TEMPLATE,
            "evidence": "live acceptance on pinned 0.29.2 (§10.6)",
        },
        interactive_streaming=_interactive_streaming_block(),
        evidence={
            "compact": "kimi_native_control.CONTROL_COMPACT (adapter pin, imported)",
            "stop": (
                "Kimi Code keyboard reference (design Appendix A.6): Esc closes a "
                "popup / cancels completion / interrupts streaming output or "
                "context compaction; verified live per §10.3 (OD2)"
            ),
            "steer_chords": "kimi_native_control._PROVEN_STEER_CHORDS (consumed, not copied)",
            "dispatch_grace_ms": "control_input_service.NATIVE_KIMI_DISPATCH_GRACE_SECONDS",
            "operator_message": (
                "adapter composer-newline plan, build-pinned for 0.29.0-0.29.2 "
                "(kimi_native_control._PROVEN_COMPOSER_NEWLINE, consumed)"
            ),
            "image": (
                "staged-path PNG via the provider's own ReadMediaFile, proven by "
                "live acceptance on pinned 0.29.2 (design §10.6, round 3); the "
                "reference template is the proven directive phrasing (§8.6)"
            ),
            "interactive_streaming": (
                "§6.7 (r15) declared interactive streaming over the deployed v3 "
                "sequence transport, live-proven on the pinned 0.29.x acceptance "
                "line (§10.1/§10.3)"
            ),
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
        operator_message=_operator_message_block(),
        image={
            "supported": True,
            # The documented set (Anthropic vision limits, Appendix A.9).
            "formats": ["png", "jpeg", "gif", "webp"],
            "max_bytes": IMAGE_MAX_BYTES,
            "max_width": IMAGE_MAX_WIDTH,
            "max_height": IMAGE_MAX_HEIGHT,
            "mechanism": "staged-path-text",
            "reference_template": CLAUDE_IMAGE_REFERENCE_TEMPLATE,
            "evidence": (
                "documented path-in-prompt flow (design Appendix A.9); " "live acceptance per §10.6"
            ),
        },
        interactive_streaming=_interactive_streaming_block(),
        evidence={
            "compact": "claude_native_control.CONTROL_COMPACT (adapter pin, imported)",
            "stop": 'providers/claude_code.py: the TUI shows "esc to interrupt"',
            "steer_chords": "no steer chord is pinned for any claude_code build",
            "operator_message": (
                "adapter composer-newline plan, build-pinned for 2.1.220 "
                "(claude_native_control._PROVEN_COMPOSER_NEWLINE, consumed)"
            ),
            "image": (
                "documented image-via-path flow (design Appendix A.9); CAO-side "
                "limits pinned per F9; live acceptance per §10.6"
            ),
            "interactive_streaming": (
                "§6.7 (r15) declared interactive streaming over the deployed v3 "
                "sequence transport, live-proven on the pinned 2.1.220 acceptance "
                "(§10.1/§10.3)"
            ),
        },
    )


def _codex_entry() -> ProviderControls:
    """The Codex row, pinned to the installed native composer contract."""
    from cli_agent_orchestrator.services import codex_native_control

    return ProviderControls(
        compact=[_text(codex_native_control.CONTROL_COMPACT), _key("Enter")],
        stop=[_key("Escape")],
        steer_chords=(),
        dispatch_grace_ms=None,
        operator_message=_operator_message_block(),
        # Staged-path image delivery is intentionally unadvertised until the
        # native Codex canary proves it on the pinned build.
        image=None,
        interactive_streaming=_interactive_streaming_block(),
        evidence={
            "compact": "codex_native_control.CONTROL_COMPACT (adapter pin, imported)",
            "stop": 'Codex TUI progress footer advertises "esc to interrupt"',
            "steer_chords": "no steer chord is pinned for Codex 0.146.0",
            "operator_message": (
                "Codex 0.146.0 composer Ctrl-J newline binding and 120ms "
                "paste-burst Enter-suppression window"
            ),
            "image": "unadvertised pending build-exact native acceptance",
            "interactive_streaming": (
                "declared streaming over the same build-pinned v3 sequence "
                "transport as native operator messages"
            ),
        },
    )


#: The registry rows.  Compact travels as ordinary composer text through
#: the v3 path — identical to the deployed Compact button — and the kimi
#: adapter's ``control()`` gating on provider-advertised commands applies
#: to the adapter operation path, not this composer-text path.
_REGISTRY = {
    provider_contracts.PROVIDER_CODEX: _codex_entry,
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
    Lane C's ``operator_message``/``image`` blocks (§8.6) follow the same
    rule: present only when the entry carries them for this build.
    """
    block: Dict[str, Any] = {}
    if entry["compact"] is not None:
        block["compact"] = {"events": entry["compact"]}
    if entry["stop"] is not None:
        block["stop"] = {"events": entry["stop"]}
    block["steer_chords"] = list(entry["steer_chords"])
    if entry["dispatch_grace_ms"] is not None:
        block["dispatch_grace_ms"] = entry["dispatch_grace_ms"]
    if entry["operator_message"] is not None:
        block["operator_message"] = dict(entry["operator_message"])
    if entry["image"] is not None:
        block["image"] = dict(entry["image"])
    if entry["interactive_streaming"] is not None:
        block["interactive_streaming"] = dict(entry["interactive_streaming"])
    return block


#: The exact builds whose live acceptance proves image delivery (§10.6,
#: keyed by the canonical wire provider keys this registry speaks).  This
#: table no longer *gates* the image block: staged-path image delivery is
#: text through the composer plan, so an unlisted build advertises the
#: block as a conservative default.  Membership here is recorded on the
#: block as ``build_proven`` — the difference between "live-accepted on
#: this exact build" and "advertised under the unpinned default" is a fact
#: a consumer is entitled to read.
IMAGE_PROVEN_BUILDS: Dict[str, tuple] = {
    provider_contracts.PROVIDER_KIMI_CLI: ("0.29.2",),
    provider_contracts.PROVIDER_CLAUDE_CODE: ("2.1.220",),
}


def _normalized_build(provider_version: Optional[str]) -> Optional[str]:
    """The observed build, or None when there was no successful observation.

    ``normalized_version`` answers "" for an unparseable banner; here that
    is collapsed into None so "unparseable" and "absent" — the two failed
    observations — take the same withholding path, distinct from any
    unlisted-but-observed build.
    """
    if provider_version is None:
        return None
    try:
        return provider_contracts.normalized_version(provider_version) or None
    except Exception:
        return None


def controls_for(provider: str, provider_version: Optional[str]) -> Optional[ProviderControls]:
    """The send-authority entry for ``provider`` at an exact build.

    Build-exact where the underlying fact is build-exact (F11): steer
    chords resolve through the adapter's own table plus its runtime bundle
    read, so a build whose bundle cannot be read gets the entry with an
    empty chord set — never the union of all builds.  The text/image
    blocks follow the version observation, with ``build_proven`` recorded
    on the image block.  ``None`` means the provider has no registry entry
    at all (no Compact/Stop/Steer is deliverable to it through the managed
    path).
    """
    builder = _REGISTRY.get(provider)
    if builder is None:
        return None
    # The builders return a fresh entry per call, so resolving the chords
    # into it here cannot leak a per-build set into a later call.
    entry = builder()
    entry["steer_chords"] = _adapter_steer_chords(provider, provider_version)
    normalized = _normalized_build(provider_version)
    if normalized is None:
        # A failed observation withholds the delivery blocks; the launch
        # boundary refuses such a build anyway, so this is defense in
        # depth for a caller that bypassed it.
        entry["operator_message"] = None
        entry["image"] = None
        entry["interactive_streaming"] = None
        return entry
    if entry["image"] is not None:
        entry["image"]["build_proven"] = normalized in IMAGE_PROVEN_BUILDS.get(provider, ())
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
