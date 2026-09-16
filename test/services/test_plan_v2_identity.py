"""Behavioral contract for the additive, publicly verifiable ``plan-v2`` identity."""

import dataclasses
import json
from itertools import combinations

import pytest

from cli_agent_orchestrator.constants import WORKFLOW_MANIFEST_MAX_BYTES
from cli_agent_orchestrator.services import execution_manifest as em
from cli_agent_orchestrator.services import plan_identifier as pi

SECRET_INPUT = "AKIAIOSFODNN7EXAMPLE"
PRIVATE_PATH = "/private/operator/checkout"


def _materials(**overrides: bytes) -> dict[str, bytes]:
    material = {
        "artifact_hash": b"SCOPE = {'main': {}}\nprint('version one')\n",
        "declaration": b'{"targets":{"main":{"agents":["developer"]}},"version":1}',
        "targets": PRIVATE_PATH.encode(),
        "limits": b'{"max_steps":10,"timeout":600}',
        "retry_policy": b'{"retries":3}',
        "policy": b'{"profile":"developer","prompt":"private prompt"}',
        "memory": b'{"mode":"off"}',
    }
    material.update(overrides)
    return material


def _components(
    *,
    materials: dict[str, bytes] | None = None,
    inputs: dict[str, object] | None = None,
    **overrides: object,
) -> pi.PlanV2Components:
    raw = materials or _materials()
    fields: dict[str, object] = {
        "tier": "script",
        "artifact_hash": pi.digest_bytes(raw["artifact_hash"]),
        "declaration": pi.digest_bytes(raw["declaration"]),
        "targets": pi.digest_bytes(raw["targets"]),
        "inputs": pi.digest_inputs(inputs if inputs is not None else {"ticket": "PR-699"}),
        "limits": pi.digest_bytes(raw["limits"]),
        "retry_policy": pi.digest_bytes(raw["retry_policy"]),
        "policy": pi.digest_bytes(raw["policy"]),
        "memory": pi.digest_bytes(raw["memory"]),
    }
    fields.update(overrides)
    return pi.PlanV2Components(**fields)


def test_v2_component_contract_and_known_digest_are_pinned():
    assert pi.PLAN_V2_COMPONENT_SET_VERSION == "2"
    assert pi.PLAN_V2_COMPONENTS == (
        "scheme",
        "component_set_version",
        "tier",
        "artifact_hash",
        "declaration",
        "targets",
        "inputs",
        "limits",
        "retry_policy",
        "policy",
        "memory",
    )
    assert pi.compute_v2(_components()) == (
        "plan-v2:c34f23c715aaae0a0a396929e4848c8c7a86897badf6ccd67be8b730e324c561"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tier", "yaml"),
        ("artifact_hash", "1" * 64),
        ("declaration", "2" * 64),
        ("targets", "3" * 64),
        ("inputs", (pi.InputDigest("4" * 64, "5" * 64),)),
        ("limits", "6" * 64),
        ("retry_policy", "7" * 64),
        ("policy", "8" * 64),
        ("memory", "9" * 64),
    ],
)
def test_mutating_each_execution_component_changes_the_plan_id(field: str, value: object):
    baseline = _components()
    assert {item.name for item in dataclasses.fields(baseline)} == {
        "tier",
        "artifact_hash",
        "declaration",
        "targets",
        "inputs",
        "limits",
        "retry_policy",
        "policy",
        "memory",
    }
    assert pi.compute_v2(dataclasses.replace(baseline, **{field: value})) != pi.compute_v2(baseline)


def test_same_scope_with_changed_artifact_bytes_is_a_new_plan():
    first = _materials()
    second = _materials(artifact_hash=b"SCOPE = {'main': {}}\nprint('version two')\n")
    assert first["declaration"] == second["declaration"]
    assert pi.compute_v2(_components(materials=first)) != pi.compute_v2(
        _components(materials=second)
    )


@pytest.mark.parametrize("missing", [None, "", "not-a-digest", "A" * 64])
def test_missing_or_malformed_component_refuses(missing: object):
    with pytest.raises(pi.PlanV2ComponentError, match="artifact_hash"):
        pi.compute_v2(_components(artifact_hash=missing))


