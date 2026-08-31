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


# --------------------------------------------------------------------------
# replace_profile
# --------------------------------------------------------------------------


def test_replace_profile_updates_an_existing_profile(store: Path) -> None:
    profile_store.write_profile("agent", "original\n")

    written = profile_store.replace_profile("agent", "updated\n")

    assert written.read_text(encoding="utf-8") == "updated\n"


def test_replace_profile_refuses_to_create_a_missing_profile(store: Path) -> None:
    """The whole point of the function: update-only, never insert.

    ``write_profile(..., overwrite=True)`` is an upsert, which is wrong for an
    HTTP PUT. Requiring the target to exist is what stops a PUT from creating a
    file at all.
    """
    with pytest.raises(profile_store.ProfileNotFoundError):
        profile_store.replace_profile("never-installed", "content\n")

    assert not (store / "never-installed.md").exists()


def test_replace_profile_will_not_shadow_a_built_in(store: Path) -> None:
    """A built-in's name is not in the local store, so PUT must reject it.

    ``code_supervisor`` ships in ``cli_agent_orchestrator/agent_store``. An upsert
    would create a *local* file of the same name that wins on load, silently
    shadowing the built-in. That is precisely the condition ``duplicated_in``
    exists to report, so it must not be manufacturable through the write path.
    """
    with pytest.raises(profile_store.ProfileNotFoundError):
        profile_store.replace_profile("code_supervisor", "hijacked\n")

    assert not (store / "code_supervisor.md").exists()


def test_replace_profile_rejects_an_unsafe_name_before_touching_disk(store: Path) -> None:
    with pytest.raises(profile_store.InvalidProfileNameError):
        profile_store.replace_profile("../escape", "content\n")

    assert not store.exists()


def test_replace_profile_can_replace_a_corrupt_store_file(store: Path) -> None:
    """Undecodable bytes must not make an existing profile unrepairable.

    Same property ``write_profile`` has, for the same reason: the write path must
    not read the old content first.
    """
    store.mkdir(parents=True, exist_ok=True)
    target = store / "agent.md"
    target.write_bytes(b"\xff\xfe not utf-8 at all")

    profile_store.replace_profile("agent", "clean\n")

    assert target.read_text(encoding="utf-8") == "clean\n"


def test_replace_profile_refuses_every_concurrent_writer_when_the_target_is_absent(
    store: Path,
) -> None:
    """The existence requirement holds under contention, not just serially.

    Two threads race to replace the same profile after it is deleted. Neither may
    succeed by creating the file, because the check lives inside the lock.

    Note what this does NOT cover: the file is removed *before* the barrier, so
    both racers are writers and no delete overlaps a write. The interleaving that
    actually threatened the update-only guarantee is covered by
    ``test_delete_profile_cannot_unlink_while_a_replace_holds_the_lock`` below.
    """
    import threading

    profile_store.write_profile("agent", "original\n")
    profile_store.delete_profile("agent")

    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt(label: str) -> None:
        barrier.wait()
        try:
            profile_store.replace_profile("agent", f"{label}\n")
            with lock:
                outcomes.append(f"created:{label}")
        except profile_store.ProfileNotFoundError:
            with lock:
                outcomes.append(f"rejected:{label}")

    threads = [threading.Thread(target=attempt, args=(name,)) for name in ("FIRST", "SECOND")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["rejected:FIRST", "rejected:SECOND"]
    assert not (store / "agent.md").exists()


def test_delete_profile_cannot_unlink_while_a_replace_holds_the_lock(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent delete cannot resurrect a profile through an in-flight update.

    Reported on PR #585. ``delete_profile`` used to do an unlocked ``exists()``
    then ``unlink()``, so this interleaving was reachable:

      1. ``replace_profile`` takes the lock and passes its ``must_exist`` check
      2. ``delete_profile`` unlinks the file and reports success
      3. ``replace_profile`` publishes, recreating what was just deleted

    Both callers were told they succeeded and the "deleted" profile was back on
    disk holding the replacement text. Pausing inside the publish makes the
    window deterministic rather than hoping the scheduler lands in it: the
    deleter must still be blocked on the lock while the replace holds it.
    """
    from cli_agent_orchestrator.utils import atomic_file

    profile_store.write_profile("agent", "original\n")
    target = store / "agent.md"

    publish_entered = threading.Event()
    release = threading.Event()
    real_publish = atomic_file._atomic_publish

    def paused_publish(t: Path, content: str, encoding: str) -> None:
        publish_entered.set()
        release.wait(timeout=10)
        return real_publish(t, content, encoding)

    monkeypatch.setattr(atomic_file, "_atomic_publish", paused_publish)

    delete_outcome: list[str] = []

    def deleter() -> None:
        try:
            profile_store.delete_profile("agent")
            delete_outcome.append("deleted")
        except Exception as exc:  # noqa: BLE001 - recording the class is the point
            delete_outcome.append(type(exc).__name__)

    replacer = threading.Thread(target=lambda: profile_store.replace_profile("agent", "REPLACED\n"))
    replacer.start()
    assert publish_entered.wait(timeout=10), "replace never reached the publish step"

    deleter_thread = threading.Thread(target=deleter)
    deleter_thread.start()
    deleter_thread.join(timeout=0.5)

    # The assertion that fails on the unlocked implementation: the delete would
    # have completed here, having unlinked a file the replace is about to
    # republish.
    assert deleter_thread.is_alive(), "delete_profile did not wait for the write lock"
    assert target.exists()

    release.set()
    replacer.join(timeout=10)
    deleter_thread.join(timeout=15)

    # Serialised, so the delete lands after the update rather than inside it and
    # the file is genuinely gone.
    assert delete_outcome == ["deleted"]
    assert not target.exists()
