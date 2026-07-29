"""Tests for the shared control-input wire contract."""

import hashlib

import pytest

from cli_agent_orchestrator.services import canonical_json
from cli_agent_orchestrator.services import control_input_contract as contract


class TestProtocolIdentity:
    def test_protocol_and_schema_are_pinned(self):
        """Both sides negotiate on these exact strings; drift is a break."""
        assert contract.CONTROL_INPUT_PROTOCOL == "cao-control-input-v1"
        assert contract.CONTROL_INPUT_SCHEMA_VERSION == 1

    def test_outcomes_are_a_closed_set(self):
        assert contract.CONTROL_INPUT_OUTCOMES == {
            "accepted",
            "refused",
            "ambiguous",
            "unsupported",
        }

    def test_only_a_refusal_may_be_reattempted(self):
        """A refusal is decided before any write; nothing else proves that."""
        assert contract.REATTEMPTABLE_OUTCOMES == {contract.REFUSED}
        assert contract.ACCEPTED not in contract.REATTEMPTABLE_OUTCOMES
        assert contract.AMBIGUOUS not in contract.REATTEMPTABLE_OUTCOMES
        assert contract.UNSUPPORTED not in contract.REATTEMPTABLE_OUTCOMES

    def test_reason_codes_cover_the_named_refusals(self):
        for reason in (
            contract.REASON_UNKNOWN_TERMINAL,
            contract.REASON_IDENTITY_MISMATCH,
            contract.REASON_STALE_GENERATION,
            contract.REASON_PANE_BUSY,
            contract.REASON_ILLEGAL_CONTROL_BYTES,
            contract.REASON_MULTILINE_REJECTED,
            contract.REASON_PROVIDER_UNSUPPORTED,
            contract.REASON_CONTROL_ROUTE_ABSENT,
        ):
            assert reason in contract.CONTROL_INPUT_REASON_CODES


class TestPaneIdValidation:
    """One definition of a legal control target, shared by every layer.

    A pane the arbiter would lock but the writer would reject — or the
    reverse — is a hole, so the writer, the arbiter, and the journal all
    ask this function.
    """

    @pytest.mark.parametrize("pane_id", ["%0", "%1", "%42", "%1234567890"])
    def test_accepts_tmux_pane_ids(self, pane_id):
        assert contract.is_valid_pane_id(pane_id) is True

    @pytest.mark.parametrize(
        "pane_id",
        [
            "",
            "%",
            "0",
            "42",
            "%4a",
            "%-1",
            "@1",
            "$1",
            "%1:2",  # ':' is a tmux target delimiter
            "%1.0",  # '.' is a tmux target delimiter
            "-t",  # would be read as an option, not a target
            "%1 %2",
            "%12345678901",  # longer than any real pane counter
            "%1\n",
            None,
            42,
            b"%1",
        ],
    )
    def test_rejects_anything_a_target_argument_could_misread(self, pane_id):
        assert contract.is_valid_pane_id(pane_id) is False


class TestRequestDigest:
    """The digest is the request id's binding to one exact control."""

    @staticmethod
    def _digest(**overrides):
        fields = {
            "control_id": "req-1",
            "text": "/model opus",
            "enter": True,
            "expected_identity": {"terminal_id": "term-1", "terminal_generation": "gen-1"},
        }
        fields.update(overrides)
        return contract.control_input_request_digest(**fields)

    def test_is_deterministic(self):
        assert self._digest() == self._digest()
        assert len(self._digest()) == 64

    @pytest.mark.parametrize(
        "override",
        [
            {"control_id": "req-2"},
            {"text": "/model haiku"},
            {"enter": False},
            {"expected_identity": {"terminal_id": "term-2", "terminal_generation": "gen-1"}},
            {"expected_identity": {"terminal_id": "term-1", "terminal_generation": "gen-2"}},
            {"expected_identity": {"terminal_id": "term-1"}},
            {"expected_identity": None},
        ],
    )
    def test_every_bound_field_changes_the_digest(self, override):
        """Same text under a different expectation is a different request."""
        assert self._digest(**override) != self._digest()

    def test_an_absent_expectation_is_not_a_dropped_one(self):
        """Explicit nulls: 'expects nothing' must not collide with 'lost in transit'."""
        assert self._digest(expected_identity={}) == self._digest(expected_identity=None)
        assert self._digest(expected_identity={}) != self._digest()

    def test_the_field_order_is_fixed_not_lexicographic(self):
        """Ordering is contract, not a property of whichever dict came first."""
        assert contract.REQUEST_DIGEST_FIELD_ORDER == (
            "domain",
            "schema_version",
            "control_id",
            "text",
            "enter",
            "expected_identity",
        )
        assert contract.REQUEST_DIGEST_FIELD_ORDER != tuple(
            sorted(contract.REQUEST_DIGEST_FIELD_ORDER)
        )

    def test_the_digest_domain_is_separate_from_the_protocol_id(self):
        """Same bytes under another purpose must not collide with a request."""
        assert contract.CONTROL_INPUT_DIGEST_DOMAIN != contract.CONTROL_INPUT_PROTOCOL

    def test_a_bumped_schema_version_invalidates_old_digests(self):
        assert self._digest(schema_version=2) != self._digest()

    @pytest.mark.parametrize("enter", [1, 0, None, "true"])
    def test_a_non_boolean_enter_is_refused(self, enter):
        """1 and True encode differently; a silent coercion forks the digest."""
        with pytest.raises(ValueError):
            self._digest(enter=enter)

    def test_owner_loss_is_two_named_reasons(self):
        """One reason per outcome: the reason is readable without the state."""
        assert contract.REASON_OWNER_LOST_BEFORE_WRITE in contract.CONTROL_INPUT_REASON_CODES
        assert contract.REASON_OWNER_LOST_MID_WRITE in contract.CONTROL_INPUT_REASON_CODES


