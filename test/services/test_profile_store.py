"""Tests for the local agent-store persistence service (issue #510, phase 3).

``profile_store`` is the single owner of the ``LOCAL_AGENT_STORE_DIR``
boundary. These tests pin the three properties its callers rely on:

* **Name validation** happens before any path join, so a traversal-shaped or
  separator-bearing name can never reach the filesystem.
* **Writes are atomic** and leave no temp debris, and refuse to clobber unless
  the caller opts in.
* **Deletion** is containment-checked and reports a missing profile distinctly
  from an invalid name.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from cli_agent_orchestrator.services import profile_store
from cli_agent_orchestrator.services.profile_store import (
    InvalidProfileNameError,
    ProfileExistsError,
    ProfileNotFoundError,
    delete_profile,
    store_path,
    write_profile,
)


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the store at a tmp dir that does NOT pre-exist.

    Deliberately not created here: ``write_profile`` must make the parent
    itself, which is what lets a first-ever install work on a clean machine.
    """
    target = tmp_path / "agent-store"
    monkeypatch.setattr(profile_store, "LOCAL_AGENT_STORE_DIR", target)
    return target


# --------------------------------------------------------------------------
# store_path
# --------------------------------------------------------------------------


def test_store_path_joins_name_under_the_store(store: Path) -> None:
    assert store_path("my-agent") == (store / "my-agent.md").resolve()


def test_store_path_does_not_require_existence(store: Path) -> None:
    """store_path is pure resolution: it answers "where would this live", not
    "is it there". Deliberately no existence helper sits beside it -- an
    existence check that gates a write has to happen under the write's lock,
    which is why write_profile(overwrite=False) owns that question instead."""
    resolved = store_path("absent")
    assert not resolved.exists()
    assert resolved.name == "absent.md"


@pytest.mark.parametrize(
    "bad_name",
    [
        "../../etc/passwd",
        "..",
        ".",
        "a/b",
        "a\\b",
        "/absolute",
        "",
        "has space",
        "has.dot",
        "x" * 65,
    ],
)
def test_store_path_rejects_unsafe_names(store: Path, bad_name: str) -> None:
    with pytest.raises(InvalidProfileNameError):
        store_path(bad_name)


def test_store_path_accepts_the_maximum_length(store: Path) -> None:
    """64 chars is the documented cap, so it must be inclusive."""
    name = "x" * 64
    assert store_path(name).name == f"{name}.md"


# --------------------------------------------------------------------------
# write_profile
# --------------------------------------------------------------------------


def test_write_profile_creates_the_store_and_the_file(store: Path) -> None:
    assert not store.exists()
    written = write_profile("alpha", "---\nname: alpha\n---\nbody\n")
    assert written == (store / "alpha.md").resolve()
    assert written.read_text(encoding="utf-8") == "---\nname: alpha\n---\nbody\n"


def test_write_profile_refuses_to_clobber_by_default(store: Path) -> None:
    write_profile("beta", "original")
    with pytest.raises(ProfileExistsError):
        write_profile("beta", "replacement")
    assert (store / "beta.md").read_text(encoding="utf-8") == "original"


def test_write_profile_replaces_when_overwrite_is_requested(store: Path) -> None:
    write_profile("beta", "original")
    write_profile("beta", "replacement", overwrite=True)
    assert (store / "beta.md").read_text(encoding="utf-8") == "replacement"


def test_write_profile_rejects_an_unsafe_name_before_touching_disk(store: Path) -> None:
    with pytest.raises(InvalidProfileNameError):
        write_profile("../escape", "payload")
    # The guard must fire before the store is even created, so a rejected write
    # leaves no trace at all.
    assert not store.exists()


def test_write_profile_can_replace_a_corrupt_store_file(store: Path) -> None:
    """Regression: a non-UTF-8 file in the store must not be unrepairable.

    ``write_profile`` deliberately uses ``locked_atomic_write`` rather than
    ``locked_atomic_rewrite``. The read-modify-write helper decodes the existing
    file first, so a truncated download or a binary accidentally named ``.md``
    would raise ``UnicodeDecodeError`` and block the very re-install that would
    have replaced it.
    """
    store.mkdir(parents=True, exist_ok=True)
    corrupt = store / "corrupt.md"
    corrupt.write_bytes(b"\xff\xfe not utf-8 \x80")

    write_profile("corrupt", "---\nname: corrupt\n---\nrepaired\n", overwrite=True)

    assert corrupt.read_text(encoding="utf-8") == "---\nname: corrupt\n---\nrepaired\n"


