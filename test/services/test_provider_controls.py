"""Tests for the §4 provider-control registry.

The registry is the single source for Compact/Stop/Steer precisely
because it *consumes* the adapters' pins instead of restating them.
These tests are the other side of that bargain: they restate the exact
wire facts independently (a test may pin a literal; production code may
not), so a drift on either side fails here rather than shipping.
"""

import pytest

from cli_agent_orchestrator.services import (
    claude_native_control,
    control_input_service,
    kimi_native_control,
    provider_contracts,
    provider_controls,
)

KIMI = provider_contracts.PROVIDER_KIMI_CLI
CLAUDE = provider_contracts.PROVIDER_CLAUDE_CODE

# The pinned v3 sequences, restated exactly as a client sends them.
COMPACT_EVENTS = [{"type": "text", "text": "/compact"}, {"type": "key", "key": "Enter"}]
STOP_EVENTS = [{"type": "key", "key": "Escape"}]

KIMI_PINNED_BUILDS = ("0.29.0", "0.29.1", "0.29.2")


class TestSendAuthority:
    """``controls_for`` resolves a provider at an exact build."""

    def test_kimi_compact_and_stop_are_the_pinned_sequences(self):
        entry = provider_controls.controls_for(KIMI, "0.29.2")
        assert entry["compact"] == COMPACT_EVENTS
        assert entry["stop"] == STOP_EVENTS

    def test_claude_compact_and_stop_are_the_pinned_sequences(self):
        entry = provider_controls.controls_for(CLAUDE, "2.1.220")
        assert entry["compact"] == COMPACT_EVENTS
        assert entry["stop"] == STOP_EVENTS

    def test_the_kimi_compact_text_is_the_adapters_own_pin(self):
        """Object identity, not equality: restating ``"/compact"`` in the
        registry would fork the one fact both sides must hold."""
        entry = provider_controls.controls_for(KIMI, "0.29.2")
        assert entry["compact"][0]["text"] is kimi_native_control.CONTROL_COMPACT

    def test_the_claude_compact_text_is_the_adapters_own_pin(self):
        entry = provider_controls.controls_for(CLAUDE, "2.1.220")
        assert entry["compact"][0]["text"] is claude_native_control.CONTROL_COMPACT

    @pytest.mark.parametrize("build", KIMI_PINNED_BUILDS)
    def test_a_proven_kimi_build_gets_its_steer_chords(self, build):
        assert provider_controls.controls_for(KIMI, build)["steer_chords"] == ("C-s",)

    def test_an_unpinned_kimi_build_gets_no_steer_chords(self):
        """Build-exact (F11): an unproven build gets the empty set, never
        the union of all builds — a guessed chord is refused, not sent."""
        assert provider_controls.controls_for(KIMI, "9.9.9")["steer_chords"] == ()

    def test_an_unknown_kimi_version_gets_no_steer_chords(self):
        assert provider_controls.controls_for(KIMI, None)["steer_chords"] == ()

    def test_claude_has_no_steer_chords_on_any_build(self):
        assert provider_controls.controls_for(CLAUDE, "2.1.220")["steer_chords"] == ()
        assert provider_controls.controls_for(CLAUDE, None)["steer_chords"] == ()

    @pytest.mark.parametrize(
        "provider,version",
        [(provider_contracts.PROVIDER_CODEX, "0.145.0"), ("no_such_provider", "1.0")],
    )
    def test_a_provider_without_a_native_adapter_has_no_entry(self, provider, version):
        """No adapter and no launch binder means no Compact/Stop/Steer is
        deliverable through the managed path (§13 OD3)."""
        assert provider_controls.controls_for(provider, version) is None

    def test_the_kimi_dispatch_grace_is_the_service_constant_in_ms(self):
        entry = provider_controls.controls_for(KIMI, "0.29.2")
        assert entry["dispatch_grace_ms"] == 5000
        # Imported, not hardcoded: the registry consumes the service's pin.
        assert entry["dispatch_grace_ms"] == int(
            control_input_service.NATIVE_KIMI_DISPATCH_GRACE_SECONDS * 1000
        )

    def test_claude_has_no_dispatch_grace(self):
        assert provider_controls.controls_for(CLAUDE, "2.1.220")["dispatch_grace_ms"] is None


