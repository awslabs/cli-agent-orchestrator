"""The native TUI launch path: claim, launch, prove, publish — or freeze.

The cases here are organised around the two ways a native launch
corrupts a provider session: starting a second TUI on a session another
controller already holds, and publishing an attachment for a process
that is not the one that was claimed.  Every "freeze" assertion below is
really an assertion that a *later* attempt cannot proceed.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from typing import Any, Mapping, Optional, Sequence

import pytest

from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import native_attachment, native_tui_launch

PROVIDER = "kimi_cli"
SESSION = "sess-native-0001"
TERMINAL = "term-native-0001"
GENERATION = "gen-native-0001"


@pytest.fixture
def pinned_binary(tmp_path: Any) -> tuple[str, str]:
    """A real, executable, digest-known file standing in for the provider.

    The launcher verifies bytes on disk, so a fake path would exercise
    nothing.  Returned as ``(path, sha256)`` because every call site
    needs the pin as well as the path.
    """
    binary = tmp_path / "kimi"
    binary.write_bytes(b"#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    path = os.path.realpath(str(binary))
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    return path, digest


def _intent() -> dict[str, Any]:
    return native_attachment.acquire_intent(
        acquisition_method=native_attachment.ACQUISITION_RESUME,
        acquisition_receipt={"kind": "pinned-resume", "receipt_id": "receipt-abc"},
        admits_only_new_instructions=True,
        replays_task_bytes=False,
    )


class FakePane:
    """A pane transport whose every outcome is chosen by the test.

    Records calls so a test can assert the *absence* of a second
    ``create_pane`` — which is the actual safety property for re-entry,
    and is invisible if you only look at return values.
    """

    def __init__(
        self,
        *,
        observation: Optional[Mapping[str, Any]] = None,
        create_error: Optional[Exception] = None,
        observe_error: Optional[Exception] = None,
        handle: Any = "native-window",
    ) -> None:
        self.observation = observation
        self.create_error = create_error
        self.observe_error = observe_error
        self.handle = handle
        self.created: list[list[str]] = []
        self.observe_calls = 0

    def create_pane(self, *, argv: Sequence[str]) -> str:
        self.created.append(list(argv))
        if self.create_error is not None:
            raise self.create_error
        return self.handle

    def observe(self) -> Optional[Mapping[str, Any]]:
        self.observe_calls += 1
        if self.observe_error is not None:
            raise self.observe_error
        return self.observation


class SequencedPane(FakePane):
    """Return successive observations while preserving one created pane."""

    def __init__(self, observations: Sequence[Mapping[str, Any]]) -> None:
        super().__init__()
        self.observations = list(observations)

    def observe(self) -> Optional[Mapping[str, Any]]:
        self.observe_calls += 1
        index = min(self.observe_calls - 1, len(self.observations) - 1)
        return self.observations[index]


def _canonical_workdir() -> str:
    """A real, existing, canonical directory the launcher will accept.

    Real rather than invented because the launcher stats it, and taken
    through ``realpath`` because on macOS the temporary root is reached
    through a symlink — which is the very shape these tests are here to
    reject when it reaches the launcher unresolved.
    """
    return os.path.realpath(tempfile.gettempdir())


def _observation(
    argv: Sequence[str], *, pid: int = 4321, cwd: Optional[str] = None
) -> dict[str, Any]:
    return {
        "pane_id": "%7",
        "pid": pid,
        "start_marker": "Thu Jul 24 10:00:00 2026",
        "argv": list(argv),
        "cwd": _canonical_workdir() if cwd is None else cwd,
    }


def _expected_argv(path: str) -> list[str]:
    return [path, native_tui_launch.kimi_native_launch.RESUME_OPTION, SESSION]


def _pinned_wrapper(tmp_path: Any, shebang: bytes) -> tuple[str, str]:
    wrapper = tmp_path / "wrapper"
    wrapper.write_bytes(shebang + b"exit 0\n")
    wrapper.chmod(0o755)
    path = os.path.realpath(str(wrapper))
    return path, hashlib.sha256(wrapper.read_bytes()).hexdigest()


def _start(pinned: tuple[str, str], transport: Any, **overrides: Any) -> dict[str, Any]:
    path, digest = pinned
    kwargs: dict[str, Any] = {
        "provider": PROVIDER,
        "native_session_id": SESSION,
        "terminal_id": TERMINAL,
        "generation": GENERATION,
        "execution_mode": em.NATIVE_TUI,
        "intent": _intent(),
        "binary": path,
        "binary_sha256": digest,
        "working_directory": _canonical_workdir(),
        "transport": transport,
    }
    kwargs.update(overrides)
    return native_tui_launch.start(**kwargs)


# --------------------------------------------------------------------------
# The golden path
# --------------------------------------------------------------------------


def test_launch_claims_ownership_before_starting_the_process(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    """Ownership must be durable before a process can hold the session.

    Asserted from inside ``create_pane``: by the time the process is
    about to exist, the store must already name this owner, because the
    window between "process running" and "ownership recorded" is exactly
    where a second launcher would see the session as free.
    """
    path, _ = pinned_binary
    seen: dict[str, Any] = {}

    class ObservingPane(FakePane):
        def create_pane(self, *, argv: Sequence[str]) -> str:
            seen["record"] = native_attachment.get(PROVIDER, SESSION)
            return super().create_pane(argv=argv)

    pane = ObservingPane(observation=_observation(_expected_argv(path)))
    result = _start(pinned_binary, pane)

    assert seen["record"] is not None
    assert seen["record"]["state"] == native_attachment.STARTING
    assert seen["record"]["owner"]["terminal_id"] == TERMINAL
    assert seen["record"]["owner"]["generation"] == GENERATION
    assert seen["record"]["owner"]["execution_mode"] == em.NATIVE_TUI
    assert result["outcome"] == native_tui_launch.OUTCOME_LAUNCHED


def test_launch_publishes_the_observed_process_identity(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    path, digest = pinned_binary
    argv = _expected_argv(path)
    pane = FakePane(observation=_observation(argv, pid=9182))
    result = _start(pinned_binary, pane)

    assert result["schema"] == native_tui_launch.LAUNCH_SCHEMA
    assert result["execution_mode"] == em.NATIVE_TUI
    assert result["argv"] == argv
    assert result["binary_sha256"] == digest
    assert pane.created == [argv]

    stored = native_attachment.get(PROVIDER, SESSION)
    assert stored is not None
    assert stored["state"] == native_attachment.ATTACHED
    assert stored["owner"]["process_identity"]["pid"] == 9182
    assert stored["owner"]["pane_id"] == "%7"


def test_launch_argv_digest_covers_the_exact_argv(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    """The digest must distinguish argvs that differ only in word boundaries.

    A digest built by joining on a space would give ``["a b"]`` and
    ``["a", "b"]`` the same value, which would let a receipt attest to an
    argv that is not the one that ran.
    """
    path, _ = pinned_binary
    argv = _expected_argv(path)
    result = _start(pinned_binary, FakePane(observation=_observation(argv)))
    expected = hashlib.sha256("\x00".join(argv).encode()).hexdigest()
    assert result["launch_argv_sha256"] == expected
    assert result["launch_argv_sha256"] != hashlib.sha256(" ".join(argv).encode()).hexdigest()


def test_relaunching_an_attached_session_never_touches_the_pane(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    path, _ = pinned_binary
    argv = _expected_argv(path)
    _start(pinned_binary, FakePane(observation=_observation(argv)))

    second = FakePane(observation=_observation(argv))
    result = _start(pinned_binary, second)

    assert result["outcome"] == native_tui_launch.OUTCOME_ALREADY_ATTACHED
    assert second.created == []
    assert second.observe_calls == 0


def test_launch_waits_for_the_same_wrapper_process_to_exec_the_inner_binary(
    isolated_memory_db: Any,
    pinned_binary: tuple[str, str],
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    wrapper, _ = pinned_binary
    inner = tmp_path / "inner"
    inner.write_bytes(b"inner")
    inner.chmod(0o755)
    inner_path = os.path.realpath(str(inner))
    pane = SequencedPane(
        [
            _observation(["/usr/bin/python3", wrapper, "--session", SESSION]),
            _observation([inner_path, "--session", SESSION]),
        ]
    )
    monkeypatch.setattr(native_tui_launch.time, "sleep", lambda _: None)

    result = _start(
        pinned_binary,
        pane,
        expected_inner_executable=inner_path,
    )

    assert result["outcome"] == native_tui_launch.OUTCOME_LAUNCHED
    assert result["binary"] == wrapper
    assert result["pane_observation"]["argv"][0] == inner_path
    assert pane.observe_calls == 2
    assert native_attachment.get(PROVIDER, SESSION)["state"] == native_attachment.ATTACHED


def test_launch_waits_for_an_env_shebang_wrapper_to_exec_the_inner_binary(
    isolated_memory_db: Any,
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    pinned = _pinned_wrapper(tmp_path, b"#!/usr/bin/env python3\n")
    wrapper, _ = pinned
    inner = tmp_path / "inner"
    inner.write_bytes(b"inner")
    inner.chmod(0o755)
    inner_path = os.path.realpath(str(inner))
    pane = SequencedPane(
        [
            _observation(
                [
                    native_tui_launch.ENV_EXECUTABLE,
                    "python3",
                    wrapper,
                    "--session",
                    SESSION,
                ]
            ),
            _observation([inner_path, "--session", SESSION]),
        ]
    )
    monkeypatch.setattr(native_tui_launch.time, "sleep", lambda _: None)

    result = _start(pinned, pane, expected_inner_executable=inner_path)

    assert result["outcome"] == native_tui_launch.OUTCOME_LAUNCHED
    assert result["pane_observation"]["argv"][0] == inner_path
    assert pane.observe_calls == 2


def test_env_shebang_transient_preserves_whitespace_bearing_argv(
    isolated_memory_db: Any,
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    pinned = _pinned_wrapper(tmp_path, b"#!/usr/bin/env python3\n")
    wrapper, _ = pinned
    inner = tmp_path / "inner"
    inner.write_bytes(b"inner")
    inner.chmod(0o755)
    inner_path = os.path.realpath(str(inner))
    extra_args = ["--settings", '{"hook": "two words"}']
    launch_tail = [*extra_args, native_tui_launch.kimi_native_launch.RESUME_OPTION, SESSION]
    pane = SequencedPane(
        [
            _observation(
                [
                    native_tui_launch.ENV_EXECUTABLE,
                    "python3",
                    wrapper,
                    *launch_tail,
                ]
            ),
            _observation([inner_path, *launch_tail]),
        ]
    )
    monkeypatch.setattr(native_tui_launch.time, "sleep", lambda _: None)

    result = _start(
        pinned,
        pane,
        expected_inner_executable=inner_path,
        extra_args=extra_args,
    )

    assert result["outcome"] == native_tui_launch.OUTCOME_LAUNCHED
    assert result["pane_observation"]["argv"] == [inner_path, *launch_tail]
    assert pane.observe_calls == 2


def test_interpreter_shebang_transient_preserves_whitespace_bearing_argv(
    isolated_memory_db: Any,
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    pinned = _pinned_wrapper(tmp_path, b"#!/usr/bin/python3\n")
    wrapper, _ = pinned
    inner = tmp_path / "inner"
    inner.write_bytes(b"inner")
    inner.chmod(0o755)
    inner_path = os.path.realpath(str(inner))
    extra_args = ["--settings", '{"hook": "two words"}']
    launch_tail = [*extra_args, native_tui_launch.kimi_native_launch.RESUME_OPTION, SESSION]
    pane = SequencedPane(
        [
            _observation(["/usr/bin/python3", wrapper, *launch_tail]),
            _observation([inner_path, *launch_tail]),
        ]
    )
    monkeypatch.setattr(native_tui_launch.time, "sleep", lambda _: None)

    result = _start(
        pinned,
        pane,
        expected_inner_executable=inner_path,
        extra_args=extra_args,
    )

    assert result["outcome"] == native_tui_launch.OUTCOME_LAUNCHED
    assert result["pane_observation"]["argv"] == [inner_path, *launch_tail]
    assert pane.observe_calls == 2


def test_env_shebang_transient_refuses_a_whitespace_tail_mismatch(
    isolated_memory_db: Any,
    tmp_path: Any,
) -> None:
    pinned = _pinned_wrapper(tmp_path, b"#!/usr/bin/env python3\n")
    wrapper, _ = pinned
    inner = tmp_path / "inner"
    inner.write_bytes(b"inner")
    inner.chmod(0o755)
    extra_args = ["--settings", '{"hook": "two words"}']
    pane = FakePane(
        observation=_observation(
            [
                native_tui_launch.ENV_EXECUTABLE,
                "python3",
                wrapper,
                "--settings",
                '{"hook": "different words"}',
                native_tui_launch.kimi_native_launch.RESUME_OPTION,
                SESSION,
            ]
        )
    )

    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(
            pinned,
            pane,
            expected_inner_executable=os.path.realpath(str(inner)),
            extra_args=extra_args,
        )

    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH
    _assert_frozen(native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH)


def test_env_shebang_transient_accepts_a_wrapper_path_with_spaces(
    isolated_memory_db: Any,
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    wrapper_dir = tmp_path / "wrapper dir"
    wrapper_dir.mkdir()
    pinned = _pinned_wrapper(wrapper_dir, b"#!/usr/bin/env python3\n")
    wrapper, _ = pinned
    inner_dir = tmp_path / "inner dir"
    inner_dir.mkdir()
    inner = inner_dir / "inner"
    inner.write_bytes(b"inner")
    inner.chmod(0o755)
    inner_path = os.path.realpath(str(inner))
    pane = SequencedPane(
        [
            _observation(
                [
                    native_tui_launch.ENV_EXECUTABLE,
                    "python3",
                    wrapper,
                    "--session",
                    SESSION,
                ]
            ),
            _observation([inner_path, "--session", SESSION]),
        ]
    )
    monkeypatch.setattr(native_tui_launch.time, "sleep", lambda _: None)

    result = _start(pinned, pane, expected_inner_executable=inner_path)

    assert result["outcome"] == native_tui_launch.OUTCOME_LAUNCHED
    assert pane.observe_calls == 2


@pytest.mark.parametrize(
    ("shebang", "observed_prefix"),
    [
        (b"#!/usr/bin/env python3\n", [native_tui_launch.ENV_EXECUTABLE, "python3.13"]),
        (b"#!/usr/bin/env -S python3\n", [native_tui_launch.ENV_EXECUTABLE, "python3"]),
        (b"#!/usr/bin/env python3 -u\n", [native_tui_launch.ENV_EXECUTABLE, "python3"]),
        (b"not-a-shebang\n", [native_tui_launch.ENV_EXECUTABLE, "python3"]),
        (
            b"#!" + b"x" * (native_tui_launch.MAX_SHEBANG_LINE_BYTES + 1),
            [native_tui_launch.ENV_EXECUTABLE, "python3"],
        ),
    ],
)
def test_env_shebang_transient_refuses_unpinned_interpreter_forms(
    isolated_memory_db: Any,
    tmp_path: Any,
    shebang: bytes,
    observed_prefix: list[str],
) -> None:
    pinned = _pinned_wrapper(tmp_path, shebang)
    wrapper, _ = pinned
    inner = tmp_path / "inner"
    inner.write_bytes(b"inner")
    inner.chmod(0o755)
    pane = FakePane(observation=_observation([*observed_prefix, wrapper, "--session", SESSION]))

    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(
            pinned,
            pane,
            expected_inner_executable=os.path.realpath(str(inner)),
        )

    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH
    _assert_frozen(native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH)


def test_bare_env_dash_s_is_not_an_interpreter_token(tmp_path: Any) -> None:
    wrapper, _ = _pinned_wrapper(tmp_path, b"#!/usr/bin/env -S\n")
    assert native_tui_launch._env_shebang_interpreter(wrapper) is None


@pytest.mark.parametrize(
    "observed_argv",
    [
        [native_tui_launch.ENV_EXECUTABLE, "python3", "/tmp/not-the-wrapper", "--session", SESSION],
        [
            native_tui_launch.ENV_EXECUTABLE,
            "python3",
            "{wrapper}",
            "--session",
            SESSION,
            "--wrong-tail",
        ],
    ],
)
def test_env_shebang_transient_refuses_wrong_wrapper_or_tail(
    isolated_memory_db: Any,
    tmp_path: Any,
    observed_argv: list[str],
) -> None:
    pinned = _pinned_wrapper(tmp_path, b"#!/usr/bin/env python3\n")
    wrapper, _ = pinned
    inner = tmp_path / "inner"
    inner.write_bytes(b"inner")
    inner.chmod(0o755)
    argv = [wrapper if value == "{wrapper}" else value for value in observed_argv]

    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(
            pinned,
            FakePane(observation=_observation(argv)),
            expected_inner_executable=os.path.realpath(str(inner)),
        )

    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH
    _assert_frozen(native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH)


def test_env_shebang_transient_requires_the_canonical_env_binary(
    isolated_memory_db: Any,
    tmp_path: Any,
) -> None:
    pinned = _pinned_wrapper(tmp_path, b"#!/usr/bin/env python3\n")
    wrapper, _ = pinned
    fake_env = tmp_path / "env"
    fake_env.write_bytes(b"env")
    fake_env.chmod(0o755)
    inner = tmp_path / "inner"
    inner.write_bytes(b"inner")
    inner.chmod(0o755)

    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(
            pinned,
            FakePane(
                observation=_observation(
                    [
                        os.path.realpath(str(fake_env)),
                        "python3",
                        wrapper,
                        "--session",
                        SESSION,
                    ]
                )
            ),
            expected_inner_executable=os.path.realpath(str(inner)),
        )

    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH
    _assert_frozen(native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH)


def test_inner_exec_convergence_refuses_a_replaced_process_identity(
    isolated_memory_db: Any,
    pinned_binary: tuple[str, str],
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    wrapper, _ = pinned_binary
    inner = tmp_path / "inner"
    inner.write_bytes(b"inner")
    inner.chmod(0o755)
    inner_path = os.path.realpath(str(inner))
    pane = SequencedPane(
        [
            _observation(
                ["/usr/bin/python3", wrapper, "--session", SESSION],
                pid=4321,
            ),
            _observation([inner_path, "--session", SESSION], pid=4322),
        ]
    )
    monkeypatch.setattr(native_tui_launch.time, "sleep", lambda _: None)

    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(
            pinned_binary,
            pane,
            expected_inner_executable=inner_path,
        )

    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH
    _assert_frozen(native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH)


def test_inner_exec_convergence_freezes_when_the_wrapper_never_execs(
    isolated_memory_db: Any,
    pinned_binary: tuple[str, str],
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    wrapper, _ = pinned_binary
    inner = tmp_path / "inner"
    inner.write_bytes(b"inner")
    inner.chmod(0o755)
    inner_path = os.path.realpath(str(inner))
    pane = FakePane(observation=_observation(["/usr/bin/python3", wrapper, "--session", SESSION]))
    monkeypatch.setattr(native_tui_launch, "INNER_EXEC_CONVERGENCE_TIMEOUT_SECONDS", 0.0)

    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(
            pinned_binary,
            pane,
            expected_inner_executable=inner_path,
        )

    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH
    assert "did not converge" in caught.value.detail
    _assert_frozen(native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH)


def test_inner_exec_convergence_does_not_wait_for_a_foreign_process(
    isolated_memory_db: Any,
    pinned_binary: tuple[str, str],
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    inner = tmp_path / "inner"
    inner.write_bytes(b"inner")
    inner.chmod(0o755)
    inner_path = os.path.realpath(str(inner))
    pane = FakePane(
        observation=_observation(["/usr/bin/python3", "/tmp/not-the-wrapper", "--session", SESSION])
    )
    slept: list[float] = []
    monkeypatch.setattr(native_tui_launch.time, "sleep", slept.append)

    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(
            pinned_binary,
            pane,
            expected_inner_executable=inner_path,
        )

    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH
    assert slept == []
    _assert_frozen(native_tui_launch.AMBIGUOUS_PROCESS_IMAGE_MISMATCH)


# --------------------------------------------------------------------------
# Mode separation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [em.ACP, "", "NATIVE_TUI", "native", None, 7])
def test_the_native_branch_refuses_every_non_native_mode(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], mode: Any
) -> None:
    pane = FakePane()
    with pytest.raises(native_tui_launch.NativeLaunchInvalid):
        _start(pinned_binary, pane, execution_mode=mode)
    assert pane.created == []
    assert native_attachment.get(PROVIDER, SESSION) is None


# --------------------------------------------------------------------------
# The pinned binary
# --------------------------------------------------------------------------


def test_a_drifted_binary_is_refused_with_nothing_claimed(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    path, _ = pinned_binary
    pane = FakePane(observation=_observation(_expected_argv(path)))
    with pytest.raises(native_tui_launch.NativeLaunchInvalid, match="digest"):
        _start(pinned_binary, pane, binary_sha256="0" * 64)
    assert pane.created == []
    assert native_attachment.get(PROVIDER, SESSION) is None


def test_a_bare_binary_name_is_refused(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    """``kimi`` is not a launch target; it is a question for ``PATH``."""
    _, digest = pinned_binary
    with pytest.raises(native_tui_launch.NativeLaunchInvalid, match="absolute"):
        _start(pinned_binary, FakePane(), binary="kimi", binary_sha256=digest)


def test_a_non_executable_binary_is_refused(isolated_memory_db: Any, tmp_path: Any) -> None:
    target = tmp_path / "not-exec"
    target.write_bytes(b"x")
    target.chmod(0o644)
    path = os.path.realpath(str(target))
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    with pytest.raises(native_tui_launch.NativeLaunchInvalid, match="executable"):
        _start((path, digest), FakePane())


@pytest.mark.parametrize("bad", ["", "abc", "z" * 64, "A" * 63])
def test_a_malformed_digest_pin_is_refused(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], bad: str
) -> None:
    with pytest.raises(native_tui_launch.NativeLaunchInvalid):
        _start(pinned_binary, FakePane(), binary_sha256=bad)


# --------------------------------------------------------------------------
# Freezing: every unresolved outcome
# --------------------------------------------------------------------------


def _assert_frozen(reason: str) -> None:
    stored = native_attachment.get(PROVIDER, SESSION)
    assert stored is not None
    assert stored["state"] == native_attachment.AMBIGUOUS
    assert stored["ambiguity_reason"] == reason


def test_a_raising_pane_create_freezes_rather_than_retries(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    pane = FakePane(create_error=RuntimeError("tmux said no"))
    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(pinned_binary, pane)
    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PANE_CREATE
    _assert_frozen(native_tui_launch.AMBIGUOUS_PANE_CREATE)


def test_a_pane_create_that_returns_no_handle_freezes(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(pinned_binary, FakePane(handle=None))
    assert caught.value.reason == native_tui_launch.AMBIGUOUS_PANE_CREATE
    _assert_frozen(native_tui_launch.AMBIGUOUS_PANE_CREATE)


def test_an_unreadable_pane_and_an_absent_pane_freeze_with_different_reasons(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    """Both freeze, but the recorded reason must tell them apart.

    "We could not look" and "we looked and nothing was there" send a
    later reconciler to different evidence, so collapsing them to one
    reason destroys the only signal it has.
    """
    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous):
        _start(pinned_binary, FakePane(observe_error=OSError("ps unavailable")))
    _assert_frozen(native_tui_launch.AMBIGUOUS_PANE_UNREADABLE)

    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous):
        native_tui_launch.start(
            provider=PROVIDER,
            native_session_id="sess-native-0002",
            terminal_id=TERMINAL,
            generation=GENERATION,
            execution_mode=em.NATIVE_TUI,
            intent=_intent(),
            binary=pinned_binary[0],
            binary_sha256=pinned_binary[1],
            working_directory=_canonical_workdir(),
            transport=FakePane(observation=None),
        )
    absent = native_attachment.get(PROVIDER, "sess-native-0002")
    assert absent is not None
    assert absent["ambiguity_reason"] == native_tui_launch.AMBIGUOUS_PANE_ABSENT_AFTER_CREATE


def test_a_transport_raising_the_module_error_still_freezes(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    """The concrete tmux transport signals "unreadable" with this error.

    It must not travel out un-frozen just because it belongs to this
    module's own exception family — an unreadable pane is unresolved
    however it was reported.
    """
    pane = FakePane(observe_error=native_tui_launch.NativeLaunchUnavailable("pane unreadable"))
    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous):
        _start(pinned_binary, pane)
    _assert_frozen(native_tui_launch.AMBIGUOUS_PANE_UNREADABLE)


@pytest.mark.parametrize(
    "observation",
    [
        {"pid": 1, "start_marker": "m", "argv": ["/x/kimi"], "cwd": "/"},
        {"pane_id": "%1", "start_marker": "m", "argv": ["/x/kimi"], "cwd": "/"},
        {"pane_id": "%1", "pid": 1, "argv": ["/x/kimi"], "cwd": "/"},
        {"pane_id": "%1", "pid": 1, "start_marker": "m", "cwd": "/"},
        {"pane_id": "%1", "pid": 0, "start_marker": "m", "argv": [], "cwd": "/"},
        {"pane_id": "%1", "pid": True, "start_marker": "m", "argv": [], "cwd": "/"},
        {"pane_id": "%1", "pid": 1, "start_marker": "m", "argv": "not-a-list", "cwd": "/"},
        # A pane that cannot say where it is has not been shown to be in
        # the directory its session was minted in, so the observation is
        # incomplete rather than merely unverified.
        {"pane_id": "%1", "pid": 1, "start_marker": "m", "argv": ["/x/kimi"]},
        {"pane_id": "%1", "pid": 1, "start_marker": "m", "argv": ["/x/kimi"], "cwd": ""},
        {"pane_id": "%1", "pid": 1, "start_marker": "m", "argv": ["/x/kimi"], "cwd": 7},
    ],
)
def test_an_incomplete_observation_freezes_rather_than_being_filled_in(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], observation: dict[str, Any]
) -> None:
    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous):
        _start(pinned_binary, FakePane(observation=observation))
    _assert_frozen(native_tui_launch.AMBIGUOUS_PANE_UNREADABLE)


@pytest.mark.parametrize(
    "argv",
    [
        # The picker hazard realised: the resume option lost its argument.
        ["{binary}", "--session"],
        # Resuming a different session entirely.
        ["{binary}", "--session", "sess-native-9999"],
        # A bare interactive start — a brand-new session, not a resume.
        ["{binary}"],
        # The right tokens, not adjacent.
        ["{binary}", "--session", "--verbose", "sess-native-0001"],
        # Two resumes: which one won is not knowable from here.
        ["{binary}", "--session", "sess-native-0001", "--session", "sess-native-9999"],
    ],
)
def test_a_pane_not_resuming_the_bound_session_freezes_and_never_attaches(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], argv: list[str]
) -> None:
    path, _ = pinned_binary
    observed = [token.format(binary=path) for token in argv]
    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(pinned_binary, FakePane(observation=_observation(observed)))
    assert caught.value.reason == native_tui_launch.AMBIGUOUS_ARGV_MISMATCH
    _assert_frozen(native_tui_launch.AMBIGUOUS_ARGV_MISMATCH)


def test_a_frozen_session_refuses_every_later_launch(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    """The point of freezing: the next attempt cannot proceed.

    Not merely that this call raised — that a caller who retries, with a
    healthy transport, is still refused.  Recovery has to go through an
    explicit no-survivor proof.
    """
    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous):
        _start(pinned_binary, FakePane(create_error=RuntimeError("boom")))

    path, _ = pinned_binary
    healthy = FakePane(observation=_observation(_expected_argv(path)))
    with pytest.raises(native_tui_launch.NativeLaunchError):
        _start(pinned_binary, healthy)
    assert healthy.created == []


# --------------------------------------------------------------------------
# Re-entry over a ``starting`` row
# --------------------------------------------------------------------------


def test_reentry_after_a_crash_reconciles_without_relaunching(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    path, _ = pinned_binary
    argv = _expected_argv(path)

    native_attachment.declare(
        provider=PROVIDER,
        native_session_id=SESSION,
        terminal_id=TERMINAL,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
        intent=_intent(),
    )
    native_attachment.mark_starting(
        provider=PROVIDER,
        native_session_id=SESSION,
        terminal_id=TERMINAL,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
    )

    pane = FakePane(observation=_observation(argv))
    result = _start(pinned_binary, pane)

    assert result["outcome"] == native_tui_launch.OUTCOME_RECONCILED
    assert pane.created == [], "re-entry must never start a second TUI"
    assert native_attachment.get(PROVIDER, SESSION)["state"] == native_attachment.ATTACHED


def test_reentry_finding_no_pane_freezes_instead_of_relaunching(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    """An absent pane after ``starting`` is unresolved, not free.

    It cannot distinguish "never started" from "started, ran, and
    exited", and those differ in whether the provider session was
    mutated.  Relaunching would replay onto a session that may already
    have advanced.
    """
    native_attachment.declare(
        provider=PROVIDER,
        native_session_id=SESSION,
        terminal_id=TERMINAL,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
        intent=_intent(),
    )
    native_attachment.mark_starting(
        provider=PROVIDER,
        native_session_id=SESSION,
        terminal_id=TERMINAL,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
    )

    pane = FakePane(observation=None)
    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as caught:
        _start(pinned_binary, pane)
    assert caught.value.reason == native_tui_launch.AMBIGUOUS_START_CROSSED_NO_PANE
    assert pane.created == []
    _assert_frozen(native_tui_launch.AMBIGUOUS_START_CROSSED_NO_PANE)


def test_reentry_over_a_pane_running_another_session_freezes(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    native_attachment.declare(
        provider=PROVIDER,
        native_session_id=SESSION,
        terminal_id=TERMINAL,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
        intent=_intent(),
    )
    native_attachment.mark_starting(
        provider=PROVIDER,
        native_session_id=SESSION,
        terminal_id=TERMINAL,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
    )
    path, _ = pinned_binary
    pane = FakePane(observation=_observation([path, "--session", "sess-other"]))
    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous):
        _start(pinned_binary, pane)
    _assert_frozen(native_tui_launch.AMBIGUOUS_ARGV_MISMATCH)


# --------------------------------------------------------------------------
# Cross-owner exclusion
# --------------------------------------------------------------------------


def test_a_second_generation_cannot_launch_over_a_live_attachment(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    path, _ = pinned_binary
    _start(pinned_binary, FakePane(observation=_observation(_expected_argv(path))))

    intruder = FakePane(observation=_observation(_expected_argv(path)))
    with pytest.raises(native_tui_launch.NativeLaunchConflict):
        _start(pinned_binary, intruder, generation="gen-native-0002")
    assert intruder.created == []

    stored = native_attachment.get(PROVIDER, SESSION)
    assert stored["owner"]["generation"] == GENERATION


def test_a_draining_owner_is_not_relaunched_into(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    path, _ = pinned_binary
    _start(pinned_binary, FakePane(observation=_observation(_expected_argv(path))))
    native_attachment.mark_draining(
        provider=PROVIDER,
        native_session_id=SESSION,
        terminal_id=TERMINAL,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
    )

    pane = FakePane(observation=_observation(_expected_argv(path)))
    with pytest.raises(native_tui_launch.NativeLaunchConflict, match="draining"):
        _start(pinned_binary, pane)
    assert pane.created == []


# --------------------------------------------------------------------------
# The concrete tmux transport
# --------------------------------------------------------------------------


class FakeBackend:
    def __init__(self, *, identity: Any = None, exists: bool = False) -> None:
        self.identity = identity
        self.exists = exists
        self.calls: list[tuple[Any, ...]] = []

    def create_window_with_argv(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        argv: list[str],
        working_directory: Any = None,
        extra_env: Any = None,
    ) -> str:
        self.calls.append((session_name, window_name, terminal_id, tuple(argv)))
        return window_name

    def window_identity(
        self,
        session_name: str,
        window_name: str,
        *,
        deadline_monotonic: Optional[float] = None,
    ) -> Any:
        self.identity_deadline = deadline_monotonic
        return self.identity

    def window_exists(
        self,
        session_name: str,
        window_name: str,
        *,
        deadline_monotonic: Optional[float] = None,
    ) -> bool:
        self.exists_deadline = deadline_monotonic
        return self.exists


def _pane(backend: FakeBackend) -> native_tui_launch.TmuxNativePane:
    return native_tui_launch.TmuxNativePane(
        backend, session_name="cao", window_name="w1", terminal_id=TERMINAL
    )


def test_tmux_transport_execs_the_argv_directly() -> None:
    """No shell, no typed command line — the TUI is the pane's own process."""
    backend = FakeBackend()
    handle = _pane(backend).create_pane(argv=["/bin/kimi", "--session", SESSION])
    assert handle == "w1"
    assert backend.calls == [("cao", "w1", TERMINAL, ("/bin/kimi", "--session", SESSION))]