class TestV2ChordDigest:
    """Schema v2 adds exactly one field, ``chord``, between ``enter`` and
    ``expected_identity`` in the digest preimage, with its own domain.

    The preimage field order and domain string are the cross-repo contract:
    both sides reproduce the golden vector byte for byte, so a one-sided
    edit to either implementation fails this test rather than diverging in
    production.  v1 stays byte-identical (its domain, version, and field
    order are untouched) -- a v2-capable server must not change what a v1
    request digests to.
    """

    # The exact golden vector from the activation spec §3.  control_id,
    # text, identity, and provider_process_id typing are all part of it.
    VECTOR = {
        "control_id": "ex-1",
        "text": "[conduct-steer ex-1] URGENT amendment for task demo: apply the reviewed fix",
        "enter": False,
        "chord": "C-s",
        "expected_identity": {
            "terminal_id": "term-1",
            "terminal_generation": "gen-3",
            "provider_process_id": 4242,
            "provider": "kimi_cli",
            "session_name": "cao-demo",
        },
    }
    PREIMAGE = (
        '{"domain":"cao-control-input-request-v2","schema_version":2,'
        '"control_id":"ex-1",'
        '"text":"[conduct-steer ex-1] URGENT amendment for task demo: apply the reviewed fix",'
        '"enter":false,"chord":"C-s","expected_identity":{"terminal_id":"term-1",'
        '"terminal_incarnation":null,"terminal_generation":"gen-3","pane_birth_id":null,'
        '"provider_process_id":4242,"provider":"kimi_cli","native_session_id":null,'
        '"execution_mode":null,"session_name":"cao-demo"}}\n'
    )
    DIGEST = "6b1086b25fbe2b0eeb8b8d884440e0a2ab5f07b714d59ceea25fbe126948806c"

    def test_v2_domain_is_separate_from_v1(self):
        assert contract.CONTROL_INPUT_DIGEST_DOMAIN_V2 == "cao-control-input-request-v2"
        assert contract.CONTROL_INPUT_DIGEST_DOMAIN_V2 != contract.CONTROL_INPUT_DIGEST_DOMAIN
        assert contract.CONTROL_INPUT_REQUEST_SCHEMA_VERSION_V2 == 2

    def test_v2_field_order_inserts_chord_before_identity(self):
        order = contract.REQUEST_DIGEST_FIELD_ORDER_V2
        assert order == (
            "domain",
            "schema_version",
            "control_id",
            "text",
            "enter",
            "chord",
            "expected_identity",
        )
        # The insertion point is contract, not a property of any dict.
        assert order != tuple(sorted(order))

    def test_matches_the_recorded_digest(self):
        assert (
            contract.control_input_request_digest_v2(
                control_id=self.VECTOR["control_id"],
                text=self.VECTOR["text"],
                enter=self.VECTOR["enter"],
                chord=self.VECTOR["chord"],
                expected_identity=self.VECTOR["expected_identity"],
            )
            == self.DIGEST
        )

    def test_matches_the_recorded_preimage_byte_for_byte(self):
        assert (
            hashlib.sha256(self.PREIMAGE.encode("utf-8")).hexdigest() == self.DIGEST
        ), "the recorded v2 preimage does not hash to the recorded digest"
        encoded = canonical_json.encode_canonical(
            {
                "domain": contract.CONTROL_INPUT_DIGEST_DOMAIN_V2,
                "schema_version": contract.CONTROL_INPUT_REQUEST_SCHEMA_VERSION_V2,
                "control_id": self.VECTOR["control_id"],
                "text": self.VECTOR["text"],
                "enter": self.VECTOR["enter"],
                "chord": self.VECTOR["chord"],
                "expected_identity": contract.normalize_expected_identity(
                    self.VECTOR["expected_identity"]
                ),
            }
        )
        assert encoded.decode("utf-8") == self.PREIMAGE

    def test_chord_is_bound_into_the_digest(self):
        base = dict(self.VECTOR)
        different = contract.control_input_request_digest_v2(
            control_id=base["control_id"],
            text=base["text"],
            enter=base["enter"],
            chord="C-c",
            expected_identity=base["expected_identity"],
        )
        assert different != self.DIGEST
        # A null chord is a different request from a named one.
        none_chord = contract.control_input_request_digest_v2(
            control_id=base["control_id"],
            text=base["text"],
            enter=base["enter"],
            chord=None,
            expected_identity=base["expected_identity"],
        )
        assert none_chord != self.DIGEST

    def test_v1_digest_is_unchanged_when_chord_exists(self):
        """v1 must keep digesting exactly as before v2 existed."""
        v1 = contract.control_input_request_digest(
            control_id="ex-1",
            text="[conduct-steer ex-1] URGENT amendment for task demo: apply the reviewed fix",
            enter=False,
            expected_identity=self.VECTOR["expected_identity"],
        )
        assert (
            v1
            == hashlib.sha256(
                (
                    '{"domain":"cao-control-input-request-v1","schema_version":1,'
                    '"control_id":"ex-1",'
                    '"text":"[conduct-steer ex-1] URGENT amendment for task demo: apply the '
                    'reviewed fix","enter":false,"expected_identity":{"terminal_id":"term-1",'
                    '"terminal_incarnation":null,"terminal_generation":"gen-3",'
                    '"pane_birth_id":null,"provider_process_id":4242,"provider":"kimi_cli",'
                    '"native_session_id":null,"execution_mode":null,"session_name":"cao-demo"}}\n'
                ).encode("utf-8")
            ).hexdigest()
        )

    @pytest.mark.parametrize("enter", [1, 0, None, "false"])
    def test_a_non_boolean_enter_is_refused(self, enter):
        with pytest.raises(ValueError):
            contract.control_input_request_digest_v2(
                control_id="ex-1", text="x", enter=enter, chord="C-s", expected_identity=None
            )

    @pytest.mark.parametrize("chord", ["", 1, True, []])
    def test_a_non_optional_chord_is_refused(self, chord):
        with pytest.raises(ValueError):
            contract.control_input_request_digest_v2(
                control_id="ex-1", text="x", enter=False, chord=chord, expected_identity=None
            )


