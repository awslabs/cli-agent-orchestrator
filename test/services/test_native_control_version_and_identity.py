"""Native control: the version the binding records, and the mode it really is.

Two defects from one fresh installed acceptance. Bind and readiness both
proved, then initial admission refused with ``composer_newline_unproven``
and zero task bytes; and the same generation could not have been reached by
control input even if admission had succeeded.

1. The durable binding records the provider banner verbatim --
   ``2.1.220 (Claude Code)``, which is what the provider printed -- while
   the composer pin table is keyed by the bare build ``2.1.220`` and the
   adapters looked it up with a raw ``.strip()``. An installed, pinned,
   proven build was therefore refused as unproven. The bind-time version
   pin passed on the same string, because it normalizes.

2. ``resolve_control_identity`` classified every managed reservation as
   ``acp`` with both identities null, so a managed native pane was refused
   by the generic path and unreachable by control input at all.

Both are asserted from the durable value rather than a convenient one: the
banner here is the exact string the binding stores.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from cli_agent_orchestrator.services import claude_native_control as claude
from cli_agent_orchestrator.services import control_input_service as cis
from cli_agent_orchestrator.services import kimi_native_control as kimi
from cli_agent_orchestrator.services import provider_contracts

#: Exactly what the provider prints and the binding stores.
CLAUDE_BANNER = "2.1.220 (Claude Code)"
CLAUDE_BARE = "2.1.220"
KIMI_BANNER = "kimi 0.29.0"
KIMI_BARE = "0.29.0"
NATIVE_SESSION = "3e9989ee-6465-4f80-8eee-6837f225bf37"
PROVIDER_PROCESS = "4242@Jul 25 20:00:00 2026"


class TestTheDurableBannerResolvesThePin:
    """Able to fail at the deployed head, on the exact installed string."""

    def test_the_claude_banner_resolves_the_bare_keyed_pin(self):
        plan = claude.plan_composer_keystrokes("one\ntwo", provider_version=CLAUDE_BANNER)

        assert plan["undeliverable_reason"] is None
        assert plan["deliverable"] is True
        assert plan["soft_newline_keystroke"] == "C-j"

    def test_the_bare_version_still_resolves(self):
        """The form that already worked must keep working."""
        plan = claude.plan_composer_keystrokes("one\ntwo", provider_version=CLAUDE_BARE)

        assert plan["undeliverable_reason"] is None

    def test_a_kimi_banner_resolves_the_bare_keyed_pin(self):
        plan = kimi.plan_composer_keystrokes("one\ntwo", provider_version=f"kimi {KIMI_BARE}")

        assert plan["undeliverable_reason"] is None

    def test_the_normalizer_is_the_one_the_version_pin_uses(self):
        """One rule, not a third spelling of it.

        If these ever disagree, a build can pass the bind-time pin and be
        refused at the adapter again -- which is this defect exactly.
        """
        assert provider_contracts.normalized_version(CLAUDE_BANNER) == CLAUDE_BARE


class TestThePinIsNotWidened:
    """Normalizing the lookup input is not loosening the pin."""

    def test_an_unproven_build_is_still_refused(self):
        plan = claude.plan_composer_keystrokes("one\ntwo", provider_version="9.9.9 (Claude Code)")

        assert plan["undeliverable_reason"] is not None
        assert plan["deliverable"] is False

    def test_an_unparseable_version_is_still_refused(self):
        plan = claude.plan_composer_keystrokes("one\ntwo", provider_version="not-a-version")

        assert plan["undeliverable_reason"] is not None
        assert plan["deliverable"] is False

    def test_the_tables_hold_only_bare_exact_versions(self):
        """A table of proven builds, never a table of spellings."""
        for table in (claude._PROVEN_COMPOSER_NEWLINE, kimi._PROVEN_COMPOSER_NEWLINE):
            assert table
            for key in table:
                assert key == provider_contracts.normalized_version(key), key
                assert " " not in key and "(" not in key, key


class TestBothAdaptersAnswerTheSameQuestionTheSameWay:
    """One consumer asks both plans whether this build's composer is proven.

    The control path refuses to type at a composer whose behaviour was
    never read, and it decides that from ``composer_evidence`` on the
    plan.  A field only one adapter published would read as "unproven
    build" for the other one permanently -- refusing every healthy
    generation that provider has, which is a rule about proof class
    turned against a provider it was never about.  Kimi is the provider
    that failure would have hit, so it is the reason this is asserted
    across both rather than on whichever adapter happened to be read
    first.
    """

    @pytest.mark.parametrize(
        "adapter, banner",
        [(claude, CLAUDE_BANNER), (kimi, KIMI_BANNER)],
        ids=["claude", "kimi"],
    )
    def test_a_pinned_build_publishes_its_composer_evidence(self, adapter, banner):
        plan = adapter.plan_composer_keystrokes("/compact", provider_version=banner)

        assert plan["composer_evidence"] is not None
        assert plan["soft_newline_keystroke"] is not None

    @pytest.mark.parametrize("adapter", [claude, kimi], ids=["claude", "kimi"])
    def test_an_unpinned_build_publishes_no_evidence(self, adapter):
        """The gate must stay falsifiable, or it refuses nothing."""
        plan = adapter.plan_composer_keystrokes("/compact", provider_version="9.9.9")

        assert plan["composer_evidence"] is None
        assert plan["soft_newline_keystroke"] is None

    @pytest.mark.parametrize("adapter", [claude, kimi], ids=["claude", "kimi"])
    def test_the_evidence_is_journaled_with_the_plan(self, adapter):
        """A keystroke recorded without its evidence is a claim, not a proof."""
        banner = CLAUDE_BANNER if adapter is claude else KIMI_BANNER
        plan = adapter.plan_composer_keystrokes("/compact", provider_version=banner)

        journalable = adapter._journalable_plan(plan)

        assert journalable["composer_evidence"] == plan["composer_evidence"]
        assert "lines" not in journalable


class TestTheControlIdentityTellsTheTruth:
    """A managed native generation is native, with the identities it proved."""

    def _resolve(self, monkeypatch, managed: Optional[Dict[str, Any]]):
        monkeypatch.setattr(
            cis,
            "_terminal_metadata",
            lambda _tid: {
                "generation": "9b87898c-2b31-49de-8cea-abbd5af574d0",
                "provider": "claude_code",
                "pane_id": "%32",
                "tmux_session": "cao-chess-shakedown",
                "server_socket_path": "/private/tmp/tmux-501/default",
            },
        )
        monkeypatch.setattr(cis, "_managed_identity", lambda _tid: managed)
        # No tmux in a unit test; the live-pane read is not what is under
        # test here and an absent client leaves it unresolved by design.
        monkeypatch.setattr(cis, "_tmux_client", lambda: None)
        return cis.resolve_control_identity("eb481cbe")

    def _managed(self, **overrides) -> Dict[str, Any]:
        record = {
            "reservation_id": "r-1",
            "terminal_id": "eb481cbe",
            "generation": "9b87898c-2b31-49de-8cea-abbd5af574d0",
            "provider": "claude_code",
            "state": "admitted",
            "controllable": True,
            "vintage": "v2",
            "execution_mode": "native_tui",
            "native_session_id": NATIVE_SESSION,
            "provider_process_id": PROVIDER_PROCESS,
        }
        record.update(overrides)
        return record

    def test_a_managed_native_generation_projects_native_tui(self, monkeypatch):
        """The reported defect: this projected ``acp`` with nulls."""
        resolved = self._resolve(monkeypatch, self._managed())

        assert resolved.execution_mode == cis.EXECUTION_MODE_NATIVE_TUI
        assert resolved.managed is True

    def test_it_publishes_the_identities_its_readiness_proved(self, monkeypatch):
        resolved = self._resolve(monkeypatch, self._managed())

        assert resolved.native_session_id == NATIVE_SESSION
        assert resolved.provider_process_id == PROVIDER_PROCESS

    def test_a_managed_acp_generation_still_projects_acp(self, monkeypatch):
        """The half that must not move."""
        resolved = self._resolve(
            monkeypatch,
            self._managed(execution_mode="acp", native_session_id=None, provider_process_id=None),
        )

        assert resolved.execution_mode == cis.EXECUTION_MODE_ACP
        assert resolved.native_session_id is None

    def test_an_unresolvable_mode_reports_the_refusing_branch(self, monkeypatch):
        """Default-deny. An unknown mode must never reach the composer.

        Routing on an unresolved mode is how a native pane would be typed
        into as though it were an ACP bridge -- or the reverse.
        """
        resolved = self._resolve(monkeypatch, self._managed(execution_mode=None))

        assert resolved.execution_mode == cis.EXECUTION_MODE_ACP

    def test_an_unknown_mode_string_also_refuses(self, monkeypatch):
        resolved = self._resolve(monkeypatch, self._managed(execution_mode="something-else"))

        assert resolved.execution_mode == cis.EXECUTION_MODE_ACP

    def test_an_unmanaged_pane_is_still_a_plain_native_tui(self, monkeypatch):
        """No managed record means no bridge in front of it."""
        resolved = self._resolve(monkeypatch, None)

        assert resolved.execution_mode == cis.EXECUTION_MODE_NATIVE_TUI
        assert resolved.managed is False
        assert resolved.native_session_id is None


class TestTheGuaranteesAreUnchanged:
    """None of this may weaken what already screens a control request."""

    @pytest.mark.parametrize("sentinel", ["\x1b[200~", "\x1b[201~"])
    def test_bracketed_paste_framing_is_still_refused(self, sentinel):
        screened = cis.screen_control_text(f"/compact{sentinel}")

        assert screened is not None
        assert screened[0] == cis.REASON_ILLEGAL_CONTROL_BYTES

    def test_ordinary_control_text_is_still_accepted(self):
        assert cis.screen_control_text("/compact") is None

    @pytest.mark.parametrize("adapter", [claude, kimi])
    def test_a_sentinel_never_reaches_an_adapter_plan(self, adapter):
        """The adapters refuse it too, independently of the screen."""
        with pytest.raises(adapter.NativeControlInvalid):
            adapter.plan_composer_keystrokes("/compact\x1b[200~", provider_version=CLAUDE_BANNER)


class TestTheGateIsAPositiveAllowlist:
    """Only a resolved native TUI may be typed into. Everything else refuses.

    Tested against the gate directly rather than through the projection,
    because the projection is what a future edit changes. A deny-list on
    the literal ``acp`` refuses that one spelling and lets every other
    value through to the composer -- including a legacy managed bridge row
    whose durable mode is NULL, which is the case that costs the most,
    since a bridge pane typed into as though it were a native composer is
    exactly what this refusal exists to prevent.
    """

    def _identity(self, mode: Optional[str]) -> cis.ResolvedControlIdentity:
        return cis.ResolvedControlIdentity(
            terminal_id="eb481cbe",
            terminal_incarnation=None,
            terminal_generation="9b87898c-2b31-49de-8cea-abbd5af574d0",
            provider="claude_code",
            native_session_id=NATIVE_SESSION,
            execution_mode=mode,
            session_name="cao-chess-shakedown",
            provider_process_id=PROVIDER_PROCESS,
            pane_id="%32",
            window_id="@32",
            pane_pid=4242,
            pane_dead=False,
            managed=True,
            recorded_pane_id="%32",
            bound_server_socket_path="/private/tmp/tmux-501/default",
            observed_server_socket_path="/private/tmp/tmux-501/default",
        )

    @pytest.mark.parametrize(
        "mode",
        [
            pytest.param("acp", id="acp"),
            pytest.param(None, id="legacy-null-mode"),
            pytest.param("", id="empty"),
            pytest.param("bridge", id="unknown-value"),
            pytest.param("NATIVE_TUI", id="wrong-case"),
        ],
    )
    def test_anything_but_a_resolved_native_tui_is_refused(self, mode):
        screened = cis.screen_expected_identity({}, self._identity(mode))

        assert screened is not None
        assert screened[0] == cis.REASON_MANAGED_ACP_PANE

    def test_a_resolved_native_tui_is_allowed_through(self):
        """The gate must still let the real thing past."""
        assert cis.screen_expected_identity({}, self._identity("native_tui")) is None


class TestALegacyManagedRowNeverProjectsNative:
    """A NULL durable mode is a legacy row, and legacy is the ACP bridge.

    Normalized through the shared mode reader rather than compared here,
    so this cannot disagree with the rest of the codebase about what an
    absent mode means.
    """

    def test_mode_of_record_maps_a_null_mode_to_acp(self):
        from cli_agent_orchestrator.services import execution_mode as em

        assert (
            em.mode_of_record({"execution_mode": None, "execution_mode_source": None})
            == cis.EXECUTION_MODE_ACP
        )

    def test_a_managed_row_with_no_mode_projects_acp(self, monkeypatch):
        monkeypatch.setattr(
            cis,
            "_terminal_metadata",
            lambda _tid: {"generation": "g", "provider": "claude_code", "pane_id": "%32"},
        )
        monkeypatch.setattr(cis, "_tmux_client", lambda: None)
        monkeypatch.setattr(
            cis,
            "_managed_identity",
            lambda _tid: {"generation": "g", "provider": "claude_code"},
        )

        resolved = cis.resolve_control_identity("eb481cbe")

        assert resolved.execution_mode == cis.EXECUTION_MODE_ACP
        assert cis.screen_expected_identity({}, resolved) is not None


class TestTheNativeRouteUsesTheNativeAdapter:
    """A managed native generation must not be typed at by the raw primitive.

    The projection and the allowlist together let a managed native
    generation past the gate.  Before the dispatch existed, the delivery
    path underneath still called ``send_literal_line`` unconditionally --
    so a managed native ``/compact`` would have been typed as a raw
    literal line with no proven newline keystroke, no paste-burst reset
    and no submit settle, and the Enter would have been swallowed with no
    error and no turn.

    That made the projection strictly unsafe to deploy alone: before it a
    managed native pane was refused, after it the pane was reachable by
    the wrong primitive.  The behaviour is proven end-to-end against a
    real bound generation in
    ``test_native_control_identity_projection.py``; what is pinned here is
    the structural requirement that the two halves stay together, since a
    later edit could quietly restore the raw write and still pass every
    outcome-shaped assertion.
    """

    def test_the_adapters_expose_the_dispatch_entry_point(self):
        """Both adapters must offer the store-free plan executor.

        The control path journals in its own record, so it needs the
        keystroke sequence without the adapter's operation store. An
        adapter that only exposed the journaling entry points would force
        a second at-most-once record for one control.
        """
        for adapter in (claude, kimi):
            assert callable(adapter.execute_composer_plan)
            assert issubclass(adapter.ComposerWriteInterrupted, adapter.NativeControlError)

    def test_the_delivery_path_reaches_a_provider_adapter(self):
        """Named in the preflight, which is where the plan is built."""
        import inspect

        source = inspect.getsource(cis._native_composer_preflight) + inspect.getsource(
            cis._send_through_native_adapter
        )

        assert "plan_composer_keystrokes" in source
        assert "execute_composer_plan" in source

    def test_the_raw_primitive_is_not_the_native_route(self):
        """The generic write stays for unmanaged panes and only for them."""
        import inspect

        assert "send_literal_line" not in inspect.getsource(cis._native_composer_preflight)
