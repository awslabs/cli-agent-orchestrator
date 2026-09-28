"""Process-level races for workflow spec create and update publication."""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import queue
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from cli_agent_orchestrator.services import workflow_spec_service as svc
from cli_agent_orchestrator.utils import atomic_file

BASE = "INPUTS = {}\nVALUE = 'base'\n"
UNICODE_PARENT_ALIASES = [
    pytest.param("ünicode", id="case"),
    pytest.param("U\u0308nicode", id="normalization"),
]


def _large_source(label: str) -> str:
    return f"INPUTS = {{}}\nVALUE = {label!r}\n# {label}" + ("x" * 180_000) + "\n"


def _create_worker(
    scan_dir: str,
    label: str,
    start: Any,
    results: Any,
) -> None:
    from cli_agent_orchestrator.services import workflow_spec_service

    start.wait(timeout=10)
    try:
        spec = workflow_spec_service.create_workflow(
            "contended", _large_source(label), scan_dir=scan_dir
        )
        results.put(("ok", label, spec.content_hash))
    except BaseException as exc:  # noqa: BLE001 - child result must reach parent
        results.put(("error", label, type(exc).__name__))


def _update_worker(
    scan_dir: str,
    label: str,
    expected_hash: str,
    start: Any,
    results: Any,
) -> None:
    from cli_agent_orchestrator.services import workflow_spec_service

    start.wait(timeout=10)
    try:
        spec = workflow_spec_service.update_workflow(
            "contended",
            _large_source(label),
            expected_hash,
            scan_dir=scan_dir,
        )
        results.put(("ok", label, spec.content_hash))
    except BaseException as exc:  # noqa: BLE001 - child result must reach parent
        results.put(("error", label, type(exc).__name__, getattr(exc, "actual", None)))


def _named_update_worker(
    scan_dir: str,
    name: str,
    label: str,
    expected_hash: str,
    start: Any,
    results: Any,
) -> None:
    from cli_agent_orchestrator.services import workflow_spec_service

    start.wait(timeout=10)
    try:
        spec = workflow_spec_service.update_workflow(
            name,
            _large_source(label),
            expected_hash,
            scan_dir=scan_dir,
        )
        results.put(("ok", label, spec.content_hash))
    except BaseException as exc:  # noqa: BLE001 - child result must reach parent
        results.put(("error", label, type(exc).__name__, getattr(exc, "actual", None)))