def test_tmux_transport_reports_a_missing_window_as_absent() -> None:
    assert _pane(FakeBackend(identity=None, exists=False)).observe() is None


def test_tmux_transport_refuses_to_call_an_unreadable_window_absent() -> None:
    """A window that exists but will not report identity is not absence.

    Returning ``None`` here would license the caller to treat a possibly
    live provider process as "nothing there".
    """
    with pytest.raises(native_tui_launch.NativeLaunchUnavailable):
        _pane(FakeBackend(identity=None, exists=True)).observe()


def test_tmux_transport_raises_when_the_pane_pid_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pane = _pane(FakeBackend(identity={"pane_id": "%3"}, exists=True))
    monkeypatch.setattr(pane, "_pane_pid", lambda: None)
    with pytest.raises(native_tui_launch.NativeLaunchUnavailable):
        pane.observe()


def test_tmux_transport_reads_exact_process_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pane = _pane(FakeBackend(identity={"pane_id": "%3"}, exists=True))
    monkeypatch.setattr(pane, "_pane_pid", lambda: 777)
    monkeypatch.setattr(
        native_tui_launch,
        "_process_field",
        lambda pid, field: (
            "Thu Jul 24 10:00:00 2026" if field == "lstart=" else f"/bin/kimi --session {SESSION}"
        ),
    )
    monkeypatch.setattr(
        native_tui_launch,
        "_process_argv",
        lambda pid: ["/bin/kimi", "--settings", '{"hook": "two words"}', "--session", SESSION],
    )
    monkeypatch.setattr(native_tui_launch, "_process_cwd", lambda pid: "/private/tmp/w")
    observed = pane.observe()
    assert observed == {
        "pane_id": "%3",
        "pid": 777,
        "start_marker": "Thu Jul 24 10:00:00 2026",
        "argv": ["/bin/kimi", "--settings", '{"hook": "two words"}', "--session", SESSION],
        "cwd": "/private/tmp/w",
    }