class TestV3SequenceDigest:
    """Schema v3 carries an ordered ``events`` array under its own domain.

    A v3 request is a different request from every v1/v2 one, so it gets its
    own domain and field order; v1/v2 stay byte-identical.  Membership
    checks (key names, chord allowlists) are service-layer facts: the digest
    must be computable for any syntactically valid sequence, including one
    the server will then refuse, so the two sides never disagree about
    which requests exist.
    """

    VECTOR = {
        "control_id": "seq-1",
        "events": [
            {"type": "text", "text": "ping, plus+ back\\slash"},
            {"type": "key", "key": "Enter"},
            {"type": "chord", "chord": "C-s"},
            {"type": "key", "key": "Escape"},
            {"type": "key", "key": "C-c"},
            {"type": "key", "key": "Backspace"},
        ],
        "expected_identity": {
            "terminal_id": "term-1",
            "terminal_generation": "gen-3",
            "provider_process_id": 4242,
            "provider": "kimi_cli",
            "session_name": "cao-demo",
        },
    }
    PREIMAGE = (
        '{"domain":"cao-control-input-request-v3","schema_version":3,'
        '"control_id":"seq-1",'
        '"events":[{"type":"text","text":"ping, plus+ back\\\\slash"},'
        '{"type":"key","key":"Enter"},{"type":"chord","chord":"C-s"},'
        '{"type":"key","key":"Escape"},{"type":"key","key":"C-c"},'
        '{"type":"key","key":"Backspace"}],'
        '"expected_identity":{"terminal_id":"term-1",'
        '"terminal_incarnation":null,"terminal_generation":"gen-3","pane_birth_id":null,'
        '"provider_process_id":4242,"provider":"kimi_cli","native_session_id":null,'
        '"execution_mode":null,"session_name":"cao-demo"}}\n'
    )
    DIGEST = "a78594e8f25ed430de24e2e1fed6672f794c18632b0bb485de748cf76c656231"

    def test_v3_domain_is_separate_from_v1_and_v2(self):
        assert contract.CONTROL_INPUT_DIGEST_DOMAIN_V3 == "cao-control-input-request-v3"
        assert contract.CONTROL_INPUT_DIGEST_DOMAIN_V3 != contract.CONTROL_INPUT_DIGEST_DOMAIN
        assert contract.CONTROL_INPUT_DIGEST_DOMAIN_V3 != contract.CONTROL_INPUT_DIGEST_DOMAIN_V2
        assert contract.CONTROL_INPUT_REQUEST_SCHEMA_VERSION_V3 == 3

    def test_v3_field_order_carries_events_before_identity(self):
        order = contract.REQUEST_DIGEST_FIELD_ORDER_V3
        assert order == (
            "domain",
            "schema_version",
            "control_id",
            "events",
            "expected_identity",
        )
        assert order != tuple(sorted(order))

    def test_matches_the_recorded_digest(self):
        assert (
            contract.control_input_request_digest_v3(
                control_id=self.VECTOR["control_id"],
                events=self.VECTOR["events"],
                expected_identity=self.VECTOR["expected_identity"],
            )
            == self.DIGEST
        )

    def test_matches_the_recorded_preimage_byte_for_byte(self):
        assert (
            hashlib.sha256(self.PREIMAGE.encode("utf-8")).hexdigest() == self.DIGEST
        ), "the recorded v3 preimage does not hash to the recorded digest"
        encoded = canonical_json.encode_canonical(
            {
                "domain": contract.CONTROL_INPUT_DIGEST_DOMAIN_V3,
                "schema_version": contract.CONTROL_INPUT_REQUEST_SCHEMA_VERSION_V3,
                "control_id": self.VECTOR["control_id"],
                "events": contract.normalize_sequence_events(self.VECTOR["events"]),
                "expected_identity": contract.normalize_expected_identity(
                    self.VECTOR["expected_identity"]
                ),
            }
        )
        assert encoded.decode("utf-8") == self.PREIMAGE

    def test_event_order_is_bound_into_the_digest(self):
        reordered = contract.control_input_request_digest_v3(
            control_id="seq-1",
            events=[{"type": "key", "key": "Enter"}, {"type": "key", "key": "Escape"}],
            expected_identity=None,
        )
        original = contract.control_input_request_digest_v3(
            control_id="seq-1",
            events=[{"type": "key", "key": "Escape"}, {"type": "key", "key": "Enter"}],
            expected_identity=None,
        )
        assert reordered != original

    @pytest.mark.parametrize(
        "events",
        [
            [{"type": "text", "text": "changed"}],
            [{"type": "text", "text": "ping, plus+ back\\slash"}, {"type": "key", "key": "Enter"}],
            [{"type": "key", "key": "Escape"}],
            [{"type": "chord", "chord": "C-c"}],
        ],
    )
    def test_every_event_field_changes_the_digest(self, events):
        assert (
            contract.control_input_request_digest_v3(
                control_id="seq-1",
                events=events,
                expected_identity=self.VECTOR["expected_identity"],
            )
            != self.DIGEST
        )

    def test_v1_and_v2_digests_are_unchanged_by_v3(self):
        """The v3 addition must not move what a v1 or v2 request digests to."""
        v1 = contract.control_input_request_digest(
            control_id="req-1", text="/model opus", enter=True, expected_identity=None
        )
        v2 = contract.control_input_request_digest_v2(
            control_id="req-1",
            text="/model opus",
            enter=False,
            chord="C-s",
            expected_identity=None,
        )
        assert (
            v1
            == hashlib.sha256(
                b'{"domain":"cao-control-input-request-v1","schema_version":1,'
                b'"control_id":"req-1","text":"/model opus","enter":true,'
                b'"expected_identity":{"terminal_id":null,"terminal_incarnation":null,'
                b'"terminal_generation":null,"pane_birth_id":null,"provider_process_id":null,'
                b'"provider":null,"native_session_id":null,"execution_mode":null,'
                b'"session_name":null}}\n'
            ).hexdigest()
        )
        assert (
            v2
            == hashlib.sha256(
                b'{"domain":"cao-control-input-request-v2","schema_version":2,'
                b'"control_id":"req-1","text":"/model opus","enter":false,"chord":"C-s",'
                b'"expected_identity":{"terminal_id":null,"terminal_incarnation":null,'
                b'"terminal_generation":null,"pane_birth_id":null,"provider_process_id":null,'
                b'"provider":null,"native_session_id":null,"execution_mode":null,'
                b'"session_name":null}}\n'
            ).hexdigest()
        )

    def test_caps_are_pinned(self):
        assert contract.MAX_SEQUENCE_EVENTS == 32
        assert contract.MAX_SEQUENCE_TEXT_BYTES == 512
        assert contract.MAX_SEQUENCE_TEXT_BYTES == 512  # aggregate across text events

    def test_the_normalized_key_set_is_the_pinned_set(self):
        """The §3.2 set: the deployed five plus the eleven navigation/editing
        keys, extended in place under schema v3 (design D1)."""
        assert contract.SEQUENCE_KEY_NAMES == {
            "Escape",
            "C-c",
            "C-s",
            "Enter",
            "Backspace",
            "Up",
            "Down",
            "Left",
            "Right",
            "Home",
            "End",
            "PageUp",
            "PageDown",
            "Delete",
            "Insert",
            "Tab",
        }

    def test_new_reason_codes_are_refusals_decided_before_any_write(self):
        assert contract.REASON_UNSUPPORTED_KEY == "unsupported-key"
        assert contract.REASON_UNREPRESENTABLE_EVENT == "unrepresentable-event"
        assert contract.outcome_for_reason(contract.REASON_UNSUPPORTED_KEY) == contract.REFUSED
        assert (
            contract.outcome_for_reason(contract.REASON_UNREPRESENTABLE_EVENT) == contract.REFUSED
        )
        assert contract.REASON_UNSUPPORTED_KEY in contract.CONTROL_INPUT_REASON_CODES
        assert contract.REASON_UNREPRESENTABLE_EVENT in contract.CONTROL_INPUT_REASON_CODES

    def test_command_class_reason_codes_are_refusals_decided_before_any_write(self):
        """§4.1: both new reasons are decided before any byte, so both carry
        the zero-bytes proof and bind to REFUSED (the import-time assert in
        the contract covers the table; this pins the intent)."""
        assert contract.REASON_MALFORMED_COMMAND_DECLARATION == "malformed-command-declaration"
        assert contract.REASON_COMPOSER_NONEMPTY == "composer-nonempty"
        assert (
            contract.outcome_for_reason(contract.REASON_MALFORMED_COMMAND_DECLARATION)
            == contract.REFUSED
        )
        assert contract.outcome_for_reason(contract.REASON_COMPOSER_NONEMPTY) == contract.REFUSED
        assert contract.REASON_MALFORMED_COMMAND_DECLARATION in contract.CONTROL_INPUT_REASON_CODES
        assert contract.REASON_COMPOSER_NONEMPTY in contract.CONTROL_INPUT_REASON_CODES
        assert contract.is_reattemptable(
            contract.outcome_for_reason(contract.REASON_COMPOSER_NONEMPTY)
        )

    def test_thirty_two_events_is_the_cap(self):
        events = [{"type": "key", "key": "Escape"}] * 32
        assert len(contract.normalize_sequence_events(events)) == 32
        with pytest.raises(ValueError):
            contract.normalize_sequence_events(events + [{"type": "key", "key": "Escape"}])

    def test_aggregate_text_bytes_are_capped(self):
        # 256 + 256 exactly fits; one more byte does not.
        events = [
            {"type": "text", "text": "a" * 256},
            {"type": "text", "text": "b" * 256},
        ]
        assert len(contract.normalize_sequence_events(events)) == 2
        with pytest.raises(ValueError):
            contract.normalize_sequence_events([events[0], {"type": "text", "text": "b" * 257}])

    def test_multibyte_text_is_measured_in_utf8_bytes(self):
        with pytest.raises(ValueError):
            contract.normalize_sequence_events([{"type": "text", "text": "é" * 300}])

    @pytest.mark.parametrize(
        "events",
        [
            [],
            "not-a-list",
            [{"type": "text"}],
            [{"type": "text", "text": ""}],
            [{"type": "text", "text": "a" * 513}],
            [{"type": "text", "text": 42}],
            [{"type": "key"}],
            [{"type": "key", "key": ""}],
            [{"type": "key", "key": 42}],
            [{"type": "chord"}],
            [{"type": "chord", "chord": ""}],
            [{"type": "text", "text": "ok", "extra": "field"}],
            [{"type": "macro", "name": "x"}],  # unknown type with extra fields
            [{"key": "Enter"}],  # missing type
            [{"type": ""}],
            [{"type": 42}],
            ["Escape"],
        ],
    )
    def test_malformed_sequences_are_refused(self, events):
        with pytest.raises(ValueError):
            contract.normalize_sequence_events(events)

    def test_membership_is_not_decided_by_the_digest(self):
        """An unsupported key or unknown bare type still digests: the server
        must be able to name the request it is about to refuse."""
        digest = contract.control_input_request_digest_v3(
            control_id="seq-2",
            events=[{"type": "key", "key": "M-x"}, {"type": "macro"}],
            expected_identity=None,
        )
        assert len(digest) == 64

    def test_normalization_pins_the_wire_shape(self):
        normalized = contract.normalize_sequence_events(self.VECTOR["events"])
        assert normalized[0] == {"type": "text", "text": "ping, plus+ back\\slash"}
        assert list(normalized[0].keys()) == ["type", "text"]
        assert list(normalized[1].keys()) == ["type", "key"]
        assert list(normalized[2].keys()) == ["type", "chord"]
        # A bare unknown type normalizes to its name only.
        assert contract.normalize_sequence_events([{"type": "macro"}]) == [{"type": "macro"}]


