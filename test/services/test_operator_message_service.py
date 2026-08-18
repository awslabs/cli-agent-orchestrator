"""Tests for the §8.3 operator-message service pipeline.

The image-attachment store and the provider adapters have their own
suites; this one pins the service's own contract: request shape, the
typed refusal taxonomy, capability gating, token-map substitution with
container translation, the under-lease guard sequence, replay/response-
loss semantics, and the zero-retry invariant.  Control-path primitives
are mocked at the ``control_input_service`` boundary exactly as the
deployed inbox-payload suite mocks them.
"""

from __future__ import annotations

import struct
import uuid
import zlib
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import control_input_service as cis
from cli_agent_orchestrator.services import generation_fence as gf
from cli_agent_orchestrator.services import managed_launch_v2
from cli_agent_orchestrator.services import operator_message_service as oms
from cli_agent_orchestrator.services.control_input_contract import (
    REASON_PANE_BUSY,
)
from cli_agent_orchestrator.services.image_attachments import AttachmentBindingError

TERMINAL = "a1b2c3d4"
GENERATION = "11111111-2222-3333-4444-555555555555"
PANE = "%7"
OP = str(uuid.uuid4())

EXPECTED = {
    "terminal_id": TERMINAL,
    "terminal_incarnation": None,
    "terminal_generation": GENERATION,
    "pane_birth_id": PANE,
    "provider_process_id": "4321@marker",
    "provider": "kimi_cli",
    "native_session_id": "ns-1",
    "execution_mode": "native_tui",
    "session_name": "cao-op",
}


def _resolved(**overrides):
    fields = dict(
        terminal_id=TERMINAL,
        terminal_incarnation=None,
        terminal_generation=GENERATION,
        provider="kimi_cli",
        native_session_id="ns-1",
        execution_mode="native_tui",
        session_name="cao-op",
        provider_process_id="4321@marker",
        provider_version="0.29.2",
        pane_id=PANE,
        window_id="@1",
        pane_pid=4321,
        pane_dead=False,
        recorded_pane_id=PANE,
        bound_server_socket_path="/tmp/op-sock",
        observed_server_socket_path="/tmp/op-sock",
    )
    fields.update(overrides)
    return cis.ResolvedControlIdentity(**fields)


def _op_id():
    return str(uuid.uuid4())


def _record(operation_id, *, state="posted", digest="d" * 64, terminal=TERMINAL, **extra):
    record = {
        "operation_id": operation_id,
        "kind": "operator-message",
        "state": state,
        "terminal_id": terminal,
        "payload_sha256": digest,
        "native_session_id": "ns-1",
        "generation": GENERATION,
        "execution_mode": "native_tui",
        "intent": {},
        "transport": {},
        "refusal_reason": None,
        "ambiguity_reason": None,
    }
    record.update(extra)
    return record