def test_write_profile_leaves_no_temp_debris(store: Path) -> None:
    """The helper writes via mkstemp + os.replace; a leaked temp file would
    show up in `cao profile list` as a bogus entry."""
    write_profile("gamma", "content")
    assert [p.name for p in store.iterdir()] == ["gamma.md"]


def test_write_profile_serializes_concurrent_writers(store: Path) -> None:
    """Two threads writing the same profile must produce one of the two exact
    payloads, never a mix. A full-replace write has no lost-update semantics to
    protect, but a torn file would still be observable without the lock.
    """
    write_profile("delta", "seed")
    payload_a = "A" * 4096
    payload_b = "B" * 4096
    errors: list[BaseException] = []

    def writer(content: str) -> None:
        try:
            write_profile("delta", content, overwrite=True)
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    t1 = threading.Thread(target=writer, args=(payload_a,))
    t2 = threading.Thread(target=writer, args=(payload_b,))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert errors == [], f"writers must not raise: {errors}"
    final = (store / "delta.md").read_text(encoding="utf-8")
    assert final in (payload_a, payload_b), "file was torn between the two writers"


def test_write_profile_lets_exactly_one_concurrent_creator_win(store: Path) -> None:
    """overwrite=False must serialize, not just usually work.

    Regression test: the guard originally lived here as a ``target.exists()``
    check before the lock was taken, so two threads both saw an absent file and
    both wrote, the second silently clobbering the first. The check now runs
    inside ``locked_atomic_write``'s critical section. Phase 4's
    ``POST /agents/profiles`` depends on this to answer 409 rather than
    overwrite a profile someone else just created.
    """
    arrived = threading.Barrier(2, timeout=5)
    won: list[str] = []
    rejected: list[str] = []
    unexpected: list[BaseException] = []

    def creator(tag: str) -> None:
        arrived.wait()
        try:
            write_profile("contended", tag, overwrite=False)
            won.append(tag)
        except ProfileExistsError:
            rejected.append(tag)
        except BaseException as exc:  # pragma: no cover - failure path
            unexpected.append(exc)

    t1 = threading.Thread(target=creator, args=("FIRST",))
    t2 = threading.Thread(target=creator, args=("SECOND",))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert unexpected == [], f"unexpected failures: {unexpected}"
    assert len(won) == 1, f"exactly one creator must win, got {won}"
    assert len(rejected) == 1, f"the loser must see ProfileExistsError, got {rejected}"
    assert (store / "contended.md").read_text(encoding="utf-8") == won[0]


# --------------------------------------------------------------------------
# delete_profile
# --------------------------------------------------------------------------


def test_delete_profile_removes_the_file(store: Path) -> None:
    write_profile("doomed", "bye")
    delete_profile("doomed")
    assert not (store / "doomed.md").exists()


def test_delete_profile_reports_a_missing_profile(store: Path) -> None:
    with pytest.raises(ProfileNotFoundError):
        delete_profile("never-existed")


def test_delete_profile_rejects_an_unsafe_name(store: Path) -> None:
    """Distinct from ProfileNotFoundError: the caller surfaces these as
    different messages ('Invalid profile name' vs 'not found')."""
    with pytest.raises(InvalidProfileNameError):
        delete_profile("../../etc/passwd")


def test_delete_profile_does_not_follow_a_name_out_of_the_store(
    store: Path, tmp_path: Path
) -> None:
    """Belt-and-braces: even a name that somehow passed the regex must not be
    able to unlink a file outside the store root."""
    outsider = tmp_path / "outsider.md"
    outsider.write_text("do not delete me", encoding="utf-8")
    store.mkdir(parents=True, exist_ok=True)
    with pytest.raises((InvalidProfileNameError, ProfileNotFoundError)):
        delete_profile("../outsider")
    assert outsider.exists()