class TestV4CommandClassDeclaration:
    """Schema v4: v3 plus the optional ``payload_class`` declaration carrier (§4.1, r7).

    A declared command is a different request from the same events
    undeclared, so the declaration participates in the digest under its
    own domain — the same reason ``chord`` participates in v2.  Command
    detection is never derived from payload shape: ``payload_class`` is
    the only trigger, its sole defined value is ``"command"``, and a v4
    request with the field absent digests as the v3 request it is.
    """

    VECTOR = {
        "control_id": "cmd-1",
        "events": [{"type": "text", "text": "/compact"}, {"type": "key", "key": "Enter"}],
        "payload_class": "command",
        "expected_identity": {
            "terminal_id": "term-1",
            "terminal_generation": "gen-3",
            "provider_process_id": 4242,
            "provider": "kimi_cli",
            "session_name": "cao-demo",
        },
    }
    PREIMAGE = (
        '{"domain":"cao-control-input-request-v4","schema_version":4,'
        '"control_id":"cmd-1",'
        '"events":[{"type":"text","text":"/compact"},{"type":"key","key":"Enter"}],'
        '"payload_class":"command",'
        '"expected_identity":{"terminal_id":"term-1",'
        '"terminal_incarnation":null,"terminal_generation":"gen-3","pane_birth_id":null,'
        '"provider_process_id":4242,"provider":"kimi_cli","native_session_id":null,'
        '"execution_mode":null,"session_name":"cao-demo"}}\n'
    )
    DIGEST = "6f774aba4cffa06755e981c89bbf5bf41e4608b0d9bedec47e71fa453ed8b2d5"

    def test_v4_domain_is_separate_from_v1_v2_and_v3(self):
        assert contract.CONTROL_INPUT_DIGEST_DOMAIN_V4 == "cao-control-input-request-v4"
        assert contract.CONTROL_INPUT_DIGEST_DOMAIN_V4 != contract.CONTROL_INPUT_DIGEST_DOMAIN
        assert contract.CONTROL_INPUT_DIGEST_DOMAIN_V4 != contract.CONTROL_INPUT_DIGEST_DOMAIN_V2
        assert contract.CONTROL_INPUT_DIGEST_DOMAIN_V4 != contract.CONTROL_INPUT_DIGEST_DOMAIN_V3
        assert contract.CONTROL_INPUT_REQUEST_SCHEMA_VERSION_V4 == 4

    def test_v4_field_order_splices_payload_class_between_events_and_identity(self):
        order = contract.REQUEST_DIGEST_FIELD_ORDER_V4
        assert order == (
            "domain",
            "schema_version",
            "control_id",
            "events",
            "payload_class",
            "expected_identity",
        )
        assert order != tuple(sorted(order))

    def test_matches_the_recorded_digest(self):
        assert (
            contract.control_input_request_digest_v4(
                control_id=self.VECTOR["control_id"],
                events=self.VECTOR["events"],
                payload_class=self.VECTOR["payload_class"],
                expected_identity=self.VECTOR["expected_identity"],
            )
            == self.DIGEST
        )

    def test_matches_the_recorded_preimage_byte_for_byte(self):
        assert (
            hashlib.sha256(self.PREIMAGE.encode("utf-8")).hexdigest() == self.DIGEST
        ), "the recorded v4 preimage does not hash to the recorded digest"
        encoded = canonical_json.encode_canonical(
            {
                "domain": contract.CONTROL_INPUT_DIGEST_DOMAIN_V4,
                "schema_version": contract.CONTROL_INPUT_REQUEST_SCHEMA_VERSION_V4,
                "control_id": self.VECTOR["control_id"],
                "events": contract.normalize_sequence_events(self.VECTOR["events"]),
                "payload_class": self.VECTOR["payload_class"],
                "expected_identity": contract.normalize_expected_identity(
                    self.VECTOR["expected_identity"]
                ),
            }
        )
        assert encoded.decode("utf-8") == self.PREIMAGE

    def test_the_declaration_participates_in_the_digest(self):
        """A declared and an undeclared request of the same id and events
        must never digest alike (rebound blindness is the failure the
        carrier exists to prevent)."""
        declared = contract.control_input_request_digest_v4(
            control_id="cmd-1",
            events=self.VECTOR["events"],
            payload_class="command",
            expected_identity=self.VECTOR["expected_identity"],
        )
        undeclared_v3 = contract.control_input_request_digest_v3(
            control_id="cmd-1",
            events=self.VECTOR["events"],
            expected_identity=self.VECTOR["expected_identity"],
        )
        null_declared_v4 = contract.control_input_request_digest_v4(
            control_id="cmd-1",
            events=self.VECTOR["events"],
            payload_class=None,
            expected_identity=self.VECTOR["expected_identity"],
        )
        assert declared != undeclared_v3
        assert declared != null_declared_v4
        # "v4 with no declaration" and "v3" are different requests too:
        # the domain separates them even though both mean prose.
        assert null_declared_v4 != undeclared_v3

    def test_a_non_string_payload_class_is_not_declarable(self):
        """The wire type is pinned the way ``chord``'s is in v2: anything
        else raises here and is the typed malformed-declaration refusal at
        the service layer."""
        with pytest.raises(ValueError):
            contract.control_input_request_digest_v4(
                control_id="cmd-1",
                events=self.VECTOR["events"],
                payload_class=42,
                expected_identity=None,
            )

    def test_declaration_validity_is_not_decided_by_the_digest(self):
        """A digest must be computable for a declaration the server will
        then refuse, so the two sides never disagree about which requests
        exist."""
        digest = contract.control_input_request_digest_v4(
            control_id="cmd-2",
            events=[{"type": "text", "text": "not a command"}],
            payload_class="command",
            expected_identity=None,
        )
        assert len(digest) == 64

    def test_v3_digest_bytes_are_unchanged_by_v4(self):
        """The v4 addition must not move what a v3 request digests to."""
        assert (
            contract.control_input_request_digest_v3(
                control_id="seq-1",
                events=TestV3SequenceDigest.VECTOR["events"],
                expected_identity=TestV3SequenceDigest.VECTOR["expected_identity"],
            )
            == TestV3SequenceDigest.DIGEST
        )

    @pytest.mark.parametrize(
        "events,violation",
        [
            # The two grammar shapes: bare command text, and the fused
            # submitting Enter (the registry Compact shape).
            ([{"type": "text", "text": "/compact"}], False),
            ([{"type": "text", "text": "/compact"}, {"type": "key", "key": "Enter"}], False),
            ([{"type": "text", "text": "/"}], False),  # bare slash is still slash-led
            # Everything else is malformed under a declaration.
            ([{"type": "text", "text": "prose"}], True),  # not slash-led
            ([{"type": "text", "text": "see /tmp/x"}], True),  # slash later, not leading
            ([{"type": "key", "key": "Enter"}], True),  # no text event
            ([{"type": "text", "text": "/a"}, {"type": "text", "text": "/b"}], True),
            ([{"type": "text", "text": "/a"}, {"type": "key", "key": "Escape"}], True),
            ([{"type": "text", "text": "/a"}, {"type": "chord", "chord": "C-s"}], True),
            (
                [
                    {"type": "text", "text": "/a"},
                    {"type": "key", "key": "Enter"},
                    {"type": "key", "key": "Enter"},
                ],
                True,
            ),
        ],
    )
    def test_the_command_grammar(self, events, violation):
        verdict = contract.command_declaration_violation(events)
        if violation:
            assert isinstance(verdict, str) and verdict
        else:
            assert verdict is None