def test_tmux_transport_threads_one_deadline_through_the_whole_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeBackend(identity={"pane_id": "%3"}, exists=True)
    pane = _pane(backend)
    deadline = time.monotonic() + 1.0
    observed_deadlines: list[float] = []

    def _pane_pid(*, deadline_monotonic: float) -> int:
        observed_deadlines.append(deadline_monotonic)
        return 777

    def _process_field(pid: int, field: str, *, deadline_monotonic: float) -> str:
        observed_deadlines.append(deadline_monotonic)
        if field == "lstart=":
            return "Thu Jul 24 10:00:00 2026"
        return f"/bin/kimi --session {SESSION}"

    def _process_cwd(pid: int, *, deadline_monotonic: float) -> str:
        observed_deadlines.append(deadline_monotonic)
        return "/private/tmp/w"

    def _process_argv(pid: int, *, deadline_monotonic: float) -> list[str]:
        observed_deadlines.append(deadline_monotonic)
        return ["/bin/kimi", "--session", SESSION]

    monkeypatch.setattr(pane, "_pane_pid", _pane_pid)
    monkeypatch.setattr(native_tui_launch, "_process_field", _process_field)
    monkeypatch.setattr(native_tui_launch, "_process_argv", _process_argv)
    monkeypatch.setattr(native_tui_launch, "_process_cwd", _process_cwd)

    pane.observe(deadline_monotonic=deadline)

    assert backend.identity_deadline == deadline
    assert observed_deadlines == [deadline, deadline, deadline, deadline, deadline]


