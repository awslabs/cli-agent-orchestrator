import pathlib
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from cli_agent_orchestrator.services import model_turn_receipt_contract as contract

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SRC_DIR = _REPO_ROOT / "src"

#: Optional local checkouts of the pinned canonical contract (definition pin
#: ``42af6fb2a4862211fcf1e1289ced44bb75b943c5``). Only the real-module proof
#: below consults these; every other proof is hermetic under ``tmp_path``.
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


_CANONICAL_EXPORT_NAMES = (
    "SCHEMA",
    "KIND_SUBMITTED",
    "SOURCE_PROVIDER_ADAPTER",
    "FIELDS",
    "TIMESTAMP_FIELDS",
    "TIMESTAMP_VECTORS",
    "ReceiptValidationError",
    "message_digest",
    "is_message_digest",
    "canonical_message_id",
    "parse_rfc3339_utc",
    "format_rfc3339_utc",
    "receipt_endpoint_path",
    "validate_receipt",
    "build_receipt",
)

_SYNTHETIC_CANONICAL_MODULE = """
SCHEMA = object()
KIND_SUBMITTED = object()
SOURCE_PROVIDER_ADAPTER = object()
FIELDS = object()
TIMESTAMP_FIELDS = object()
TIMESTAMP_VECTORS = object()
ReceiptValidationError = object()
message_digest = object()
is_message_digest = object()
canonical_message_id = object()
parse_rfc3339_utc = object()
format_rfc3339_utc = object()
receipt_endpoint_path = object()
validate_receipt = object()
build_receipt = object()
"""


def _installed_success_probe(
    names: tuple[str, ...] = _CANONICAL_EXPORT_NAMES,
    *,
    pinned_root: pathlib.Path | None = None,
    extra_asserts: str = "",
) -> str:
    """A fresh-process probe: every export is the canonical object by ``is``,
    and the legacy aliases resolve to the canonical constants."""
    header = ""
    if pinned_root is not None:
        header = f"assert canonical.__file__.startswith({str(pinned_root)!r}), canonical.__file__\n"
    return f"""
import conductor_sentinel.model_turn_receipt_contract as canonical
from cli_agent_orchestrator.services import model_turn_receipt_contract as facade

{header}assert facade.CONTRACT_SOURCE == "conductor-sentinel", facade.CONTRACT_SOURCE
for name in {names!r}:
    assert getattr(facade, name) is getattr(canonical, name), name
{extra_asserts}assert facade.KIND is facade.KIND_SUBMITTED
assert facade.SOURCE is facade.SOURCE_PROVIDER_ADAPTER
print("OK")
"""


#: The pinned real module's exact string facts and named vector keys.
_PINNED_EXTRAS = (
    'assert facade.SCHEMA == "cao-model-turn-receipt-v1"\n'
    'assert facade.KIND_SUBMITTED == "submitted"\n'
    'assert facade.SOURCE_PROVIDER_ADAPTER == "provider-adapter"\n'
    "assert list(facade.TIMESTAMP_VECTORS) == [\n"
    '    "same-second-nonzero-micros",\n'
    '    "offset-to-utc-nonzero-micros",\n'
    "]\n"
)

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


def test_installed_success_is_hermetic_with_a_synthetic_canonical_package(tmp_path):
    pkg = tmp_path / "conductor_sentinel"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "model_turn_receipt_contract.py").write_text(_SYNTHETIC_CANONICAL_MODULE)
    _assert_isolated_ok(_installed_success_probe(), extra_paths=(tmp_path,))


def test_installed_mode_reexports_the_pinned_canonical_objects_exactly():
    canonical_root = _pinned_canonical_plugin_dir()
    if canonical_root is None:
        pytest.skip("pinned conductor_sentinel source is not available on this machine")
    _assert_isolated_ok(
        _installed_success_probe(pinned_root=canonical_root, extra_asserts=_PINNED_EXTRAS),
        extra_paths=(canonical_root,),
    )


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