def test_input_values_are_hashed_per_key_before_the_public_document_exists():
    components = _components(inputs={"token": SECRET_INPUT, "ticket": "PR-699"})
    changed_secret = _components(inputs={"token": "different secret", "ticket": "PR-699"})
    document = pi.v2_component_document(components)
    encoded = json.dumps(document, sort_keys=True)

    assert SECRET_INPUT not in encoded
    assert pi.compute_v2(changed_secret) != pi.compute_v2(components)
    assert len(document["inputs"]) == 2
    assert all(set(entry) == {"key_digest", "value_digest"} for entry in document["inputs"])
    assert pi.verify_v2_components(document, pi.compute_v2(components))


@pytest.mark.parametrize(
    "values",
    [
        (1, "1", True, 1.0),
        (600, 600.0, "600"),
        (1.0000001, 1.0000002),
        (None, "None", ""),
        ([1, "1"], [1.0, "1"], {"value": [1, "1"]}),
    ],
)
def test_v2_canonical_values_preserve_types_and_numeric_precision(values: tuple[object, ...]):
    encoded = [pi.canonical_component_bytes(value) for value in values]
    assert all(left != right for left, right in combinations(encoded, 2))
    assert len({pi.digest_json(value) for value in values}) == len(values)


def test_v2_canonical_dict_order_is_stable_and_nested_types_remain_distinct():
    first = {"z": [1, {"flag": True}], "a": {"value": 1.0}}
    reordered = {"a": {"value": 1.0}, "z": [1, {"flag": True}]}
    changed = {"a": {"value": 1}, "z": [1, {"flag": True}]}

    assert pi.canonical_component_bytes(first) == pi.canonical_component_bytes(reordered)
    assert pi.digest_json(first) != pi.digest_json(changed)


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        0,
        -1,
        600,
        600.0,
        1.0000001,
        "",
        "1",
        "ü",
        b"\x00\xff",
        bytearray(b"\x00\xff"),
        [],
        (1, "tuple"),
        {},
        [None, True, {"tag": "s", "values": [1, 1.0, b"\x00"]}],
        {"array": ["i", "f"], "nested": {"bytes": b"\x01"}},
    ],
)
def test_decode_component_bytes_strictly_round_trips_canonical_values(value):
    encoded = pi.canonical_component_bytes(value)

    decoded = pi.decode_component_bytes(encoded)

    assert pi.canonical_component_bytes(decoded) == encoded


def test_decoder_uses_the_intentional_shared_canonical_container_types():
    assert pi.decode_component_bytes(pi.canonical_component_bytes((1, 2))) == [1, 2]
    assert pi.decode_component_bytes(pi.canonical_component_bytes(bytearray(b"x"))) == b"x"


def _raw_frame(payload: bytes) -> bytes:
    return str(len(payload)).encode("ascii") + b":" + payload


@pytest.mark.parametrize(
    "malformed",
    [
        b"",
        b"z",
        b"ntrailing",
        b"b2",
        b"i:",
        b"i1",
        b"i2:1",
        b"i01:1",
        b"i2:+1",
        b"f3:nan",
        b"s2:\xff",
        b"y2:x",
        b"a1:nextra",
        b"o1:x",
        b"o" + _raw_frame(b"k" + _raw_frame(b"b") + b"n" + b"k" + _raw_frame(b"a") + b"n"),
        b"o" + _raw_frame(b"k" + _raw_frame(b"a") + b"n" + b"k" + _raw_frame(b"a") + b"n"),
        bytearray(b"n"),
        "n",
    ],
)
def test_decode_component_bytes_refuses_malformed_or_noncanonical_forms(malformed):
    with pytest.raises(pi.PlanV2ComponentError) as excinfo:
        pi.decode_component_bytes(malformed)

    assert str(excinfo.value) == "encoded value component is invalid"


def test_decode_component_bytes_error_never_echoes_malformed_secret_material():
    with pytest.raises(pi.PlanV2ComponentError) as excinfo:
        pi.decode_component_bytes(b"s999:" + SECRET_INPUT.encode())

    assert SECRET_INPUT not in str(excinfo.value)


def test_v2_bytes_and_bytearray_share_the_binary_encoding_only():
    assert pi.canonical_component_bytes(b"value") == pi.canonical_component_bytes(
        bytearray(b"value")
    )
    assert pi.canonical_component_bytes(b"value") != pi.canonical_component_bytes("value")