def _run_pair(target: Any, args: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Barrier(3)
    results = ctx.Queue()
    processes = [
        ctx.Process(target=target, args=(*worker_args, start, results)) for worker_args in args
    ]
    for process in processes:
        process.start()
    start.wait(timeout=10)
    for process in processes:
        process.join(timeout=20)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
            pytest.fail("workflow writer did not finish within the bounded join")
        assert process.exitcode == 0

    received: list[tuple[Any, ...]] = []
    for _ in processes:
        try:
            received.append(results.get(timeout=5))
        except queue.Empty:
            pytest.fail("workflow writer exited without returning a result")
    return received


def test_two_process_creates_have_exactly_one_winner(tmp_path: Path) -> None:
    outcomes = _run_pair(
        _create_worker,
        [(str(tmp_path), "alpha"), (str(tmp_path), "bravo")],
    )

    winners = [outcome for outcome in outcomes if outcome[0] == "ok"]
    losers = [outcome for outcome in outcomes if outcome[0] == "error"]
    assert len(winners) == 1, outcomes
    assert len(losers) == 1, outcomes
    assert losers[0][2] == "FileExistsError"

    final = (tmp_path / "contended.py").read_bytes()
    winner_label = winners[0][1]
    assert final == _large_source(winner_label).encode("utf-8")
    assert winners[0][2] == hashlib.sha256(final).hexdigest()


def test_two_same_hash_process_updates_have_exactly_one_winner(tmp_path: Path) -> None:
    created = svc.create_workflow("contended", BASE, scan_dir=str(tmp_path))

    outcomes = _run_pair(
        _update_worker,
        [
            (str(tmp_path), "alpha", created.content_hash),
            (str(tmp_path), "bravo", created.content_hash),
        ],
    )

    winners = [outcome for outcome in outcomes if outcome[0] == "ok"]
    losers = [outcome for outcome in outcomes if outcome[0] == "error"]
    assert len(winners) == 1, outcomes
    assert len(losers) == 1, outcomes
    assert losers[0][2] == "StaleSpecError"

    final = (tmp_path / "contended.py").read_bytes()
    winner_label = winners[0][1]
    assert final == _large_source(winner_label).encode("utf-8")
    assert winners[0][2] == hashlib.sha256(final).hexdigest()
    assert losers[0][3] == winners[0][2]


def test_case_variants_select_same_workflow_lock_identity(tmp_path: Path) -> None:
    safe_base = os.path.realpath(tmp_path)
    mixed_case = svc._workflow_lock_identity(
        svc._safe_spec_path(tmp_path / "MixedCase.py", safe_base)
    )
    lower_case = svc._workflow_lock_identity(
        svc._safe_spec_path(tmp_path / "mixedcase.py", safe_base)
    )

    assert mixed_case == lower_case
    assert atomic_file._lock_path_for_identity(mixed_case) == atomic_file._lock_path_for_identity(
        lower_case
    )


def test_case_alias_same_hash_updates_have_exactly_one_winner(tmp_path: Path) -> None:
    created = svc.create_workflow("MixedCase", BASE, scan_dir=str(tmp_path))
    mixed_case = tmp_path / "MixedCase.py"
    lower_case = tmp_path / "mixedcase.py"
    try:
        case_aliases_same_file = os.path.samefile(mixed_case, lower_case)
    except FileNotFoundError:
        case_aliases_same_file = False
    if not case_aliases_same_file:
        pytest.skip("temporary filesystem is case-sensitive")

    outcomes = _run_pair(
        _named_update_worker,
        [
            (str(tmp_path), "MixedCase", "alpha", created.content_hash),
            (str(tmp_path), "mixedcase", "bravo", created.content_hash),
        ],
    )

    winners = [outcome for outcome in outcomes if outcome[0] == "ok"]
    losers = [outcome for outcome in outcomes if outcome[0] == "error"]
    assert len(winners) == 1, outcomes
    assert len(losers) == 1, outcomes
    assert losers[0][2] == "StaleSpecError"

    final = mixed_case.read_bytes()
    winner_label = winners[0][1]
    assert final == _large_source(winner_label).encode("utf-8")
    assert winners[0][2] == hashlib.sha256(final).hexdigest()
    assert losers[0][3] == winners[0][2]


def _unicode_parent_aliases(tmp_path: Path, alias_name: str) -> tuple[Path, Path]:
    canonical_parent = tmp_path / "Ünicode"
    canonical_parent.mkdir()
    alias_parent = tmp_path / alias_name
    try:
        aliases_same_parent = os.path.samefile(canonical_parent, alias_parent)
    except FileNotFoundError:
        aliases_same_parent = False
    if not aliases_same_parent:
        pytest.skip(f"temporary filesystem does not support {alias_name!r} alias")
    return canonical_parent, alias_parent


@pytest.mark.parametrize("alias_name", UNICODE_PARENT_ALIASES)
def test_unicode_parent_aliases_select_same_workflow_lock_identity(
    tmp_path: Path, alias_name: str
) -> None:
    canonical_parent, alias_parent = _unicode_parent_aliases(tmp_path, alias_name)
    canonical_target = svc._safe_spec_path(canonical_parent / "workflow.py", str(canonical_parent))
    alias_target = svc._safe_spec_path(alias_parent / "workflow.py", str(alias_parent))

    assert svc._workflow_lock_identity(canonical_target) == svc._workflow_lock_identity(
        alias_target
    )


@pytest.mark.parametrize("alias_name", UNICODE_PARENT_ALIASES)
def test_unicode_parent_alias_same_hash_updates_have_exactly_one_winner(
    tmp_path: Path, alias_name: str
) -> None:
    canonical_parent, alias_parent = _unicode_parent_aliases(tmp_path, alias_name)
    created = svc.create_workflow("contended", BASE, scan_dir=str(canonical_parent))

    outcomes = _run_pair(
        _update_worker,
        [
            (str(canonical_parent), "alpha", created.content_hash),
            (str(alias_parent), "bravo", created.content_hash),
        ],
    )

    winners = [outcome for outcome in outcomes if outcome[0] == "ok"]
    losers = [outcome for outcome in outcomes if outcome[0] == "error"]
    assert len(winners) == 1, outcomes
    assert len(losers) == 1, outcomes
    assert losers[0][2] == "StaleSpecError"

    final = (canonical_parent / "contended.py").read_bytes()
    winner_label = winners[0][1]
    assert final == _large_source(winner_label).encode("utf-8")
    assert winners[0][2] == hashlib.sha256(final).hexdigest()
    assert losers[0][3] == winners[0][2]


def test_distinct_physical_parents_select_distinct_workflow_lock_identities(
    tmp_path: Path,
) -> None:
    first_parent = tmp_path / "first"
    second_parent = tmp_path / "second"
    first_parent.mkdir()
    second_parent.mkdir()

    first_target = svc._safe_spec_path(first_parent / "workflow.py", str(first_parent))
    second_target = svc._safe_spec_path(second_parent / "workflow.py", str(second_parent))

    assert svc._workflow_lock_identity(first_target) != svc._workflow_lock_identity(second_target)


def test_workflow_lock_identity_refuses_unavailable_parent_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = svc._safe_spec_path(tmp_path / "workflow.py", str(tmp_path))
    parent = os.path.dirname(target)
    real_stat = os.stat

    def _refuse_target_parent(
        path: os.PathLike[str] | str, *args: Any, **kwargs: Any
    ) -> os.stat_result:
        if os.fspath(path) == parent:
            raise PermissionError(f"refused {path}")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(svc.os, "stat", _refuse_target_parent)

    with pytest.raises(
        atomic_file.LockUnavailableError,
        match="cannot identify workflow lock parent",
    ):
        svc._workflow_lock_identity(target)


def test_concurrent_reader_observes_only_complete_old_or_new_bytes(tmp_path: Path) -> None:
    created = svc.create_workflow("contended", BASE, scan_dir=str(tmp_path))
    new_source = _large_source("reader-safe")
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Barrier(2)
    results = ctx.Queue()
    process = ctx.Process(
        target=_update_worker,
        args=(str(tmp_path), "reader-safe", created.content_hash, start, results),
    )
    process.start()
    start.wait(timeout=10)

    target = tmp_path / "contended.py"
    allowed = {BASE.encode("utf-8"), new_source.encode("utf-8")}
    observed: set[bytes] = set()
    deadline = time.monotonic() + 20
    while process.is_alive() and time.monotonic() < deadline:
        observed.add(target.read_bytes())
    process.join(timeout=5)

    assert process.exitcode == 0
    assert results.get(timeout=5)[0] == "ok"
    observed.add(target.read_bytes())
    assert observed <= allowed
    assert target.read_bytes() == new_source.encode("utf-8")


def test_failed_create_contender_releases_lock_for_next_writer(tmp_path: Path) -> None:
    svc.create_workflow("contended", BASE, scan_dir=str(tmp_path))

    with pytest.raises(FileExistsError):
        svc.create_workflow("contended", _large_source("loser"), scan_dir=str(tmp_path))

    (tmp_path / "contended.py").unlink()
    winner = svc.create_workflow("contended", _large_source("next"), scan_dir=str(tmp_path))
    assert winner.source == _large_source("next")


def test_service_lock_timeout_preserves_target_and_releases_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "contended.py"
    target.write_text(BASE, encoding="utf-8")
    holder_ready = threading.Event()
    release_holder = threading.Event()
    lock_identity = svc._workflow_lock_identity(svc._safe_spec_path(target, str(tmp_path)))

    def _hold() -> None:
        with atomic_file.strict_target_lock(target, lock_identity=lock_identity):
            holder_ready.set()
            release_holder.wait(timeout=5)

    holder = threading.Thread(target=_hold)
    holder.start()
    assert holder_ready.wait(timeout=5)

    real_strict_lock = atomic_file.strict_target_lock

    @contextmanager
    def _short_lock(path: Path, *, lock_identity: str | None = None):
        with real_strict_lock(path, lock_timeout=0.1, lock_identity=lock_identity):
            yield

    monkeypatch.setattr(svc, "strict_target_lock", _short_lock)
    try:
        with pytest.raises(atomic_file.LockTimeoutError):
            svc.update_workflow(
                "contended",
                _large_source("never"),
                hashlib.sha256(BASE.encode("utf-8")).hexdigest(),
                scan_dir=str(tmp_path),
            )
        assert target.read_text(encoding="utf-8") == BASE
    finally:
        release_holder.set()
        holder.join(timeout=5)

    assert not holder.is_alive()
    with real_strict_lock(target, lock_timeout=0.5, lock_identity=lock_identity):
        pass


def test_service_refuses_unsupported_lock_without_touching_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "contended.py"
    target.write_text(BASE, encoding="utf-8")
    monkeypatch.setattr(atomic_file, "_FCNTL_AVAILABLE", False)

    with pytest.raises(atomic_file.LockUnavailableError):
        svc.update_workflow(
            "contended",
            _large_source("never"),
            hashlib.sha256(BASE.encode("utf-8")).hexdigest(),
            scan_dir=str(tmp_path),
        )

    assert target.read_text(encoding="utf-8") == BASE
    assert sorted(path.name for path in tmp_path.iterdir()) == ["contended.py"]