class TestV4InteractiveDeclaration:
    """Schema v4's second declared class (§6.7, r15): ``"interactive"``.

    The declaration is a distinct request identity — declared interactive,
    declared command, and undeclared requests of the same id and events
    digest differently under the one v4 domain — while its legal payload
    is the ordinary v3 sequence grammar (no command grammar applies).
    """

    VECTOR = {
        "control_id": "ctl-interactive-1",
        "events": [{"type": "text", "text": "hello mid-turn"}, {"type": "key", "key": "Enter"}],
        "payload_class": "interactive",
        "expected_identity": {
            "terminal_id": "term-1",
            "terminal_generation": "gen-3",
            "provider_process_id": 4242,
            "provider": "kimi_cli",
            "session_name": "cao-demo",
        },
    }
    PREIMAGE = (
        '{"domain":"cao-control-input-request-v4","schema_version":4,'
        '"control_id":"ctl-interactive-1",'
        '"events":[{"type":"text","text":"hello mid-turn"},{"type":"key","key":"Enter"}],'
        '"payload_class":"interactive",'
        '"expected_identity":{"terminal_id":"term-1",'
        '"terminal_incarnation":null,"terminal_generation":"gen-3","pane_birth_id":null,'
        '"provider_process_id":4242,"provider":"kimi_cli","native_session_id":null,'
        '"execution_mode":null,"session_name":"cao-demo"}}\n'
    )
    DIGEST = "522621f6b05c036a63bfd8a0ec6b3393a589fcf3811b502c4a814c35591e4ef2"

    def test_interactive_matches_the_recorded_digest(self):
        assert (
            contract.control_input_request_digest_v4(
                control_id=self.VECTOR["control_id"],
                events=self.VECTOR["events"],
                payload_class=self.VECTOR["payload_class"],
                expected_identity=self.VECTOR["expected_identity"],
            )
            == self.DIGEST
        )

    def test_interactive_matches_the_recorded_preimage_byte_for_byte(self):
        assert hashlib.sha256(self.PREIMAGE.encode("utf-8")).hexdigest() == self.DIGEST
        encoded = canonical_json.encode_canonical(
            {
                "domain": contract.CONTROL_INPUT_DIGEST_DOMAIN_V4,
                "schema_version": contract.CONTROL_INPUT_REQUEST_SCHEMA_VERSION_V4,
                "control_id": self.VECTOR["control_id"],
                "events": contract.normalize_sequence_events(self.VECTOR["events"]),
                "payload_class": self.VECTOR["payload_class"],
                "expected_identity": contract.normalize_expected_identity(
                    self.VECTOR["expected_identity"]
                ),
            }
        )
        assert encoded.decode("utf-8") == self.PREIMAGE

    def test_the_three_declaration_states_digest_distinctly(self):
        """Declared interactive, declared command, and undeclared requests
        of one id and one events array are three different requests."""
        interactive = contract.control_input_request_digest_v4(
            control_id=self.VECTOR["control_id"],
            events=self.VECTOR["events"],
            payload_class="interactive",
            expected_identity=self.VECTOR["expected_identity"],
        )
        command = contract.control_input_request_digest_v4(
            control_id=self.VECTOR["control_id"],
            events=self.VECTOR["events"],
            payload_class="command",
            expected_identity=self.VECTOR["expected_identity"],
        )
        undeclared = contract.control_input_request_digest_v3(
            control_id=self.VECTOR["control_id"],
            events=self.VECTOR["events"],
            expected_identity=self.VECTOR["expected_identity"],
        )
        assert len({interactive, command, undeclared}) == 3