class TestSupportedVersionGating:
    """A registry row is only as broad as the builds the provider contract
    accepts: every supported build resolves, and to the pinned answer.

    ``SUPPORTED_VERSIONS`` is keyed by the short provider names
    (``kimi``/``claude``) while the registry is keyed by the canonical
    wire keys (``kimi_cli``/``claude_code``) — the two namespaces are
    deliberately not merged, so the crossing is named here once.
    """

    @pytest.mark.parametrize(
        "build", provider_contracts.SUPPORTED_VERSIONS[provider_contracts.PROVIDER_KIMI]
    )
    def test_every_supported_kimi_build_yields_the_proven_chords(self, build):
        assert provider_controls.controls_for(KIMI, build)["steer_chords"] == ("C-s",)

    @pytest.mark.parametrize(
        "build", provider_contracts.SUPPORTED_VERSIONS[provider_contracts.PROVIDER_CLAUDE]
    )
    def test_every_supported_claude_build_yields_no_chords(self, build):
        entry = provider_controls.controls_for(CLAUDE, build)
        assert entry is not None
        assert entry["steer_chords"] == ()


class TestDiscoveryWireShape:
    """``advertised_provider_controls`` is the §3.5 capabilities block:
    discovery only, unioned over builds — it never licenses a send."""

    def test_the_advertised_block_matches_the_wire_shape_exactly(self):
        assert provider_controls.advertised_provider_controls() == {
            KIMI: {
                "compact": {"events": COMPACT_EVENTS},
                "stop": {"events": STOP_EVENTS},
                "steer_chords": ["C-s"],
                "dispatch_grace_ms": 5000,
            },
            CLAUDE: {
                "compact": {"events": COMPACT_EVENTS},
                "stop": {"events": STOP_EVENTS},
                "steer_chords": [],
            },
        }

    def test_an_absent_fact_is_omitted_not_nulled(self):
        """Additive-advertisement discipline: claude has no dispatch
        grace, so the key is absent from its block entirely."""
        claude_block = provider_controls.advertised_provider_controls()[CLAUDE]
        assert "dispatch_grace_ms" not in claude_block

    def test_the_advertised_chords_union_the_proven_kimi_builds(self):
        # The union tells a client chord events exist before it has named
        # a build; the per-terminal block stays the send authority.
        kimi_block = provider_controls.advertised_provider_controls()[KIMI]
        assert kimi_block["steer_chords"] == ["C-s"]

    def test_the_wire_shape_carries_no_internal_evidence(self):
        for block in provider_controls.advertised_provider_controls().values():
            assert "evidence" not in block


class TestPerTerminalBlock:
    """``controls_block_for`` is the same wire shape resolved build-exact —
    the per-terminal send authority on the control-identity route."""

    def test_a_proven_build_advertises_its_chords(self):
        block = provider_controls.controls_block_for(KIMI, "0.29.1")
        assert block == {
            "compact": {"events": COMPACT_EVENTS},
            "stop": {"events": STOP_EVENTS},
            "steer_chords": ["C-s"],
            "dispatch_grace_ms": 5000,
        }

    def test_an_unpinned_build_advertises_no_chords(self):
        block = provider_controls.controls_block_for(KIMI, "9.9.9")
        assert block["steer_chords"] == []

    def test_an_unknown_version_advertises_no_chords(self):
        assert provider_controls.controls_block_for(KIMI, None)["steer_chords"] == []

    def test_sequences_travel_wrapped_so_the_block_can_grow(self):
        block = provider_controls.controls_block_for(CLAUDE, "2.1.220")
        assert block["compact"] == {"events": COMPACT_EVENTS}
        assert block["stop"] == {"events": STOP_EVENTS}
        assert "dispatch_grace_ms" not in block
        assert "evidence" not in block

    def test_a_provider_without_an_entry_has_no_block(self):
        codex = provider_contracts.PROVIDER_CODEX
        assert provider_controls.controls_block_for(codex, "0.145.0") is None


class TestEvidence:
    """Every registry fact names its source pointer, so a reviewer can
    check the entry without re-walking the tree."""

    def test_the_kimi_entry_names_its_adapter_pins(self):
        evidence = provider_controls.controls_for(KIMI, "0.29.2")["evidence"]
        assert evidence["compact"] == "kimi_native_control.CONTROL_COMPACT (adapter pin, imported)"
        assert evidence["steer_chords"] == (
            "kimi_native_control._PROVEN_STEER_CHORDS (consumed, not copied)"
        )
        assert evidence["dispatch_grace_ms"] == (
            "control_input_service.NATIVE_KIMI_DISPATCH_GRACE_SECONDS"
        )
        assert "keyboard reference" in evidence["stop"]

    def test_the_claude_entry_names_its_adapter_pins(self):
        evidence = provider_controls.controls_for(CLAUDE, "2.1.220")["evidence"]
        assert evidence["compact"] == (
            "claude_native_control.CONTROL_COMPACT (adapter pin, imported)"
        )
        assert "esc to interrupt" in evidence["stop"]
        assert evidence["steer_chords"] == "no steer chord is pinned for any claude_code build"
        # No dispatch grace exists for claude, so no evidence names one.
        assert "dispatch_grace_ms" not in evidence