def test_v2_canonicalization_refuses_unsupported_secret_without_echoing_it():
    class UnsupportedSecret:
        def __repr__(self) -> str:
            return f"UnsupportedSecret({SECRET_INPUT})"

    for value in (
        UnsupportedSecret(),
        {1: "non-string key"},
        float("nan"),
        float("inf"),
    ):
        with pytest.raises(pi.PlanV2ComponentError) as excinfo:
            pi.canonical_component_bytes(value)
        assert SECRET_INPUT not in str(excinfo.value)


@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"])
def test_v2_utf8_surrogates_refuse_with_fixed_secret_safe_component_errors(surrogate):
    secret_surrogate = SECRET_INPUT + surrogate
    cases = (
        lambda: pi.canonical_key_bytes(secret_surrogate),
        lambda: pi.canonical_component_bytes(secret_surrogate),
        lambda: pi.canonical_component_bytes({"nested": secret_surrogate}),
        lambda: pi.canonical_component_bytes({secret_surrogate: "value"}),
        lambda: pi.digest_inputs({"key": secret_surrogate}),
        lambda: pi.digest_inputs({secret_surrogate: "value"}),
    )

    for operation in cases:
        with pytest.raises(pi.PlanV2ComponentError) as excinfo:
            operation()
        assert SECRET_INPUT not in str(excinfo.value)
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__suppress_context__ is True


def test_redacted_public_document_alone_recomputes_the_plan_id():
    components = _components(inputs={"token": SECRET_INPUT})
    envelope = em.build_v2_public(components)
    encoded = em.serialise_v2_public(envelope)

    assert SECRET_INPUT not in encoded
    assert PRIVATE_PATH not in encoded
    assert em.verify_v2_public(encoded)


def test_sorted_json_transport_is_publicly_verifiable():
    envelope = em.build_v2_public(_components(inputs={"token": SECRET_INPUT}))
    transported = json.dumps(json.loads(em.serialise_v2_public(envelope)), sort_keys=True)

    assert em.verify_v2_public(transported)


def test_public_verifiers_return_false_for_surrogate_component_documents():
    document = pi.v2_component_document(_components())
    document["tier"] = "\ud800"
    public = {
        "approved": {"plan_id": pi.compute_v2(_components()), "components": document},
        "evidence": [],
        "evidence_dropped": 0,
    }

    assert pi.verify_v2_components(document, public["approved"]["plan_id"]) is False
    assert em.verify_v2_public(public) is False


def test_build_v2_public_accepts_exact_size_and_refuses_one_byte_over(
    monkeypatch: pytest.MonkeyPatch,
):
    components = _components()
    baseline = em.build_v2_public(components)
    exact_size = len(em.serialise_v2_public(baseline).encode("utf-8"))

    monkeypatch.setattr(em, "WORKFLOW_MANIFEST_MAX_BYTES", exact_size)
    assert em.build_v2_public(components) == baseline

    monkeypatch.setattr(em, "WORKFLOW_MANIFEST_MAX_BYTES", exact_size - 1)
    with pytest.raises(em.PlanV2PublicEnvelopeTooLargeError) as excinfo:
        em.build_v2_public(components)
    assert str(excinfo.value) == "plan-v2 public envelope exceeds size limit"


def test_large_public_input_digest_list_refuses_even_when_private_material_is_small():
    inputs = tuple(
        pi.InputDigest(key_digest=f"{index:064x}", value_digest=f"{index + 2000:064x}")
        for index in range(1, 1600)
    )
    components = dataclasses.replace(_components(), inputs=inputs)
    private_material_bytes = len(inputs) * 2

    assert private_material_bytes < WORKFLOW_MANIFEST_MAX_BYTES
    with pytest.raises(em.PlanV2PublicEnvelopeTooLargeError):
        em.build_v2_public(components)


@pytest.mark.parametrize("change", ["extra", "missing", "reordered-inputs"])
def test_public_verification_refuses_component_shape_or_input_order_changes(change: str):
    envelope = json.loads(
        em.serialise_v2_public(em.build_v2_public(_components(inputs={"alpha": 1, "omega": 2})))
    )
    components = envelope["approved"]["components"]
    if change == "extra":
        components["unexpected"] = "0" * 64
    elif change == "missing":
        del components["memory"]
    else:
        components["inputs"].reverse()

    assert not em.verify_v2_public(json.dumps(envelope, sort_keys=True))