class TestCrossImplementationDigest:
    """The fork and the conductor must produce the same 64 hex characters.

    Two independently-reasonable digests are each correct in isolation
    and disagree only when a conflict actually needs detecting, so the
    divergence would surface as a spurious rebind refusal on exactly the
    request whose identity mattered most.  The vector below is not a
    self-check: the expected preimage and hex were produced by the
    conductor's own encoder and are asserted here byte for byte, so a
    one-sided edit to either implementation fails this test rather than
    being discovered in production.
    """

    # From conduct/lib/control_input.py's request_digest at conductor
    # commit a72cc76, recorded in that lane's checkpoint-3 §3.2.
    VECTOR = {
        "control_id": "abc123def456",
        "text": "/compact",
        "enter": True,
        "expected_identity": {"terminal_id": "t-1", "terminal_generation": "g-7"},
    }
    PREIMAGE = (
        '{"domain":"cao-control-input-request-v1","schema_version":1,'
        '"control_id":"abc123def456","text":"/compact","enter":true,'
        '"expected_identity":{"terminal_id":"t-1","terminal_incarnation":null,'
        '"terminal_generation":"g-7","pane_birth_id":null,"provider_process_id":null,'
        '"provider":null,"native_session_id":null,"execution_mode":null,'
        '"session_name":null}}\n'
    )
    DIGEST = "9199aa0a709bb05c3ba9c4ebff633368e2ebaf79fc3126f83cf9e4eca4ecddc6"

    def test_matches_the_conductor_digest(self):
        assert contract.control_input_request_digest(**self.VECTOR) == self.DIGEST

    def test_matches_the_conductor_preimage_byte_for_byte(self):
        """Pin the bytes too: equal hashes of different bytes is luck, not agreement."""
        assert (
            hashlib.sha256(self.PREIMAGE.encode("utf-8")).hexdigest() == self.DIGEST
        ), "the recorded preimage does not hash to the recorded digest"
        encoded = canonical_json.encode_canonical(
            {
                "domain": contract.CONTROL_INPUT_DIGEST_DOMAIN,
                "schema_version": 1,
                "control_id": self.VECTOR["control_id"],
                "text": self.VECTOR["text"],
                "enter": self.VECTOR["enter"],
                "expected_identity": contract.normalize_expected_identity(
                    self.VECTOR["expected_identity"]
                ),
            }
        )
        assert encoded.decode("utf-8") == self.PREIMAGE

    def test_the_identity_field_set_is_the_conductor_s(self):
        """Adding a tenth field here would silently fork the digest."""
        assert contract.IDENTITY_FIELDS == (
            "terminal_id",
            "terminal_incarnation",
            "terminal_generation",
            "pane_birth_id",
            "provider_process_id",
            "provider",
            "native_session_id",
            "execution_mode",
            "session_name",
        )