class _FakeAdapter:
    """Records the adapter call; the canned record is its answer.

    Runs the Lane C r1 pre-write hook exactly as the real adapters do: a
    hook refusal becomes the journaled refused record with zero bytes
    typed, and a replay (or a raise) never touches the hook.
    """

    REFUSED_IMAGE_UNKNOWN = "image_attachment_unknown"
    REFUSED_IMAGE_NOT_READY = "image_attachment_not_ready"

    class NativeControlConflict(Exception):
        pass

    class NativeControlInvalid(Exception):
        pass

    def __init__(self, record=None, raises=None, raises_after_hook=False):
        self.calls = []
        self.record = record
        self.raises = raises
        # False: raise before the hook (claim/transport failure); True:
        # raise after a successful hook (the post-binding ambiguity window).
        self.raises_after_hook = raises_after_hook
        self.ambiguous_marks = []

    def turn_observation(self, *, active_turn_id, observed_at, observer):
        return {
            "schema": "cao-kimi-native-turn-observation-v1",
            "active_turn_id": active_turn_id,
            "observed_at": observed_at,
            "observer": observer,
        }

    def operator_message(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None and not self.raises_after_hook:
            raise self.raises
        pre_write = kwargs.get("pre_write")
        if pre_write is not None:
            hook_refusal = pre_write()
            if hook_refusal is not None:
                reason, detail = hook_refusal
                return _record(
                    kwargs["operation_id"],
                    state="refused",
                    refusal_reason=reason,
                    observation={"detail": detail},
                )
        if self.raises is not None:
            raise self.raises
        return self.record

    def mark_ambiguous(self, *, operation_id, reason):
        self.ambiguous_marks.append((operation_id, reason))
        return _record(operation_id, state="ambiguous", ambiguity_reason=reason)


@pytest.fixture
def wire(monkeypatch):
    """Patch the control-path boundary; every test starts with a resolved
    kimi 0.29.2 identity and a provably idle, copy-mode-free pane."""
    with cis._native_kimi_dispatch_guard_lock:
        cis._native_kimi_dispatch_times.clear()
    state = SimpleNamespace(turn_status=TerminalStatus.IDLE, adapter=None, plan=None)
    monkeypatch.setattr(oms, "_find_operation", lambda operation_id: (None, []))

    def install(resolved, adapter, plan, turn_status=TerminalStatus.IDLE):
        state.adapter, state.plan, state.turn_status = adapter, plan, turn_status
        monkeypatch.setattr(cis, "resolve_control_identity", lambda tid: resolved)
        live = SimpleNamespace(
            dead=False,
            window_id=resolved.window_id,
            pane_pid=resolved.pane_pid,
            server_socket_path=resolved.bound_server_socket_path,
        )
        client = SimpleNamespace(
            pane_control_identity=lambda *, pane_id, deadline_monotonic: live,
            pane_in_copy_mode=lambda pane_id, **kwargs: False,
            send_copy_mode_cancel=lambda pane_id, **kwargs: True,
        )
        monkeypatch.setattr(cis, "_tmux_client", lambda: client)
        monkeypatch.setattr(cis, "_copy_mode_guard_refusal", lambda *a, **k: None)
        monkeypatch.setattr(
            cis,
            "_native_composer_preflight",
            lambda *a, **k: (adapter, plan, None) if adapter is not None else (None, None, plan),
        )

        def _observe(provider, **kwargs):
            if isinstance(turn_status, BaseException):
                raise turn_status
            return turn_status

        monkeypatch.setattr(managed_launch_v2, "_observe_turn_state", _observe)
        return state

    return install


def _submit(operation_id=OP, **overrides):
    kwargs = dict(
        operation_id=operation_id,
        text="hello operator",
        attachments=None,
        token_map=None,
        expected_identity=EXPECTED,
    )
    kwargs.update(overrides)
    return oms.submit_operator_message(TERMINAL, **kwargs)


class TestShapeValidation:
    def test_operation_id_must_be_a_uuid(self):
        with pytest.raises(oms.OperatorMessageRequestInvalid):
            _submit(operation_id="not-a-uuid")

    def test_more_than_four_attachments_is_a_shape_error(self):
        with pytest.raises(oms.OperatorMessageRequestInvalid):
            _submit(
                text="x [Image #1]",
                attachments=["a", "b", "c", "d", "e"],
                token_map={"1": "a"},
            )

    def test_a_token_without_a_mapping_is_a_shape_error(self):
        with pytest.raises(oms.OperatorMessageRequestInvalid) as excinfo:
            _submit(text="see [Image #1]", attachments=["att-1"], token_map={})
        assert "no token_map entry" in str(excinfo.value)

    def test_a_mapping_to_an_unlisted_attachment_is_a_shape_error(self):
        with pytest.raises(oms.OperatorMessageRequestInvalid):
            _submit(text="see [Image #1]", attachments=["att-1"], token_map={"1": "att-2"})

    def test_an_unreferenced_attachment_is_a_shape_error(self):
        with pytest.raises(oms.OperatorMessageRequestInvalid) as excinfo:
            _submit(text="plain text", attachments=["att-1"], token_map={})
        assert "no [Image #N] token references it" in str(excinfo.value)

    def test_an_empty_draft_is_a_shape_error(self):
        with pytest.raises(oms.OperatorMessageRequestInvalid):
            _submit(text="   ")

    def test_expected_identity_is_required(self):
        with pytest.raises(oms.OperatorMessageRequestInvalid):
            _submit(expected_identity=None)


class TestTypedRefusalsBeforeIdentity:
    def test_over_8192_bytes_is_message_too_large(self):
        result = _submit(text="x" * 8193)
        assert result.outcome == "refused"
        assert result.reason_code == "message-too-large"
        assert "8193" in result.detail

    def test_exactly_8192_bytes_is_not_too_large(self, wire):
        adapter = _FakeAdapter(
            record=_record(OP, state="refused", refusal_reason="active_turn_in_progress")
        )
        wire(_resolved(), adapter, {"deliverable": True})
        result = _submit(text="x" * 8192)
        assert result.reason_code != "message-too-large"

    def test_escape_bytes_are_refused(self):
        result = _submit(text="hello\x1b[200~paste")
        assert result.outcome == "refused"
        assert result.reason_code == "illegal-control-bytes"


class TestIdentityAndCapabilityGates:
    def test_unknown_terminal(self):
        result = _submit()
        assert result.outcome == "refused"
        assert result.reason_code == "unknown-terminal"

    def test_stale_generation_refuses(self, wire, monkeypatch):
        monkeypatch.setattr(cis, "resolve_control_identity", lambda tid: _resolved())
        expected = {**EXPECTED, "terminal_generation": "00000000-0000-0000-0000-000000000000"}
        result = _submit(expected_identity=expected)
        assert result.outcome == "refused"
        assert result.reason_code == "stale-generation"

    def test_identity_mismatch_refuses(self, wire, monkeypatch):
        monkeypatch.setattr(cis, "resolve_control_identity", lambda tid: _resolved())
        expected = {**EXPECTED, "provider": "claude_code"}
        result = _submit(expected_identity=expected)
        assert result.outcome == "refused"
        assert result.reason_code == "identity-mismatch"

    def test_a_provider_without_a_registry_entry_is_unsupported(self, wire, monkeypatch):
        monkeypatch.setattr(
            cis,
            "resolve_control_identity",
            lambda tid: _resolved(provider="opencode", provider_version="1.2.3"),
        )
        expected = {**EXPECTED, "provider": "opencode"}
        result = _submit(expected_identity=expected)
        assert result.outcome == "refused"
        assert result.reason_code == "provider-unsupported"

    def test_an_unlisted_build_delivers_the_operator_message(self, wire, monkeypatch):
        """Unpinned: the delivery block follows the version observation.

        An unlisted-but-observed build advertises the operator-message
        block as the conservative default, so the submit proceeds; only a
        failed observation withholds it.
        """
        adapter = _FakeAdapter(record=_record(OP))
        wire(_resolved(provider_version="9.9.9"), adapter, {"deliverable": True})
        result = _submit()
        assert result.outcome == "accepted"

    def test_a_failed_version_observation_withholds_the_operator_message(self, wire, monkeypatch):
        """Unparseable is a failed observation, distinct from unlisted."""
        adapter = _FakeAdapter(record=_record(OP))
        wire(_resolved(provider_version="not-a-version"), adapter, {"deliverable": True})
        result = _submit()
        assert result.outcome == "refused"
        assert result.reason_code == "provider-unsupported"

    def test_parked_generation_refuses_before_the_operator_adapter(
        self, wire, monkeypatch, tmp_path
    ):
        """The dashboard/operator lane records no adapter/provider call after park."""
        from cli_agent_orchestrator import constants

        companion = tmp_path / "companion"
        monkeypatch.setattr(constants, "COMPANION_DIR", companion)
        gf.install_fence(
            companion,
            terminal_id=TERMINAL,
            generation=GENERATION,
            vintage="v2",
            request={
                "schema": gf.FENCE_REQUEST_SCHEMA,
                "terminal_generation": GENERATION,
                "obligation_generation": "obligation-1",
                "attempt_id": "attempt-1",
                "intent_id": str(uuid.uuid4()),
                "report_sha256": "a" * 64,
            },
            fencing_token_id="token-1",
        )
        adapter = _FakeAdapter(record=_record(_op_id()))
        wire(_resolved(managed=True), adapter, {"deliverable": True})

        result = _submit(operation_id=_op_id())

        assert result.outcome == "refused"
        assert result.reason_code == "generation-fenced"
        assert adapter.calls == []

    def test_attachments_on_an_unlisted_build_ride_the_conservative_default(
        self, wire, monkeypatch
    ):
        """Unpinned: the image block is advertised with build_proven False.

        Staged-path image delivery is text through the composer plan, so an
        unlisted-but-observed build accepts the attachment; the per-build
        live acceptance is recorded on the block, not enforced by
        withholding.
        """
        adapter = _FakeAdapter(record=_record(OP))
        resolved = _resolved(provider="claude_code", provider_version="1.0.0")
        wire(resolved, adapter, {"deliverable": True})
        record = {"attachment_id": "att-1", "format": "png", "state": "ready"}
        monkeypatch.setattr(
            oms.image_attachments,
            "get_attachment",
            lambda terminal_id, attachment_id: dict(record),
        )
        monkeypatch.setattr(oms.image_attachments, "bind_for_submit", lambda *a: [record])
        monkeypatch.setattr(
            oms.image_attachments,
            "staged_absolute_path",
            lambda record: f"/staged/{record['attachment_id']}.png",
        )
        monkeypatch.setattr(oms, "_terminal_agent_profile", lambda tid: None)
        expected = {**EXPECTED, "provider": "claude_code"}
        result = _submit(
            text="see [Image #1]",
            attachments=["att-1"],
            token_map={"1": "att-1"},
            expected_identity=expected,
        )
        assert result.outcome == "accepted"
        assert adapter.calls[0]["text"] == "see /staged/att-1.png"

    def test_attachments_without_a_version_observation_are_unsupported(self, wire, monkeypatch):
        """A failed observation withholds the image block."""
        monkeypatch.setattr(
            cis,
            "resolve_control_identity",
            lambda tid: _resolved(provider="claude_code", provider_version=None),
        )
        expected = {**EXPECTED, "provider": "claude_code"}
        result = _submit(
            text="see [Image #1]",
            attachments=["att-1"],
            token_map={"1": "att-1"},
            expected_identity=expected,
        )
        assert result.outcome == "refused"
        assert result.reason_code == "provider-unsupported"

    @pytest.mark.parametrize("managed", [False, True])
    def test_a_row_without_a_generation_is_refused_not_asserted(self, wire, managed):
        """cond-0413 on the operator lane, which the control lane cannot reach.

        This lane proves the composer is idle by reading the turn state of
        the deterministic managed window, and that window's name is derived
        from the generation.  Neither an unmanaged legacy row nor a managed
        row that resolves none can have it named, so both leave with a typed
        zero-byte refusal naming what was observed.  A bare ``assert`` here
        exits as an untyped 500 that tells an operator nothing about whether
        the message was typed.

        The unmanaged case is the live one: ``7b1a724b`` is an unmanaged
        ``claude_code`` row whose generation is NULL.
        """
        adapter = _FakeAdapter(record=_record(_op_id()))
        wire(
            _resolved(managed=managed, terminal_generation=None),
            adapter,
            {"deliverable": True},
        )
        expected = {**EXPECTED, "terminal_generation": None}

        result = _submit(operation_id=_op_id(), expected_identity=expected)

        assert result.outcome == "refused"
        assert result.reason_code == "lineage-unproven"
        assert "records no generation" in result.detail
        assert "nothing was typed" in result.detail
        # The zero-byte proof this lane records: the adapter owns the durable
        # claim and every keystroke, so an untouched adapter is the durable
        # statement that no byte was typed and no operation was opened.
        assert adapter.calls == []

    def test_the_admission_backstops_a_managed_row_that_reaches_it(self):
        """The choke point the readiness gate above sits in front of.

        The gate answers first, so this can only fire if a later change
        moves or narrows it -- which is exactly when an unfenced managed
        byte would otherwise be admitted.  ``FencedError`` is its base, so
        this lane's existing handler settles it without a new except arm.
        """
        resolved = _resolved(managed=True, terminal_generation=None)
        with pytest.raises(gf.GenerationUnselectable) as raised:
            with cis.provider_byte_admission(resolved, TERMINAL, None):
                pytest.fail("a managed row with no generation was admitted")
        assert isinstance(raised.value, gf.FencedError)
        assert cis._admission_refusal_reason(raised.value) == "lineage-unproven"


class TestAttachmentBindingAndSubstitution:
    def _bound(self, monkeypatch, records):
        """Fake the attachment store's read side and the hook's binding.

        Records default to ``ready`` so the service's pre-lease read-side
        checks pass; ``bind_for_submit`` is stubbed because the pre-write
        hook would otherwise hit the real manifest.
        """
        by_id = {record["attachment_id"]: {"state": "ready", **record} for record in records}
        monkeypatch.setattr(
            oms.image_attachments,
            "get_attachment",
            lambda terminal_id, attachment_id: dict(by_id[attachment_id]),
        )
        monkeypatch.setattr(oms.image_attachments, "bind_for_submit", lambda *a: records)
        monkeypatch.setattr(
            oms.image_attachments,
            "staged_absolute_path",
            lambda record: f"/staged/{record['attachment_id']}.png",
        )

    def test_binding_error_maps_to_the_typed_reason(self, wire, monkeypatch):
        def _raise(*a):
            raise AttachmentBindingError("attachment-unknown", "no such attachment")

        self._bound(monkeypatch, [{"attachment_id": "att-1", "format": "png"}])
        monkeypatch.setattr(oms.image_attachments, "bind_for_submit", _raise)
        adapter = _FakeAdapter(record=_record(OP))
        wire(_resolved(), adapter, {"deliverable": True})
        result = _submit(text="see [Image #1]", attachments=["att-1"], token_map={"1": "att-1"})
        assert result.outcome == "refused"
        assert result.reason_code == "attachment-unknown"
        # The hook refusal is journaled: zero bytes typed, fresh id retriable.
        assert result.record_state == "refused"

    def test_a_format_outside_the_advertised_set_is_refused(self, wire, monkeypatch):
        # A kimi terminal (png-only) holding a staged jpeg: upload gating
        # should have prevented it; the submit-time recheck is the backstop.
        self._bound(monkeypatch, [{"attachment_id": "att-1", "format": "jpeg"}])
        monkeypatch.setattr(cis, "resolve_control_identity", lambda tid: _resolved())
        result = _submit(text="see [Image #1]", attachments=["att-1"], token_map={"1": "att-1"})
        assert result.outcome == "refused"
        assert result.reason_code == "attachment-type-unsupported"

    def test_kimi_template_substitution_at_the_token_position(self, wire, monkeypatch):
        adapter = _FakeAdapter(record=_record(OP))
        wire(_resolved(), adapter, {"deliverable": True})
        self._bound(monkeypatch, [{"attachment_id": "att-1", "format": "png"}])
        monkeypatch.setattr(oms, "_terminal_agent_profile", lambda tid: None)
        result = _submit(
            text="before [Image #1] after", attachments=["att-1"], token_map={"1": "att-1"}
        )
        assert result.outcome == "accepted"
        sent = adapter.calls[0]["text"]
        assert sent == (
            "before Use the ReadMediaFile tool to read the image file at "
            "/staged/att-1.png and analyze it in the context of this message. after"
        )

    def test_claude_template_is_the_bare_translated_path(self, wire, monkeypatch):
        adapter = _FakeAdapter(record=_record(OP))
        resolved = _resolved(provider="claude_code", provider_version="2.1.220")
        wire(resolved, adapter, {"deliverable": True})
        self._bound(monkeypatch, [{"attachment_id": "att-1", "format": "png"}])
        monkeypatch.setattr(oms, "_terminal_agent_profile", lambda tid: None)
        expected = {**EXPECTED, "provider": "claude_code"}
        result = _submit(
            text="analyze [Image #1]",
            attachments=["att-1"],
            token_map={"1": "att-1"},
            expected_identity=expected,
        )
        assert result.outcome == "accepted"
        assert adapter.calls[0]["text"] == "analyze /staged/att-1.png"

    def test_container_path_maps_translate_longest_prefix(self, wire, monkeypatch):
        from cli_agent_orchestrator.models.agent_profile import (
            AgentProfile,
            ContainerConfig,
            ContainerPathMap,
        )

        adapter = _FakeAdapter(record=_record(OP))
        resolved = _resolved(provider="claude_code", provider_version="2.1.220")
        wire(resolved, adapter, {"deliverable": True})
        monkeypatch.setattr(
            oms.image_attachments,
            "get_attachment",
            lambda terminal_id, attachment_id: {
                "attachment_id": "att-1",
                "state": "ready",
                "format": "png",
            },
        )
        monkeypatch.setattr(
            oms.image_attachments,
            "bind_for_submit",
            lambda *a: [{"attachment_id": "att-1", "format": "png"}],
        )
        monkeypatch.setattr(
            oms.image_attachments,
            "staged_absolute_path",
            lambda record: "/host/state/attachments/t1/att-1.png",
        )
        profile = AgentProfile(
            name="containerized",
            description="test",
            container=ContainerConfig(
                path_maps=[
                    ContainerPathMap(host="/host", guest="/g"),
                    ContainerPathMap(host="/host/state", guest="/guest/state"),
                ]
            ),
        )
        monkeypatch.setattr(oms, "_terminal_agent_profile", lambda tid: profile)
        expected = {**EXPECTED, "provider": "claude_code"}
        result = _submit(
            text="see [Image #1]",
            attachments=["att-1"],
            token_map={"1": "att-1"},
            expected_identity=expected,
        )
        assert result.outcome == "accepted"
        # Longest prefix wins: /host/state, not /host.
        assert adapter.calls[0]["text"] == "see /guest/state/attachments/t1/att-1.png"

    def test_a_staged_path_with_no_matching_map_is_refused(self, wire, monkeypatch):
        from cli_agent_orchestrator.models.agent_profile import (
            AgentProfile,
            ContainerConfig,
            ContainerPathMap,
        )

        monkeypatch.setattr(
            oms.image_attachments,
            "get_attachment",
            lambda terminal_id, attachment_id: {
                "attachment_id": "att-1",
                "state": "ready",
                "format": "png",
            },
        )
        monkeypatch.setattr(
            oms.image_attachments,
            "bind_for_submit",
            lambda *a: [{"attachment_id": "att-1", "format": "png"}],
        )
        monkeypatch.setattr(
            oms.image_attachments,
            "staged_absolute_path",
            lambda record: "/elsewhere/att-1.png",
        )
        monkeypatch.setattr(
            oms,
            "_terminal_agent_profile",
            lambda tid: AgentProfile(
                name="containerized",
                description="test",
                container=ContainerConfig(
                    path_maps=[ContainerPathMap(host="/host", guest="/guest")]
                ),
            ),
        )
        resolved = _resolved(provider="claude_code", provider_version="2.1.220")
        wire(resolved, _FakeAdapter(), {"deliverable": True})
        expected = {**EXPECTED, "provider": "claude_code"}
        result = _submit(
            text="see [Image #1]",
            attachments=["att-1"],
            token_map={"1": "att-1"},
            expected_identity=expected,
        )
        assert result.outcome == "refused"
        assert result.reason_code == "attachment-not-ready"
        assert "maps to no guest path" in result.detail


class TestBindingSeam:
    """Lane C r1: the ready → submitted binding happens at the adapter's
    pre-write hook — after the journal claim and every gate — so a
    zero-byte refusal never strands a ready image as submitted, an
    unchanged ready image is retriable by a fresh operation id, and a
    post-hook ambiguity keeps the binding (no rollback races the write).
    """

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        root = tmp_path / "attachments"
        manifest = tmp_path / "attachments.json"
        monkeypatch.setattr(oms.image_attachments, "attachments_root", lambda: root)
        monkeypatch.setattr(oms.image_attachments, "manifest_path", lambda: manifest)
        return tmp_path

    @staticmethod
    def _png_bytes() -> bytes:
        def chunk(tag: bytes, data: bytes) -> bytes:
            return (
                struct.pack(">I", len(data))
                + tag
                + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
            )

        ihdr = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)
        raw = b"".join(b"\x00" + b"\x7f" * (2 * 3) for _ in range(2))
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b"")
        )

    def _stage(self):
        return oms.image_attachments.stage_upload(
            TERMINAL,
            display_filename="shot.png",
            content=self._png_bytes(),
            allowed_formats=frozenset({"png"}),
        )

    def _submit_image(self, operation_id, attachment_id):
        return _submit(
            operation_id=operation_id,
            text="see [Image #1]",
            attachments=[attachment_id],
            token_map={"1": attachment_id},
        )

    def test_a_post_bind_gate_refusal_leaves_the_image_ready_and_a_fresh_id_retries(
        self, wire, monkeypatch, store
    ):
        attachment = self._stage()
        aid = attachment["attachment_id"]
        monkeypatch.setattr(oms, "_terminal_agent_profile", lambda tid: None)

        # A busy turn refuses with zero bytes before the adapter call: the
        # hook never ran, so the image stays ready and unbound.
        adapter = _FakeAdapter(record=_record(OP))
        wire(_resolved(), adapter, {"deliverable": True}, turn_status=TerminalStatus.PROCESSING)
        result = self._submit_image(_op_id(), aid)
        assert result.outcome == "refused"
        assert result.reason_code == REASON_PANE_BUSY
        assert adapter.calls == []
        record = oms.image_attachments.get_attachment(TERMINAL, aid)
        assert record["state"] == "ready"
        assert record["bound_operation_id"] is None

        # A fresh operation id retries the unchanged ready image and binds it.
        retry_adapter = _FakeAdapter(record=_record(OP))
        wire(_resolved(), retry_adapter, {"deliverable": True}, turn_status=TerminalStatus.IDLE)
        retry_id = _op_id()
        retry = self._submit_image(retry_id, aid)
        assert retry.outcome == "accepted", retry.detail
        record = oms.image_attachments.get_attachment(TERMINAL, aid)
        assert record["state"] == "submitted"
        assert record["bound_operation_id"] == retry_id

    def test_a_substitution_refusal_leaves_the_image_ready(self, wire, monkeypatch, store):
        """Path substitution is a zero-byte gate ahead of the hook."""
        from cli_agent_orchestrator.models.agent_profile import (
            AgentProfile,
            ContainerConfig,
            ContainerPathMap,
        )

        attachment = self._stage()
        aid = attachment["attachment_id"]
        monkeypatch.setattr(
            oms,
            "_terminal_agent_profile",
            lambda tid: AgentProfile(
                name="containerized",
                description="test",
                container=ContainerConfig(
                    path_maps=[ContainerPathMap(host="/nowhere", guest="/guest")]
                ),
            ),
        )
        resolved = _resolved(provider="claude_code", provider_version="2.1.220")
        adapter = _FakeAdapter(record=_record(OP))
        wire(resolved, adapter, {"deliverable": True})
        expected = {**EXPECTED, "provider": "claude_code"}
        result = _submit(
            operation_id=_op_id(),
            text="see [Image #1]",
            attachments=[aid],
            token_map={"1": aid},
            expected_identity=expected,
        )
        assert result.outcome == "refused"
        assert result.reason_code == "attachment-not-ready"
        assert adapter.calls == []
        record = oms.image_attachments.get_attachment(TERMINAL, aid)
        assert record["state"] == "ready"
        assert record["bound_operation_id"] is None

    def test_a_hook_refusal_is_journaled_with_zero_bytes(self, wire, monkeypatch, store):
        """A bind that fails only at the hook (the pre-lease read side was
        raced) is journaled as a typed refusal with zero bytes typed."""
        monkeypatch.setattr(
            oms.image_attachments,
            "get_attachment",
            lambda terminal_id, attachment_id: {
                "attachment_id": attachment_id,
                "state": "ready",
                "format": "png",
            },
        )
        monkeypatch.setattr(
            oms.image_attachments,
            "staged_absolute_path",
            lambda record: f"{store}/attachments/{TERMINAL}/att-ghost.png",
        )
        monkeypatch.setattr(oms, "_terminal_agent_profile", lambda tid: None)
        adapter = _FakeAdapter(record=_record(OP))
        wire(_resolved(), adapter, {"deliverable": True})
        # The hook's bind hits the real (empty) store: attachment-unknown.
        result = self._submit_image(_op_id(), "att-ghost")
        assert result.outcome == "refused"
        assert result.reason_code == "attachment-unknown"
        assert result.record_state == "refused"
        assert adapter.calls[0]["pre_write"] is not None

    def test_an_ambiguity_after_the_hook_keeps_the_binding(self, wire, monkeypatch, store):
        """After a successful hook, a mid-submit raise freezes the operation
        and the image stays submitted — no rollback may race the write."""
        attachment = self._stage()
        aid = attachment["attachment_id"]
        monkeypatch.setattr(oms, "_terminal_agent_profile", lambda tid: None)
        adapter = _FakeAdapter(
            record=_record(OP), raises=RuntimeError("transport died"), raises_after_hook=True
        )
        wire(_resolved(), adapter, {"deliverable": True})
        operation_id = _op_id()
        result = self._submit_image(operation_id, aid)
        assert result.outcome == "ambiguous"
        assert result.reason_code == "response-lost"
        assert adapter.ambiguous_marks
        record = oms.image_attachments.get_attachment(TERMINAL, aid)
        assert record["state"] == "submitted"
        assert record["bound_operation_id"] == operation_id

    def test_the_hook_binds_only_after_every_gate(self, wire, monkeypatch, store):
        """Lease contention never reaches the hook: no binding, no write."""
        attachment = self._stage()
        aid = attachment["attachment_id"]
        monkeypatch.setattr(oms, "_terminal_agent_profile", lambda tid: None)
        adapter = _FakeAdapter(record=_record(OP))
        wire(_resolved(), adapter, {"deliverable": True})

        @contextmanager
        def _busy_lease(*a, **k):
            raise cis.PaneBusyError("held by another writer")

        monkeypatch.setattr(oms, "pane_input_lease", _busy_lease)
        result = self._submit_image(_op_id(), aid)
        assert result.outcome == "refused"
        assert result.reason_code == REASON_PANE_BUSY
        assert adapter.calls == []
        record = oms.image_attachments.get_attachment(TERMINAL, aid)
        assert record["state"] == "ready"
        assert record["bound_operation_id"] is None