def test_evidence_append_and_bounding_leave_approved_bytes_identical():
    envelope = em.build_v2_public(_components())
    approved_before = em.approved_v2_bytes(envelope)

    appended = em.append_v2_evidence(envelope, {"result": "ok"})
    bounded = em.append_v2_evidence(appended, {"detail": "x" * 300_000})

    assert em.approved_v2_bytes(appended) == approved_before
    assert em.approved_v2_bytes(bounded) == approved_before
    assert bounded.evidence_truncated is True
    assert bounded.evidence_dropped == 1
    assert em.verify_v2_public(em.serialise_v2_public(bounded))

    terminal = em.append_v2_evidence(bounded, {"later": "small"})
    assert terminal.evidence_dropped == 2
    assert terminal.evidence_json == appended.evidence_json
    assert em.approved_v2_bytes(terminal) == approved_before
    assert em.v2_public_size_bytes(terminal) <= WORKFLOW_MANIFEST_MAX_BYTES


@pytest.mark.parametrize("accepted", [0, 1, 4])
@pytest.mark.parametrize("dropped", [0, 1, 12])
def test_cached_evidence_bytes_match_exact_serialized_utf8_length(accepted: int, dropped: int):
    envelope = em.build_v2_public(_components())
    for index in range(accepted):
        envelope = em.append_v2_evidence(
            envelope, {"index": index, "unicode": "\N{SNOWMAN}" * (index + 1)}
        )
    if dropped:
        envelope = dataclasses.replace(envelope, evidence_dropped=dropped)

    assert em.v2_public_size_bytes(envelope) == len(
        em.serialise_v2_public(envelope).encode("utf-8")
    )
    assert json.loads(em.serialise_v2_public(envelope))["evidence_dropped"] == dropped


def test_evidence_cache_mismatch_is_refused_at_dataclass_replace_with_unicode():
    envelope = em.append_v2_evidence(em.build_v2_public(_components()), {"unicode": "\N{SNOWMAN}"})

    with pytest.raises(em.EvidenceEncodingError) as excinfo:
        dataclasses.replace(envelope, evidence_bytes=envelope.evidence_bytes - 1)

    assert str(excinfo.value) == "plan-v2 evidence metadata is invalid"


@pytest.mark.parametrize(
    "approved",
    [
        None,
        {"plan_id": "plan-v2:" + "0" * 64, "components_json": "{}"},
        em.ApprovedPlanV2(plan_id=1, components_json="{}"),
        em.ApprovedPlanV2(plan_id="plan-v1:" + "0" * 64, components_json="{}"),
        em.ApprovedPlanV2(plan_id="plan-v2:" + "A" * 64, components_json="{}"),
        em.ApprovedPlanV2(plan_id="plan-v2:short", components_json="{}"),
        em.ApprovedPlanV2(plan_id="plan-v2:" + "0" * 64, components_json=None),
        em.ApprovedPlanV2(
            plan_id="plan-v2:" + "0" * 64,
            components_json='{"secret":"\ud800"}',
        ),
    ],
)
def test_public_envelope_refuses_invalid_approved_metadata_secret_safely(approved):
    with pytest.raises(em.PlanV2PublicEnvelopeEncodingError) as excinfo:
        em.PlanV2PublicEnvelope(approved=approved)

    assert str(excinfo.value) == "plan-v2 public envelope is invalid"
    assert SECRET_INPUT not in str(excinfo.value)


@pytest.mark.parametrize(
    "approved",
    [
        None,
        em.ApprovedPlanV2(
            plan_id="plan-v2:" + "0" * 64,
            components_json='{"secret":"\ud800"}',
        ),
    ],
)
def test_public_verifier_refuses_constructor_bypassed_invalid_envelopes(approved):
    bypassed = object.__new__(em.PlanV2PublicEnvelope)
    object.__setattr__(bypassed, "approved", approved)
    object.__setattr__(bypassed, "evidence_json", ())
    object.__setattr__(bypassed, "evidence_dropped", 0)
    object.__setattr__(bypassed, "evidence_bytes", 0)

    assert em.verify_v2_public(bypassed) is False