class TestExpectedIdentity:
    def test_absences_become_explicit_nulls_in_fixed_order(self):
        normalized = contract.normalize_expected_identity({"terminal_id": "t-1"})
        assert tuple(normalized) == contract.IDENTITY_FIELDS
        assert normalized["terminal_id"] == "t-1"
        assert all(normalized[name] is None for name in contract.IDENTITY_FIELDS[1:])

    def test_none_and_empty_agree(self):
        assert contract.normalize_expected_identity(None) == contract.normalize_expected_identity(
            {}
        )

    def test_an_unknown_field_is_refused_not_ignored(self):
        """An ignored misspelling is 'no expectation for the field I meant'."""
        with pytest.raises(ValueError) as excinfo:
            contract.normalize_expected_identity({"terminal_generatoin": "g-1"})
        assert "terminal_generatoin" in str(excinfo.value)

    @pytest.mark.parametrize(
        "identity",
        [
            {"terminal_id": ""},  # empty string is not 'no expectation'
            {"provider_process_id": -1},
            {"provider_process_id": True},  # bool is an int in Python, not on the wire
            {"terminal_id": 1.5},
            {"terminal_id": b"t-1"},
            {"terminal_id": ["t-1"]},
        ],
    )
    def test_unencodable_expectations_are_refused(self, identity):
        with pytest.raises(ValueError):
            contract.normalize_expected_identity(identity)

    def test_a_non_mapping_is_refused(self):
        with pytest.raises(ValueError):
            contract.normalize_expected_identity([("terminal_id", "t-1")])