class TestUnderLeaseGuards:
    def test_happy_path_is_accepted_and_marks_the_kimi_dispatch(self, wire):
        adapter = _FakeAdapter(record=_record(OP))
        wire(_resolved(), adapter, {"deliverable": True})
        result = _submit()
        assert result.outcome == "accepted"
        assert result.reason_code == "delivered"
        assert result.record_state == "posted"
        assert adapter.calls[0]["payload_sha256"] == oms._request_digest(
            TERMINAL, "hello operator", [], {}
        )
        assert adapter.calls[0]["observation"]["active_turn_id"] is None
        key = cis._native_kimi_dispatch_key(
            _resolved(), SimpleNamespace(pane_id=PANE, pane_pid=4321)
        )
        assert cis._native_kimi_dispatch_is_guarded(key)

    def test_lease_contention_is_pane_busy_with_zero_writes(self, wire, monkeypatch):
        adapter = _FakeAdapter(record=_record(OP))
        wire(_resolved(), adapter, {"deliverable": True})

        @contextmanager
        def _busy_lease(*a, **k):
            raise cis.PaneBusyError("held by another writer")

        monkeypatch.setattr(oms, "pane_input_lease", _busy_lease)
        result = _submit()
        assert result.outcome == "refused"
        assert result.reason_code == REASON_PANE_BUSY
        assert adapter.calls == []

    def test_copy_mode_refusal_passes_through(self, wire, monkeypatch):
        adapter = _FakeAdapter(record=_record(OP))
        wire(_resolved(), adapter, {"deliverable": True})
        monkeypatch.setattr(
            cis,
            "_copy_mode_guard_refusal",
            lambda *a, **k: ("copy-mode-active", "the pane is in copy mode"),
        )
        result = _submit()
        assert result.outcome == "refused"
        assert result.reason_code == "copy-mode-active"
        assert adapter.calls == []

    def test_a_busy_turn_refuses_pane_busy(self, wire):
        adapter = _FakeAdapter(record=_record(OP))
        wire(_resolved(), adapter, {"deliverable": True}, turn_status=TerminalStatus.PROCESSING)
        result = _submit()
        assert result.outcome == "refused"
        assert result.reason_code == "pane-busy"
        assert adapter.calls == []

    def test_an_unobservable_turn_state_refuses_pane_busy(self, wire):
        adapter = _FakeAdapter(record=_record(OP))
        wire(_resolved(), adapter, {"deliverable": True}, turn_status=RuntimeError("no screen"))
        result = _submit()
        assert result.outcome == "refused"
        assert result.reason_code == "pane-busy"

    def test_multiline_on_an_unproven_build_is_multiline_unproven(self, wire):
        wire(
            _resolved(),
            None,
            ("provider-unsupported", "no composer newline proven for this build"),
        )
        result = _submit(text="line one\nline two")
        assert result.outcome == "refused"
        assert result.reason_code == "multiline-unproven"

    def test_single_line_on_an_unproven_build_is_provider_unsupported(self, wire):
        wire(
            _resolved(),
            None,
            ("provider-unsupported", "no composer behaviour is proven"),
        )
        result = _submit(text="single line")
        assert result.outcome == "refused"
        assert result.reason_code == "provider-unsupported"


