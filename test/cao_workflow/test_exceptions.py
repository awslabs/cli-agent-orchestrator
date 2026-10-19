"""Unit tests for the cao_workflow exception hierarchy."""

from __future__ import annotations

import json

import pytest

from cao_workflow import ShimHTTPError


@pytest.mark.parametrize(
    ("status", "kind", "message"),
    [
        (
            409,
            "diverged",
            "step 's1': run 'run-1' step 's1': the stored call fingerprint "
            "differs from this call's under the current scheme, so the step "
            "changed between runs at the same key",
        ),
        (
            409,
            "decision_required",
            "step 's1' [interrupted_no_policy]: run 'run-1' step 's1': the "
            "step was dispatched and its outcome is unknown, and no declared "
            "recovery policy permits re-execution",
        ),
        # Forward compatibility with the typed refusal introduced by PR #699.
        (409, "plan_inputs_changed", "resolved inputs no longer match"),
        (502, "error", "terminal t-1 reached ERROR status"),
        (504, "timeout", "step on terminal t-1 did not complete within 600s"),
    ],
)
def test_http_error_includes_nested_kind_and_message(status, kind, message):
    body = json.dumps({"detail": {"kind": kind, "message": message}})

    error = ShimHTTPError(status, body)

    assert str(error) == f"run-step returned HTTP {status} ({kind}): {message}"
    assert error.status == status
    assert error.body == body


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ({"kind": "diverged"}, "run-step returned HTTP 409 (diverged)"),
        (
            {"kind": "diverged", "message": 409},
            "run-step returned HTTP 409 (diverged)",
        ),
        ({"kind": "timeout", "message": ""}, "run-step returned HTTP 409 (timeout)"),
    ],
)
def test_http_error_includes_nested_kind_without_string_message(detail, expected):
    error = ShimHTTPError(409, json.dumps({"detail": detail}))

    assert str(error) == expected


@pytest.mark.parametrize(
    "body",
    [
        "not JSON",
        '["not", "a", "dict"]',
        '{"detail":"stale workflow generation"}',
        '{"detail":{"message":"missing kind"}}',
        '{"detail":{"kind":409,"message":"kind is not a string"}}',
        '{"detail":{"kind":"","message":"empty kind is absent"}}',
    ],
)
def test_http_error_keeps_default_message_for_untyped_body_shapes(body):
    error = ShimHTTPError(409, body)

    assert str(error) == "run-step returned HTTP 409"
