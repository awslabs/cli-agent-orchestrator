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
    codex_native_control,
    control_input_service,
    kimi_native_control,
    provider_contracts,
    provider_controls,
)

KIMI = provider_contracts.PROVIDER_KIMI_CLI
CLAUDE = provider_contracts.PROVIDER_CLAUDE_CODE
CODEX = provider_contracts.PROVIDER_CODEX

# The pinned v3 sequences, restated exactly as a client sends them.
COMPACT_EVENTS = [{"type": "text", "text": "/compact"}, {"type": "key", "key": "Enter"}]
STOP_EVENTS = [{"type": "key", "key": "Escape"}]

KIMI_PINNED_BUILDS = ("0.29.0", "0.29.1", "0.29.2", "0.30.0", "0.31.0", "0.32.0", "0.33.0")

# The §8.6 Lane C blocks, restated exactly (a test may pin a literal;
# production code may not).
OPERATOR_MESSAGE_BLOCK = {
    "supported": True,
    "max_text_bytes": 8192,
    "multiline": True,
    "max_attachments": 4,
}
KIMI_IMAGE_BLOCK = {
    "supported": True,
    "formats": ["png"],
    "max_bytes": 5242880,
    "max_width": 8000,
    "max_height": 8000,
    "mechanism": "staged-path-text",
    "reference_template": (
        "Use the ReadMediaFile tool to read the image file at {path} and "
        "analyze it in the context of this message."
    ),
    "evidence": "live acceptance on pinned 0.29.2 (§10.6)",
}
#: The per-terminal image block carries ``build_proven``: the discovery
#: union above never licenses a send, so it does not.
KIMI_IMAGE_BLOCK_PROVEN = {**KIMI_IMAGE_BLOCK, "build_proven": True}
KIMI_IMAGE_BLOCK_UNPROVEN = {**KIMI_IMAGE_BLOCK, "build_proven": False}
CLAUDE_IMAGE_BLOCK = {
    "supported": True,
    "formats": ["png", "jpeg", "gif", "webp"],
    "max_bytes": 5242880,
    "max_width": 8000,
    "max_height": 8000,
    "mechanism": "staged-path-text",
    "reference_template": "{path}",
    "evidence": (
        "documented path-in-prompt flow (design Appendix A.9); " "live acceptance per §10.6"
    ),
}


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

    def test_codex_compact_and_stop_are_the_pinned_sequences(self):
        entry = provider_controls.controls_for(CODEX, "0.146.0")
        assert entry["compact"] == COMPACT_EVENTS
        assert entry["stop"] == STOP_EVENTS
        assert entry["compact"][0]["text"] is codex_native_control.CONTROL_COMPACT

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
        [("no_such_provider", "1.0")],
    )
    def test_a_provider_without_a_native_adapter_has_no_entry(self, provider, version):
        """No adapter means no Compact/Stop/Steer is deliverable."""
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
            CODEX: {
                "compact": {"events": COMPACT_EVENTS},
                "stop": {"events": STOP_EVENTS},
                "steer_chords": [],
                "operator_message": OPERATOR_MESSAGE_BLOCK,
                "interactive_streaming": {"supported": True},
            },
            KIMI: {
                "compact": {"events": COMPACT_EVENTS},
                "stop": {"events": STOP_EVENTS},
                "steer_chords": ["C-s"],
                "dispatch_grace_ms": 5000,
                "operator_message": OPERATOR_MESSAGE_BLOCK,
                "image": KIMI_IMAGE_BLOCK,
                "interactive_streaming": {"supported": True},
            },
            CLAUDE: {
                "compact": {"events": COMPACT_EVENTS},
                "stop": {"events": STOP_EVENTS},
                "steer_chords": [],
                "operator_message": OPERATOR_MESSAGE_BLOCK,
                "image": CLAUDE_IMAGE_BLOCK,
                "interactive_streaming": {"supported": True},
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
        # 0.29.2 is the exact build whose live acceptance proves image
        # delivery, so it is the one whose image block reads build_proven.
        block = provider_controls.controls_block_for(KIMI, "0.29.2")
        assert block == {
            "compact": {"events": COMPACT_EVENTS},
            "stop": {"events": STOP_EVENTS},
            "steer_chords": ["C-s"],
            "dispatch_grace_ms": 5000,
            "operator_message": OPERATOR_MESSAGE_BLOCK,
            "image": KIMI_IMAGE_BLOCK_PROVEN,
            "interactive_streaming": {"supported": True},
        }

    def test_an_unpinned_build_advertises_no_chords(self):
        block = provider_controls.controls_block_for(KIMI, "9.9.9")
        assert block["steer_chords"] == []

    @pytest.mark.parametrize("build", ["0.30.0", "0.31.0", "0.32.0", "0.33.0", "0.34.0"])
    def test_a_text_proven_build_advertises_image_as_unproven(self, build):
        # cond-0310/cond-0315/cond-0331: these builds are
        # text/multiline/steer proven.  Under the unpinned policy the image
        # block is advertised for any observed build — staged-path image
        # delivery is text riding the composer plan — but ``build_proven``
        # records that only 0.29.2 holds the live acceptance, so a consumer
        # can tell the default from the proof.
        block = provider_controls.controls_block_for(KIMI, build)
        assert block["steer_chords"] == ["C-s"]
        assert block["operator_message"] == OPERATOR_MESSAGE_BLOCK
        assert block["image"] == KIMI_IMAGE_BLOCK_UNPROVEN

    def test_an_unknown_version_advertises_no_chords(self):
        assert provider_controls.controls_block_for(KIMI, None)["steer_chords"] == []

    def test_sequences_travel_wrapped_so_the_block_can_grow(self):
        block = provider_controls.controls_block_for(CLAUDE, "2.1.220")
        assert block["compact"] == {"events": COMPACT_EVENTS}
        assert block["stop"] == {"events": STOP_EVENTS}
        assert "dispatch_grace_ms" not in block
        assert "evidence" not in block

    def test_codex_has_a_build_exact_native_control_block(self):
        block = provider_controls.controls_block_for(CODEX, "0.146.0")
        assert block["compact"] == {"events": COMPACT_EVENTS}
        assert block["stop"] == {"events": STOP_EVENTS}
        assert block["operator_message"] == OPERATOR_MESSAGE_BLOCK
        assert "image" not in block

    def test_a_narrowly_bind_proven_codex_build_advertises_text_only(self):
        """0.147.0 may bind a native session; its control block follows the
        observation rule.

        The native-bind capability table and these control tables are
        independent.  Bind proof grants no steer chords (Codex has no chord
        table at all) and no image block (Codex image delivery is
        intentionally unadvertised until the native canary proves it); the
        operator-message block is advertised for any observed build as the
        conservative default.
        """
        block = provider_controls.controls_block_for(CODEX, "0.147.0")
        assert block["steer_chords"] == []
        assert block["operator_message"] == OPERATOR_MESSAGE_BLOCK
        assert "image" not in block


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


class TestLaneCBlocks:
    """The §8.6 additive capability blocks: advertised for any observed
    build, with ``build_proven`` recording which builds hold the live
    acceptance; absent entirely only where the version was never observed
    (never nulled on the wire)."""

    def test_kimi_image_is_png_only_with_the_proven_template(self):
        block = provider_controls.controls_block_for(KIMI, "0.29.2")
        assert block["image"] == KIMI_IMAGE_BLOCK_PROVEN
        # The pinned directive phrasing, because that is the trigger form
        # the pinned 0.29.2 live acceptance exercised (§8.6): bare-path
        # substitution is unproven for kimi and is not claimed.
        assert "{path}" in block["image"]["reference_template"]
        assert block["image"]["reference_template"].startswith("Use the ReadMediaFile tool")

    def test_claude_image_advertises_the_documented_formats(self):
        block = provider_controls.controls_block_for(CLAUDE, "2.1.220")
        assert block["image"] == {**CLAUDE_IMAGE_BLOCK, "build_proven": True}
        assert block["image"]["reference_template"] == "{path}"

    @pytest.mark.parametrize("build", KIMI_PINNED_BUILDS)
    def test_every_supported_kimi_build_advertises_the_message_block(self, build):
        # The text plan is proven across the 0.29.x line (the adapter's
        # build-pinned composer-newline table), so all three advertise it.
        block = provider_controls.controls_block_for(KIMI, build)
        assert block["operator_message"] == OPERATOR_MESSAGE_BLOCK

    @pytest.mark.parametrize("build", ["0.29.0", "0.29.1"])
    def test_a_text_proven_but_image_unproven_kimi_build_marks_image_unproven(self, build):
        """Image delivery's live acceptance covers 0.29.2 alone (§10.6):
        the older text-proven builds advertise the image block under the
        unpinned default and say so — ``build_proven`` is False, rather
        than the block silently claiming 0.29.2's acceptance."""
        block = provider_controls.controls_block_for(KIMI, build)
        assert block["operator_message"] == OPERATOR_MESSAGE_BLOCK
        assert block["image"] == KIMI_IMAGE_BLOCK_UNPROVEN

    def test_only_the_image_proven_kimi_build_marks_image_proven(self):
        for build in KIMI_PINNED_BUILDS:
            block = provider_controls.controls_block_for(KIMI, build)
            assert block["image"] == (
                KIMI_IMAGE_BLOCK_PROVEN if build == "0.29.2" else KIMI_IMAGE_BLOCK_UNPROVEN
            )

    def test_0300_advertises_text_interactive_and_unproven_image(self):
        """cond-0198: the proven 0.30.0 build advertises the text/control
        blocks and the §6.7 interactive block with its steer chords; the
        image block rides the conservative default and says so."""
        block = provider_controls.controls_block_for(KIMI, "0.30.0")
        assert block["operator_message"] == OPERATOR_MESSAGE_BLOCK
        assert block["interactive_streaming"] == {"supported": True}
        assert block["steer_chords"] == ["C-s"]
        assert block["image"] == KIMI_IMAGE_BLOCK_UNPROVEN

    def test_operator_message_limits_are_the_spec_pins(self):
        block = provider_controls.controls_block_for(CLAUDE, "2.1.220")
        assert block["operator_message"]["max_text_bytes"] == 8192
        assert block["operator_message"]["multiline"] is True
        assert block["operator_message"]["max_attachments"] == 4

    def test_an_unlisted_build_advertises_the_blocks_as_unproven(self):
        """The conservative default is not the null value: an observed but
        unlisted build keeps the delivery blocks, with the image block
        marked unproven."""
        entry = provider_controls.controls_for(KIMI, "9.9.9")
        assert entry["operator_message"] is not None
        assert entry["image"] == KIMI_IMAGE_BLOCK_UNPROVEN
        block = provider_controls.controls_block_for(KIMI, "9.9.9")
        assert block["operator_message"] == OPERATOR_MESSAGE_BLOCK
        assert block["image"] == KIMI_IMAGE_BLOCK_UNPROVEN

    def test_an_unknown_version_omits_the_blocks(self):
        """A failed observation (no version at all) still withholds."""
        block = provider_controls.controls_block_for(KIMI, None)
        assert "operator_message" not in block
        assert "image" not in block

    def test_codex_native_text_is_advertised_but_image_waits_for_acceptance(self):
        block = provider_controls.controls_block_for(CODEX, "0.146.0")
        assert block["operator_message"] == OPERATOR_MESSAGE_BLOCK
        assert block["interactive_streaming"] == {"supported": True}
        assert "image" not in block

    def test_the_entries_name_their_lane_c_evidence(self):
        kimi_evidence = provider_controls.controls_for(KIMI, "0.29.2")["evidence"]
        assert "ReadMediaFile" in kimi_evidence["image"]
        assert "0.29.2" in kimi_evidence["image"]
        assert "_PROVEN_COMPOSER_NEWLINE" in kimi_evidence["operator_message"]
        claude_evidence = provider_controls.controls_for(CLAUDE, "2.1.220")["evidence"]
        assert "Appendix A.9" in claude_evidence["image"]
        assert "_PROVEN_COMPOSER_NEWLINE" in claude_evidence["operator_message"]