class TestReplayAndResponseLoss:
    def test_an_unreadable_store_on_submit_fails_closed_with_zero_io(self, wire, monkeypatch):
        """r16 Sol P1.1: a store that cannot be read is not a store without
        the record.  Absence is unprovable, so the submit answers the
        honest unknown — never a fresh send — before any identity,
        attachment, lease, or adapter I/O."""
        monkeypatch.setattr(
            oms,
            "_find_operation",
            lambda operation_id: (None, ["kimi_native_control: database is locked"]),
        )
        monkeypatch.setattr(
            cis,
            "resolve_control_identity",
            lambda tid: pytest.fail("fail-closed must not touch identity resolution"),
        )
        monkeypatch.setattr(
            oms.image_attachments,
            "get_attachment",
            lambda *a: pytest.fail("fail-closed must not touch attachment I/O"),
        )
        adapter = _FakeAdapter(record=_record(OP))
        result = _submit(text="see [Image #1]", attachments=["att-1"], token_map={"1": "att-1"})
        assert result.outcome == "ambiguous"
        assert result.reason_code == "response-lost"
        assert "could not be read" in result.detail
        assert "never resend" in result.detail
        assert adapter.calls == []

    def test_an_identical_replay_answers_from_the_store_with_zero_io(self, wire, monkeypatch):
        digest = oms._request_digest(TERMINAL, "hello operator", [], {})
        stored = _record(OP, digest=digest)
        monkeypatch.setattr(oms, "_find_operation", lambda operation_id: (stored, []))
        sentinel = object()
        monkeypatch.setattr(
            cis,
            "resolve_control_identity",
            lambda tid: pytest.fail("replay must not touch identity resolution"),
        )
        adapter = _FakeAdapter(record=stored)
        result = _submit()
        assert result.outcome == "accepted"
        assert result.replayed is True
        assert adapter.calls == []

    def test_a_divergent_replay_is_request_rebound(self, wire, monkeypatch):
        stored = _record(OP, digest="0" * 64)
        monkeypatch.setattr(oms, "_find_operation", lambda operation_id: (stored, []))
        result = _submit()
        assert result.outcome == "refused"
        assert result.reason_code == "request-rebound"

    def test_an_adapter_conflict_is_request_rebound(self, wire):
        adapter = _FakeAdapter(raises=_FakeAdapter.NativeControlConflict("id reuse"))
        wire(_resolved(), adapter, {"deliverable": True})
        result = _submit()
        assert result.outcome == "refused"
        assert result.reason_code == "request-rebound"

    def test_a_mid_submit_raise_freezes_and_answers_ambiguous(self, wire):
        adapter = _FakeAdapter(raises=RuntimeError("store gone"))
        wire(_resolved(), adapter, {"deliverable": True})
        result = _submit()
        assert result.outcome == "ambiguous"
        assert result.reason_code == "response-lost"
        # Best-effort freeze: the record is marked ambiguous, never retried.
        assert adapter.ambiguous_marks and adapter.ambiguous_marks[0][0] == OP

    def test_an_ambiguous_record_is_never_resent_on_resubmit(self, wire, monkeypatch):
        """The zero-retry invariant: after a lost response, the same id
        replays the frozen ambiguous answer; the adapter never re-executes."""
        digest = oms._request_digest(TERMINAL, "hello operator", [], {})
        stored = _record(OP, state="ambiguous", digest=digest, ambiguity_reason="enter raised")
        monkeypatch.setattr(oms, "_find_operation", lambda operation_id: (stored, []))
        result = _submit()
        assert result.outcome == "ambiguous"
        assert result.reason_code == "write-incomplete"
        assert result.replayed is True

    def test_a_stranded_intended_record_is_the_unknown_it_is(self, wire, monkeypatch):
        digest = oms._request_digest(TERMINAL, "hello operator", [], {})
        stored = _record(OP, state="intended", digest=digest)
        monkeypatch.setattr(oms, "_find_operation", lambda operation_id: (stored, []))
        result = _submit()
        assert result.outcome == "ambiguous"
        assert result.reason_code == "response-lost"

    def test_a_refused_record_maps_the_adapter_reason(self, wire, monkeypatch):
        digest = oms._request_digest(TERMINAL, "hello operator", [], {})
        stored = _record(
            OP,
            state="refused",
            digest=digest,
            refusal_reason="unresolved_ambiguity",
        )
        monkeypatch.setattr(oms, "_find_operation", lambda operation_id: (stored, []))
        result = _submit()
        assert result.outcome == "refused"
        assert result.reason_code == "unresolved-ambiguity"