def test_tmux_transport_raises_when_the_pane_cwd_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable cwd is an unreadable pane, not a pane that passes.

    Returning the observation without a cwd would let the launch publish
    an attachment having never checked the one thing that distinguishes
    a resumable pane from one that is about to die on the directory the
    session was filed under.
    """
    pane = _pane(FakeBackend(identity={"pane_id": "%3"}, exists=True))
    monkeypatch.setattr(pane, "_pane_pid", lambda: 777)
    monkeypatch.setattr(
        native_tui_launch,
        "_process_field",
        lambda pid, field: (
            "Thu Jul 24 10:00:00 2026" if field == "lstart=" else f"/bin/kimi --session {SESSION}"
        ),
    )
    monkeypatch.setattr(
        native_tui_launch,
        "_process_argv",
        lambda pid: ["/bin/kimi", "--session", SESSION],
    )
    monkeypatch.setattr(native_tui_launch, "_process_cwd", lambda pid: None)
    with pytest.raises(native_tui_launch.NativeLaunchUnavailable):
        pane.observe()


def test_tmux_transport_refuses_an_unreadable_exact_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pane = _pane(FakeBackend(identity={"pane_id": "%3"}, exists=True))
    monkeypatch.setattr(pane, "_pane_pid", lambda: 777)
    monkeypatch.setattr(
        native_tui_launch,
        "_process_field",
        lambda pid, field: (
            "Thu Jul 24 10:00:00 2026" if field == "lstart=" else f"/bin/kimi --session {SESSION}"
        ),
    )
    monkeypatch.setattr(native_tui_launch, "_process_argv", lambda pid: None)

    with pytest.raises(native_tui_launch.NativeLaunchUnavailable):
        pane.observe()


def test_darwin_procargs2_parser_preserves_argument_boundaries() -> None:
    import struct

    argv = ["/usr/bin/env", "python3", "/tmp/wrapper", "alpha", "two words"]
    raw = (
        struct.pack("i", len(argv))
        + b"/usr/bin/env\0"
        + b"\0\0"
        + b"\0".join(os.fsencode(argument) for argument in argv)
        + b"\0"
    )

    assert native_tui_launch._parse_darwin_procargs2(raw) == argv


def test_process_argv_preserves_whitespace_boundaries() -> None:
    import subprocess
    import sys
    import time

    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "two words"])
    try:
        for _ in range(50):
            observed = native_tui_launch._process_argv(process.pid)
            if observed is not None:
                break
            time.sleep(0.05)
        assert observed is not None
        assert observed[-1] == "two words"
    finally:
        process.kill()
        process.wait()
    assert native_tui_launch._process_argv(process.pid) is None


def test_process_cwd_reads_the_real_directory_of_a_live_process() -> None:
    """The cwd probe must report a live process's resolved directory.

    Run against a real process because the whole check rests on the
    kernel disagreeing with the launcher's own record when they diverge;
    a stubbed probe could only ever agree with whatever it was told.
    """
    import subprocess
    import time

    alias = os.path.join(tempfile.gettempdir(), "cao-cwd-probe")
    os.makedirs(alias, exist_ok=True)
    canonical = os.path.realpath(alias)
    process = subprocess.Popen(["/bin/sh", "-c", "sleep 30"], cwd=alias)
    try:
        for _ in range(50):
            observed = native_tui_launch._process_cwd(process.pid)
            if observed is not None:
                break
            time.sleep(0.05)
        # Started through whatever name the caller used; reported as the
        # resolved one -- exactly the asymmetry the launch check exists
        # to catch, and the reason comparing raw strings would not do.
        assert observed == canonical
    finally:
        process.kill()
        process.wait()
    assert native_tui_launch._process_cwd(process.pid) is None


# --------------------------------------------------------------------------
# The directory the bound session was minted in
# --------------------------------------------------------------------------


@pytest.mark.parametrize("suffix", ["", "/nested"])
def test_a_non_canonical_working_directory_claims_nothing_and_starts_nothing(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], tmp_path: Any, suffix: str
) -> None:
    """Refused before ``declare``, so the session is left free.

    Ordering is the whole assertion.  Every other refusal in this module
    that happens after a claim leaves the session frozen and needing an
    explicit human release; this one happens before, so a caller can fix
    the path and simply try again.  Both a symlinked leaf and a symlinked
    interior component are covered, because a check that only compared
    the last component would pass the second.
    """
    real = tmp_path / "real"
    (real / "nested").mkdir(parents=True)
    (tmp_path / "link").symlink_to(real)
    through_link = f"{tmp_path / 'link'}{suffix}"

    pane = FakePane(observation=_observation([]))
    with pytest.raises(native_tui_launch.NativeLaunchInvalid) as refusal:
        _start(pinned_binary, pane, working_directory=through_link)

    assert os.path.realpath(through_link) in str(refusal.value)
    assert pane.created == []
    # Nothing claimed: not attached, not starting, not frozen.
    assert native_attachment.get(PROVIDER, SESSION) is None


def test_a_working_directory_that_does_not_exist_is_refused(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], tmp_path: Any
) -> None:
    """``realpath`` resolves a path that is not there, so existence is checked.

    A canonical-looking path to nothing would otherwise pass every
    string test and fail only when the pane tried to start in it.
    """
    pane = FakePane(observation=_observation([]))
    with pytest.raises(native_tui_launch.NativeLaunchInvalid):
        _start(pinned_binary, pane, working_directory=str(tmp_path / "absent"))
    assert pane.created == []
    assert native_attachment.get(PROVIDER, SESSION) is None


def test_a_pane_running_in_the_wrong_directory_freezes_before_attaching(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], tmp_path: Any
) -> None:
    """A correct argv in the wrong directory is still the wrong pane.

    The process resumes exactly the bound session id, so the argv check
    passes.  It is in a directory that session was never filed under, so
    the provider will refuse to open it and the pane will exit.  Frozen
    rather than published, because publishing would make the generation
    bindable and a task would then be typed at a dying process.
    """
    elsewhere = os.path.realpath(str(tmp_path))
    workdir = os.path.realpath(tempfile.gettempdir())
    assert elsewhere != workdir

    path, _digest = pinned_binary
    pane = FakePane(observation=_observation(_expected_argv(path), cwd=elsewhere))
    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous) as frozen:
        _start(pinned_binary, pane, working_directory=workdir)

    assert frozen.value.reason == native_tui_launch.AMBIGUOUS_PANE_WORKDIR_MISMATCH
    # Both directories named, so a reconciler is not left guessing which
    # of the two moved.
    assert elsewhere in str(frozen.value) and workdir in str(frozen.value)
    _assert_frozen(native_tui_launch.AMBIGUOUS_PANE_WORKDIR_MISMATCH)


def test_a_pane_reporting_the_bound_directory_by_another_name_still_attaches(
    isolated_memory_db: Any, pinned_binary: tuple[str, str]
) -> None:
    """One physical directory under two names is not a mismatch.

    The comparison resolves what the process reports before comparing.
    Refusing here would freeze healthy sessions on any platform whose
    process table happens to answer with an unresolved path.
    """
    workdir = os.path.realpath(tempfile.gettempdir())
    alias = os.path.join(workdir, "..", os.path.basename(workdir))

    path, _digest = pinned_binary
    pane = FakePane(observation=_observation(_expected_argv(path), cwd=alias))
    result = _start(pinned_binary, pane, working_directory=workdir)

    assert result["outcome"] == native_tui_launch.OUTCOME_LAUNCHED
    assert native_attachment.get(PROVIDER, SESSION)["state"] == native_attachment.ATTACHED


def test_reentry_over_a_pane_in_the_wrong_directory_freezes_too(
    isolated_memory_db: Any, pinned_binary: tuple[str, str], tmp_path: Any
) -> None:
    """The reconcile path checks the directory as well.

    Re-entry publishes an attachment for a pane it did not start, which
    makes it the path *most* in need of the check: the directory the pane
    is in was never observed by whoever created it.
    """
    path, _digest = pinned_binary
    workdir = os.path.realpath(tempfile.gettempdir())
    native_attachment.declare(
        provider=PROVIDER,
        native_session_id=SESSION,
        terminal_id=TERMINAL,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
        intent=_intent(),
    )
    native_attachment.mark_starting(
        provider=PROVIDER,
        native_session_id=SESSION,
        terminal_id=TERMINAL,
        generation=GENERATION,
        execution_mode=em.NATIVE_TUI,
    )

    pane = FakePane(
        observation=_observation(_expected_argv(path), cwd=os.path.realpath(str(tmp_path)))
    )
    with pytest.raises(native_tui_launch.NativeLaunchAmbiguous):
        _start(pinned_binary, pane, working_directory=workdir)

    # Observed, never relaunched: the freeze must not be reached by way
    # of starting a second process.
    assert pane.created == []
    _assert_frozen(native_tui_launch.AMBIGUOUS_PANE_WORKDIR_MISMATCH)