def test_approved_bytes_use_the_stored_component_json_without_reencoding():
    envelope = em.build_v2_public(_components())
    sorted_components = json.dumps(
        json.loads(envelope.approved.components_json),
        sort_keys=True,
        separators=(",", ":"),
    )
    transported = dataclasses.replace(
        envelope,
        approved=dataclasses.replace(envelope.approved, components_json=sorted_components),
    )

    assert sorted_components.encode() in em.approved_v2_bytes(transported)
    assert em.verify_v2_public(em.serialise_v2_public(transported))


def test_non_json_evidence_has_a_named_secret_safe_error():
    class UnsupportedSecret:
        def __repr__(self) -> str:
            return f"UnsupportedSecret({SECRET_INPUT})"

    with pytest.raises(em.EvidenceEncodingError) as excinfo:
        em.append_v2_evidence(em.build_v2_public(_components()), UnsupportedSecret())
    assert SECRET_INPUT not in str(excinfo.value)


@pytest.mark.parametrize(
    ("evidence", "safe_location"),
    [
        ({"safe": [object()]}, "evidence.object.value[0]"),
        ({object(): "value"}, "evidence.object.key"),
    ],
)
def test_non_json_evidence_error_names_only_a_safe_structural_location(
    evidence, safe_location: str
):
    with pytest.raises(em.EvidenceEncodingError) as excinfo:
        em.append_v2_evidence(em.build_v2_public(_components()), evidence)

    assert safe_location in str(excinfo.value)
    assert "object at" not in str(excinfo.value)


def test_non_json_evidence_never_renders_an_arbitrary_secret_key():
    class SecretKey:
        def __hash__(self) -> int:
            return 1

        def __repr__(self) -> str:
            return SECRET_INPUT

    with pytest.raises(em.EvidenceEncodingError) as excinfo:
        em.append_v2_evidence(em.build_v2_public(_components()), {SecretKey(): "value"})

    assert SECRET_INPUT not in str(excinfo.value)
    assert "evidence.object.key" in str(excinfo.value)


def test_forged_scheme_or_component_version_is_not_publicly_verifiable():
    encoded = em.serialise_v2_public(em.build_v2_public(_components()))
    original = json.loads(encoded)
    for field, value in (("scheme", "plan-v3"), ("component_set_version", "3")):
        forged = json.loads(encoded)
        forged["approved"]["components"][field] = value
        assert not em.verify_v2_public(json.dumps(forged))
        assert original["approved"] != forged["approved"]


def test_non_string_plan_id_is_not_publicly_verifiable():
    envelope = json.loads(em.serialise_v2_public(em.build_v2_public(_components())))
    envelope["approved"]["plan_id"] = {"unexpected": "shape"}

    assert not em.verify_v2_public(json.dumps(envelope))


@pytest.mark.parametrize("invalid", [None, [], 1, object()])
def test_public_verifier_returns_false_for_non_public_objects_without_repr(invalid):
    assert em.verify_v2_public(invalid) is False


def test_public_verifier_accepts_parsed_dict_and_refuses_malformed_dicts():
    document = json.loads(em.serialise_v2_public(em.build_v2_public(_components())))

    assert em.verify_v2_public(document) is True
    assert em.verify_v2_public({}) is False
    assert em.verify_v2_public({"approved": None}) is False


def test_public_verifier_returns_false_for_deeply_nested_json_transport():
    deeply_nested = '{"approved":' + "[" * 50_000 + "]" * 50_000 + "}"

    assert em.verify_v2_public(deeply_nested) is False


@pytest.mark.parametrize("control", [KeyboardInterrupt(), SystemExit()])
def test_public_verifier_does_not_swallow_process_control_from_validation(
    control: BaseException, monkeypatch: pytest.MonkeyPatch
):
    document = json.loads(em.serialise_v2_public(em.build_v2_public(_components())))

    def _interrupt_validation(*_args, **_kwargs):
        raise control

    monkeypatch.setattr(pi, "verify_v2_components", _interrupt_validation)

    with pytest.raises(type(control)):
        em.verify_v2_public(document)
