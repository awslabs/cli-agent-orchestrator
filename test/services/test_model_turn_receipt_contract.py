import pathlib
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from cli_agent_orchestrator.services import model_turn_receipt_contract as contract

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SRC_DIR = _REPO_ROOT / "src"

#: Candidate checkouts hosting the pinned canonical contract module. The
#: conductor definition pin is ``42af6fb2a4862211fcf1e1289ced44bb75b943c5``,
#: and conductor origin/main ``924b38dc7`` has no intervening change to the
#: module, so either checkout is an exact copy of the installed canonical.
_CANONICAL_PLUGIN_CANDIDATES = (
    pathlib.Path(
        "/Users/colin/Projects/cao-conductor-worktrees/"
        "fire-marshal-postmerge-auditor/cao-conductor/plugin"
    ),
    pathlib.Path("/Users/colin/Projects/cao-conductor/plugin"),
)


def _pinned_canonical_plugin_dir() -> pathlib.Path | None:
    for candidate in _CANONICAL_PLUGIN_CANDIDATES:
        if (candidate / "conductor_sentinel" / "model_turn_receipt_contract.py").is_file():
            return candidate
    return None


def _run_isolated(
    code: str, *, extra_paths: tuple[pathlib.Path, ...] = ()
) -> subprocess.CompletedProcess:
    """Run ``code`` in a fresh interpreter with the fork source pinned on path.

    The subprocess is the isolation boundary: import selection and
    ``sys.modules`` state can neither leak from this test process nor be
    mutated back into it.
    """
    prologue = f"import sys\nsys.path.insert(0, {str(_SRC_DIR)!r})\n"
    for path in extra_paths:
        prologue += f"sys.path.insert(0, {str(path)!r})\n"
    return subprocess.run(
        [sys.executable, "-c", prologue + code],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _assert_isolated_ok(code: str, *, extra_paths: tuple[pathlib.Path, ...] = ()) -> str:
    result = _run_isolated(code, extra_paths=extra_paths)
    assert (
        result.returncode == 0
    ), f"isolated probe failed ({result.returncode})\n{result.stdout}\n{result.stderr}"
    assert "OK" in result.stdout
    return result.stdout


_STANDALONE_PROBE = """
from datetime import datetime, timezone
from importlib.abc import MetaPathFinder
import sys


class _AbsentConductor:
    \"\"\"Force the top-level package to be missing for this interpreter.\"\"\"

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "conductor_sentinel" or fullname.startswith("conductor_sentinel."):
            raise ModuleNotFoundError(
                "No module named %r" % fullname, name="conductor_sentinel"
            )
        return None


sys.meta_path.insert(0, _AbsentConductor())

from cli_agent_orchestrator.services import model_turn_receipt_contract as facade

assert facade.CONTRACT_SOURCE == "standalone", facade.CONTRACT_SOURCE
assert facade.KIND == "submitted"
assert facade.SOURCE == "provider-adapter"
receipt = facade.build_receipt(
    message_id=7,
    message_sha256="a" * 64,
    message_created_at=datetime(2026, 7, 30, 12, 0, 0, 123456, tzinfo=timezone.utc),
    sender_id="supervisor",
    sender_generation="supervisor-generation",
    receiver_id="worker",
    receiver_generation="worker-generation",
    provider="codex",
    provider_session_id="session",
    provider_turn_id="turn",
    submitted_at=datetime(2026, 7, 30, 12, 0, 1, 654321, tzinfo=timezone.utc),
)
assert tuple(receipt) == facade.FIELDS
assert receipt["schema"] == "cao-model-turn-receipt-v1"
assert receipt["source"] == "provider-adapter"
assert receipt["message_created_at"].endswith(".123456Z")
assert receipt["submitted_at"].endswith(".654321Z")
print("OK")
"""

_MISSING_SUBMODULE_PROBE = """
try:
    from cli_agent_orchestrator.services import model_turn_receipt_contract as facade
except ModuleNotFoundError as exc:
    assert exc.name == "conductor_sentinel.model_turn_receipt_contract", exc.name
    print("OK")
    raise SystemExit(0)
else:
    print("NO-RAISE", facade.CONTRACT_SOURCE)
    raise SystemExit(2)
"""

_BROKEN_INTERNAL_IMPORT_PROBE = """
try:
    from cli_agent_orchestrator.services import model_turn_receipt_contract as facade
except ModuleNotFoundError as exc:
    assert exc.name == "definitely_missing_internal_module", exc.name
    print("OK")
    raise SystemExit(0)
else:
    print("NO-RAISE", facade.CONTRACT_SOURCE)
    raise SystemExit(2)
"""


def _installed_mode_probe(canonical_root: pathlib.Path) -> str:
    """Probe asserting exact object identity and values with the pinned module."""
    return f"""
import hashlib
from datetime import datetime, timezone

import conductor_sentinel.model_turn_receipt_contract as canonical
from cli_agent_orchestrator.services import model_turn_receipt_contract as facade

assert canonical.__file__.startswith({str(canonical_root)!r}), canonical.__file__
assert facade.CONTRACT_SOURCE == "conductor-sentinel", facade.CONTRACT_SOURCE

# exact object identity with the canonical module: no wrappers or copies
assert facade.ReceiptValidationError is canonical.ReceiptValidationError
assert facade.message_digest is canonical.message_digest
assert facade.is_message_digest is canonical.is_message_digest
assert facade.canonical_message_id is canonical.canonical_message_id
assert facade.parse_rfc3339_utc is canonical.parse_rfc3339_utc
assert facade.format_rfc3339_utc is canonical.format_rfc3339_utc
assert facade.receipt_endpoint_path is canonical.receipt_endpoint_path
assert facade.validate_receipt is canonical.validate_receipt
assert facade.build_receipt is canonical.build_receipt
assert facade.FIELDS is canonical.FIELDS
assert facade.TIMESTAMP_FIELDS is canonical.TIMESTAMP_FIELDS
assert facade.TIMESTAMP_VECTORS is canonical.TIMESTAMP_VECTORS

# canonical values are exact
assert facade.SCHEMA == canonical.SCHEMA == "cao-model-turn-receipt-v1"
assert facade.KIND_SUBMITTED == canonical.KIND_SUBMITTED == "submitted"
assert facade.SOURCE_PROVIDER_ADAPTER == canonical.SOURCE_PROVIDER_ADAPTER == "provider-adapter"

# legacy fork aliases point at the canonical objects themselves
assert facade.KIND is canonical.KIND_SUBMITTED
assert facade.SOURCE is canonical.SOURCE_PROVIDER_ADAPTER

# the named cross-repository timestamp vectors behave identically
assert list(facade.TIMESTAMP_VECTORS) == [
    "same-second-nonzero-micros",
    "offset-to-utc-nonzero-micros",
]
for name, vector in canonical.TIMESTAMP_VECTORS.items():
    assert facade.format_rfc3339_utc(vector["created"]) == vector["wire_created"]
    assert facade.format_rfc3339_utc(vector["submitted"]) == vector["wire_submitted"]
    assert facade.parse_rfc3339_utc(vector["wire_created"]) == vector["created"]
    assert facade.parse_rfc3339_utc(vector["wire_submitted"]) == vector["submitted"]

# the single digest helper and canonical id normalization
assert facade.message_digest(b"ping") == hashlib.sha256(b"ping").hexdigest()
assert facade.message_digest("ping") == hashlib.sha256(b"ping").hexdigest()
assert facade.is_message_digest(facade.message_digest("ping"))
assert not facade.is_message_digest("Q" + "a" * 63)
assert facade.canonical_message_id(7) == "7"
assert facade.canonical_message_id("7") == "7"
assert (
    facade.receipt_endpoint_path("deadbeef", 7)
    == "/terminals/deadbeef/inbox/messages/7/turn-receipt"
)

# strict typed failures carry the canonical error surface
try:
    facade.validate_receipt({{}})
except facade.ReceiptValidationError as exc:
    assert exc.code == "missing-fields"
else:
    raise AssertionError("empty payload must fail strictly")
try:
    facade.build_receipt(
        message_id=7,
        message_sha256="bad",
        message_created_at=datetime(2026, 7, 29, 14, 15, 16, 123456, tzinfo=timezone.utc),
        sender_id="s",
        sender_generation="sg",
        receiver_id="r",
        receiver_generation="rg",
        provider="p",
        provider_session_id="ps",
        provider_turn_id="pt",
        submitted_at=datetime(2026, 7, 29, 14, 15, 17, 123456, tzinfo=timezone.utc),
    )
except facade.ReceiptValidationError as exc:
    assert exc.code == "digest-not-64-lowercase-hex"
    assert exc.field == "message_sha256"
else:
    raise AssertionError("an invalid digest must fail strictly")

# a valid receipt built through the facade round-trips through the facade
receipt = facade.build_receipt(
    message_id=7,
    message_sha256="a" * 64,
    message_created_at=datetime(2026, 7, 29, 14, 15, 16, 123456, tzinfo=timezone.utc),
    sender_id="supervisor",
    sender_generation="supervisor-generation",
    receiver_id="worker",
    receiver_generation="worker-generation",
    provider="codex",
    provider_session_id="session",
    provider_turn_id="turn",
    submitted_at=datetime(2026, 7, 29, 14, 15, 17, 654321, tzinfo=timezone.utc),
)
assert facade.validate_receipt(receipt) == receipt
print("OK")
"""


def _receipt():
    return contract.build_receipt(
        message_id=7,
        message_sha256="a" * 64,
        message_created_at=datetime(2026, 7, 30, 12, 0, 0, 123456, tzinfo=timezone.utc),
        sender_id="supervisor",
        sender_generation="supervisor-generation",
        receiver_id="worker",
        receiver_generation="worker-generation",
        provider="codex",
        provider_session_id="session",
        provider_turn_id="turn",
        submitted_at=datetime(2026, 7, 30, 12, 0, 1, 654321, tzinfo=timezone.utc),
    )


def test_builder_emits_exact_strict_v1_contract():
    receipt = _receipt()
    assert tuple(receipt) == contract.FIELDS
    assert receipt["schema"] == "cao-model-turn-receipt-v1"
    assert receipt["source"] == "provider-adapter"
    assert receipt["message_created_at"].endswith(".123456Z")
    assert receipt["submitted_at"].endswith(".654321Z")


def test_unknown_or_missing_fields_fail_closed():
    extra = {**_receipt(), "extension": "not-v1"}
    with pytest.raises(contract.ReceiptValidationError):
        contract.validate_receipt(extra)
    missing = _receipt()
    missing.pop("sender_generation")
    with pytest.raises(contract.ReceiptValidationError):
        contract.validate_receipt(missing)


def test_contract_source_is_a_static_discriminator():
    assert contract.CONTRACT_SOURCE in {"conductor-sentinel", "standalone"}


def test_standalone_selected_when_the_top_level_package_is_absent():
    _assert_isolated_ok(_STANDALONE_PROBE)


def test_installed_mode_reexports_the_canonical_objects_exactly():
    canonical_root = _pinned_canonical_plugin_dir()
    if canonical_root is None:
        pytest.skip("pinned conductor_sentinel source is not available on this machine")
    _assert_isolated_ok(_installed_mode_probe(canonical_root), extra_paths=(canonical_root,))


def test_missing_contract_submodule_fails_visibly_not_fallback(tmp_path):
    pkg = tmp_path / "conductor_sentinel"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    result = _run_isolated(_MISSING_SUBMODULE_PROBE, extra_paths=(tmp_path,))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_broken_internal_canonical_import_propagates(tmp_path):
    pkg = tmp_path / "conductor_sentinel"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "model_turn_receipt_contract.py").write_text(
        "import definitely_missing_internal_module\n"
    )
    result = _run_isolated(_BROKEN_INTERNAL_IMPORT_PROBE, extra_paths=(tmp_path,))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