class TestReasonOutcomeBinding:
    """Every reason carries exactly one outcome, and the map is total."""

    def test_every_reason_is_bound(self):
        assert set(contract.REASON_OUTCOMES) == set(contract.CONTROL_INPUT_REASON_CODES)

    def test_every_bound_outcome_is_a_real_outcome(self):
        assert set(contract.REASON_OUTCOMES.values()) <= contract.CONTROL_INPUT_OUTCOMES

    @pytest.mark.parametrize(
        "reason",
        [
            contract.REASON_RESPONSE_LOST,
            contract.REASON_WRITE_INCOMPLETE,
            contract.REASON_OWNER_LOST_MID_WRITE,
        ],
    )
    def test_post_attempt_uncertainty_is_never_reattemptable(self, reason):
        """The whole point: these can never license a second delivery."""
        outcome = contract.outcome_for_reason(reason)
        assert outcome == contract.AMBIGUOUS
        assert contract.is_reattemptable(outcome) is False

    @pytest.mark.parametrize(
        "reason",
        [
            contract.REASON_UNKNOWN_TERMINAL,
            contract.REASON_IDENTITY_MISMATCH,
            contract.REASON_STALE_GENERATION,
            contract.REASON_PANE_BUSY,
            contract.REASON_ILLEGAL_CONTROL_BYTES,
            contract.REASON_REQUEST_REBOUND,
            contract.REASON_OWNER_LOST_BEFORE_WRITE,
        ],
    )
    def test_pre_write_reasons_are_refusals(self, reason):
        assert contract.outcome_for_reason(reason) == contract.REFUSED

    @pytest.mark.parametrize(
        "reason",
        [contract.REASON_CONTROL_ROUTE_ABSENT, contract.REASON_PROTOCOL_MISMATCH],
    )
    def test_old_server_reasons_are_unsupported(self, reason):
        """An old server is never a refusal a caller could retry into success."""
        assert contract.outcome_for_reason(reason) == contract.UNSUPPORTED

    def test_no_reason_is_bound_to_accepted(self):
        """A delivered control has no reason; a reason there would explain away a write."""
        assert contract.ACCEPTED not in set(contract.REASON_OUTCOMES.values())

    def test_an_unknown_reason_raises_rather_than_defaulting(self):
        """Both defaults are wrong: one invents a retry, one strands a request."""
        with pytest.raises(ValueError):
            contract.outcome_for_reason("owner-lost")

    def test_only_refused_is_reattemptable(self):
        assert contract.is_reattemptable(contract.REFUSED) is True
        for outcome in (contract.ACCEPTED, contract.AMBIGUOUS, contract.UNSUPPORTED):
            assert contract.is_reattemptable(outcome) is False


class TestBracketedPasteSentinels:
    def test_sentinels_are_the_decset_2004_sequences(self):
        assert contract.BRACKETED_PASTE_START == "\x1b[200~"
        assert contract.BRACKETED_PASTE_END == "\x1b[201~"

    def test_the_c1_spellings_are_screened_too(self):
        """U+009B is the 8-bit form of 'ESC ['; a terminal reads them alike."""
        assert contract.BRACKETED_PASTE_START_C1 == "\x9b200~"
        assert contract.BRACKETED_PASTE_END_C1 == "\x9b201~"
        assert set(contract.BRACKETED_PASTE_SENTINELS) == {
            "\x1b[200~",
            "\x1b[201~",
            "\x9b200~",
            "\x9b201~",
        }

    @pytest.mark.parametrize(
        "payload",
        [
            "\x1b[200~hello",
            "hello\x1b[201~",
            "a\x1b[200~b\x1b[201~c",
            b"\x1b[200~hello",
            b"hello\x1b[201~",
            # The C1 spelling: a screen with a known bypass is not a screen.
            "\x9b200~hello",
            "hello\x9b201~",
            # Raw off a terminal (0x9b) and the same text as UTF-8 (0xc2 0x9b).
            b"\x9b200~hello",
            b"hello\x9b201~",
            "\x9b200~payload".encode("utf-8"),
            "trailing\x9b201~".encode("utf-8"),
        ],
    )
    def test_detects_sentinels_in_text_and_bytes(self, payload):
        assert contract.contains_bracketed_paste_sentinel(payload) is True

    @pytest.mark.parametrize(
        "payload",
        [
            "/compact",
            "",
            "^[[200~ rendered by a leaking pane",
            "\x1b[2J",
            b"/compact",
            b"",
            # A C1 CSI that is not paste framing must still pass, or the
            # screen becomes a ban on a byte rather than on a sequence.
            "\x9b31m",
            b"\x9b31m",
        ],
    )
    def test_clean_payloads_are_clean(self, payload):
        assert contract.contains_bracketed_paste_sentinel(payload) is False


class TestTransportClassification:
    def test_two_hundred_defers_to_the_typed_body(self):
        assert contract.classify_transport_status(200) is None

    def test_no_response_is_ambiguous_not_absent(self):
        """A lost response is not evidence that nothing was delivered."""
        assert contract.classify_transport_status(None) == contract.AMBIGUOUS

    @pytest.mark.parametrize("status", [404, 405, 501])
    def test_missing_route_is_unsupported(self, status):
        assert contract.classify_transport_status(status) == contract.UNSUPPORTED

    def test_protocol_literal_rejection_is_unsupported(self):
        assert (
            contract.classify_transport_status(422, protocol_mismatch=True) == contract.UNSUPPORTED
        )

    def test_plain_unprocessable_entity_is_a_refusal(self):
        """A malformed body is the caller's bug, not an old server."""
        assert contract.classify_transport_status(422) == contract.REFUSED

    @pytest.mark.parametrize("status", [400, 401, 403, 409, 429, 418])
    def test_client_errors_are_refusals(self, status):
        assert contract.classify_transport_status(status) == contract.REFUSED

    @pytest.mark.parametrize("status", [408, 425, 500, 502, 503, 504, 599])
    def test_interrupted_or_failed_requests_are_ambiguous(self, status):
        assert contract.classify_transport_status(status) == contract.AMBIGUOUS

    @pytest.mark.parametrize("status", [100, 204, 302, 0, -1])
    def test_unrecognised_results_fail_towards_ambiguous(self, status):
        assert contract.classify_transport_status(status) == contract.AMBIGUOUS

    def test_every_status_yields_a_typed_outcome_or_a_body_read(self):
        """No status may leave a caller without a typed answer to report."""
        statuses = [None] + list(range(100, 600))
        for status in statuses:
            for mismatch in (False, True):
                outcome = contract.classify_transport_status(status, protocol_mismatch=mismatch)
                assert outcome is None or outcome in contract.CONTROL_INPUT_OUTCOMES
                if status != 200:
                    assert outcome is not None

    def test_no_status_is_reported_as_accepted_without_a_body(self):
        """Acceptance is a fact the server states, never one inferred here."""
        for status in [None] + list(range(100, 600)):
            assert contract.classify_transport_status(status) != contract.ACCEPTED