class TestReconcile:
    def test_a_clean_miss_proves_nothing_was_typed(self, monkeypatch):
        monkeypatch.setattr(oms, "_find_operation", lambda operation_id: (None, []))
        result = oms.reconcile_operator_message(OP)
        assert result.outcome == "refused"
        assert result.reason_code == "owner-lost-before-write"

    def test_an_unreadable_store_is_the_unknown_it_is(self, monkeypatch):
        monkeypatch.setattr(oms, "_find_operation", lambda operation_id: (None, ["db locked"]))
        result = oms.reconcile_operator_message(OP)
        assert result.outcome == "ambiguous"
        assert result.reason_code == "response-lost"

    def test_a_found_record_maps_its_state(self, monkeypatch):
        stored = _record(OP, state="posted")
        monkeypatch.setattr(oms, "_find_operation", lambda operation_id: (stored, []))
        result = oms.reconcile_operator_message(OP)
        assert result.outcome == "accepted"
        assert result.record_state == "posted"

    def test_reconcile_requires_a_uuid(self):
        with pytest.raises(oms.OperatorMessageRequestInvalid):
            oms.reconcile_operator_message("nope")


class TestUploadGating:
    def test_a_provider_without_image_support_is_a_typed_refusal(self, monkeypatch):
        monkeypatch.setattr(
            cis,
            "resolve_control_identity",
            lambda tid: _resolved(provider="codex", provider_version="0.146.0"),
        )
        with pytest.raises(oms.AttachmentRefusal) as excinfo:
            oms.upload_attachment(TERMINAL, display_filename="a.png", content=b"x")
        assert excinfo.value.status_code == 422
        assert excinfo.value.reason_code == "provider-unsupported"

    def test_an_unknown_terminal_is_a_lookup_error(self, monkeypatch):
        monkeypatch.setattr(cis, "resolve_control_identity", lambda tid: None)
        with pytest.raises(LookupError):
            oms.upload_attachment(TERMINAL, display_filename="a.png", content=b"x")

    def test_a_validation_failure_carries_the_failed_record(self, monkeypatch):
        from cli_agent_orchestrator.services import image_attachments

        monkeypatch.setattr(cis, "resolve_control_identity", lambda tid: _resolved())

        def _raise(*a, **k):
            error = image_attachments.AttachmentValidationError(
                "attachment-too-large", "image is 6 MB"
            )
            error.record = {"attachment_id": "att-x", "state": "failed"}
            raise error

        monkeypatch.setattr(image_attachments, "stage_upload", _raise)
        with pytest.raises(oms.AttachmentRefusal) as excinfo:
            oms.upload_attachment(TERMINAL, display_filename="a.png", content=b"x")
        assert excinfo.value.reason_code == "attachment-too-large"
        assert excinfo.value.record["attachment_id"] == "att-x"
