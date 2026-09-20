"""Readiness for a runtime that serves no HTTP (#745).

A bridge-mode pod has no ``/health`` to GET: nothing reaches it inbound, so the
only thing "ready" can mean is that its outbound channel to the central server
is established and the hello was accepted. These tests pin that signal, because
an exec probe in the shipped manifest depends on it and a marker that lied would
route work to a pod that cannot receive it.

The cases that matter are the ones where a socket exists but the runtime does
not work: version mismatch, auth rejection, a dropped connection mid-session,
and a marker left behind by a killed predecessor sharing the same mount.
"""

import asyncio
import os

import pytest
from websockets.exceptions import ConnectionClosedError, InvalidStatus

from cli_agent_orchestrator.runtime_channel import bridge as bridge_mod
from cli_agent_orchestrator.runtime_channel.bridge import (
    DEFAULT_READY_FILE,
    READY_FILE_ENV,
    Bridge,
    clear_channel_ready,
    mark_channel_ready,
    ready_file_path,
)
from cli_agent_orchestrator.runtime_channel.protocol import (
    PROTOCOL_VERSION,
    HelloFrame,
    decode_frame,
    encode_frame,
)


@pytest.fixture()
def ready_file(tmp_path, monkeypatch):
    """A private readiness path, so no test observes another's marker."""
    path = tmp_path / "state" / "bridge-connected"
    monkeypatch.setenv(READY_FILE_ENV, str(path))
    return path


class _FakeWS:
    """A channel that answers hello, optionally fails, then ends the session.

    Ending the iteration is how a real dropped connection looks to ``_serve``;
    ``stop_after`` lets ``run()`` exit its reconnect loop once, instead of
    retrying forever inside a test.
    """

    def __init__(self, server_hello, raise_on_iter=None, stop_after=None, on_iter=None):
        self.sent = []
        self._server_hello = server_hello
        self._raise = raise_on_iter
        self._stop_after = stop_after
        self._on_iter = on_iter

    async def send(self, raw):
        self.sent.append(decode_frame(raw))

    async def recv(self):
        return encode_frame(self._server_hello)

    def __aiter__(self):
        async def gen():
            # Yield to the loop so a probe between connect and drop is possible.
            await asyncio.sleep(0)
            if self._on_iter is not None:
                # Sampled while the session is live, which is the only window in
                # which the marker is supposed to exist.
                self._on_iter()
            if self._stop_after is not None:
                self._stop_after.stop()
            if self._raise is not None:
                raise self._raise
            return
            yield  # pragma: no cover - generator marker

        return gen()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _bridge():
    return Bridge("ws://server/runtime/channel", "worker-1", "tok")


def _good_hello():
    return HelloFrame(protocol_version=PROTOCOL_VERSION, runtime_id="server")


class TestTheMarkerFollowsTheChannel:
    @pytest.mark.asyncio
    async def test_an_established_channel_is_announced(self, ready_file):
        bridge = _bridge()
        await bridge._serve(_FakeWS(_good_hello()))
        assert ready_file.exists()
        # The owning pid, so an operator inspecting the pod can tell the marker
        # apart from a leftover belonging to nothing.
        assert ready_file.read_text().strip() == str(os.getpid())

    @pytest.mark.asyncio
    async def test_a_version_mismatch_is_never_announced_ready(self, ready_file):
        """A socket that opened is not a runtime that works.

        The hello gate rejects here, and readiness must sit behind that gate -
        otherwise the pod would take traffic it cannot serve.
        """
        bridge = _bridge()
        hello = HelloFrame(protocol_version=PROTOCOL_VERSION + 1, runtime_id="server")
        with pytest.raises(ValueError):
            await bridge._serve(_FakeWS(hello))
        assert not ready_file.exists()

    @pytest.mark.asyncio
    async def test_a_dropped_channel_withdraws_readiness(self, ready_file, monkeypatch):
        bridge = _bridge()
        ws = _FakeWS(
            _good_hello(),
            raise_on_iter=ConnectionClosedError(None, None),
            stop_after=bridge,
        )
        monkeypatch.setattr(bridge_mod.websockets, "connect", lambda *a, **k: ws)
        await asyncio.wait_for(bridge.run(), timeout=10)

        # The channel is gone, so the pod must read unready even though the
        # process is alive and will keep reconnecting.
        assert not ready_file.exists()

    @pytest.mark.asyncio
    async def test_a_bridge_that_was_ready_is_unready_after_a_fatal_rejection(
        self, ready_file, monkeypatch
    ):
        """The token is revoked while the pod is running, which is fatal (#776).

        A pod that had been serving must not be left announcing readiness by a
        process that is exiting. The sequence is the real one - established,
        dropped, then refused - because a marker only exists to be withdrawn
        after a channel actually came up.
        """
        bridge = _bridge()
        seen_ready = []

        class _Response:
            status_code = 401

        def _connect(*args, **kwargs):
            if not seen_ready:
                return _FakeWS(
                    _good_hello(),
                    raise_on_iter=ConnectionClosedError(None, None),
                    on_iter=lambda: seen_ready.append(ready_file.exists()),
                )
            raise InvalidStatus(_Response())

        monkeypatch.setattr(bridge_mod.websockets, "connect", _connect)
        monkeypatch.setattr(bridge_mod, "RECONNECT_BACKOFF_INITIAL", 0.01)
        with pytest.raises(InvalidStatus):
            await asyncio.wait_for(bridge.run(), timeout=10)

        assert seen_ready == [True], "the first channel never announced readiness"
        assert not ready_file.exists()

    @pytest.mark.asyncio
    async def test_a_predecessors_marker_is_cleared_at_startup(self, ready_file, monkeypatch):
        """SIGKILL cannot run cleanup, and the state mount can outlive the pod.

        Without this, a restarted bridge would be probed ready on a file written
        by a process that no longer exists, before its own channel came up.
        """
        mark_channel_ready()
        assert ready_file.exists()

        bridge = _bridge()
        observed = []

        def _refuse(*args, **kwargs):
            # Sampled at the first connect attempt: the clear has to happen
            # before any connection, not after the first success.
            observed.append(ready_file.exists())
            bridge.stop()
            raise OSError("no route to host")

        monkeypatch.setattr(bridge_mod.websockets, "connect", _refuse)
        await asyncio.wait_for(bridge.run(), timeout=10)
        assert observed == [False]


class TestMarkerMechanics:
    def test_the_default_path_travels_with_the_state_directory(self):
        from cli_agent_orchestrator.constants import CAO_HOME_DIR

        assert DEFAULT_READY_FILE.parent == CAO_HOME_DIR

    def test_the_path_is_overridable_per_call(self, tmp_path, monkeypatch):
        # Read per call, not bound at import: the manifest sets it in the
        # container env, which is not necessarily set when this module loads.
        monkeypatch.setenv(READY_FILE_ENV, str(tmp_path / "elsewhere"))
        assert ready_file_path() == tmp_path / "elsewhere"
        monkeypatch.delenv(READY_FILE_ENV)
        assert ready_file_path() == DEFAULT_READY_FILE

    def test_clearing_a_marker_that_is_not_there_is_a_noop(self, ready_file):
        clear_channel_ready()  # must not raise

    def test_the_shipped_manifest_probes_the_path_it_configures(self):
        """The manifest and the code cannot drift apart silently.

        The EKS example's supervisor is a bridge pod whose readiness is an exec
        probe on this marker. If the probe path and ``CAO_BRIDGE_READY_FILE``
        disagreed, the pod would never become Ready and the deploy would fail as
        a 900s rollout timeout - a long way from the one-character cause.
        """
        from pathlib import Path

        import yaml

        manifest = (
            Path(__file__).resolve().parents[2]
            / "examples/cao-clusters/kubernetes/eks/supervisor.yaml"
        )
        docs = [d for d in yaml.safe_load_all(manifest.read_text()) if d]
        sts = next(d for d in docs if d["kind"] == "StatefulSet")
        container = sts["spec"]["template"]["spec"]["containers"][0]
        env = {e["name"]: e.get("value") for e in container["env"]}

        configured = env[READY_FILE_ENV]
        assert env["CAO_NODE_MODE"] == "bridge", "this test is about the bridge pod"
        for probe in ("startupProbe", "readinessProbe"):
            assert container[probe]["exec"]["command"] == ["test", "-f", configured]
        # And it must sit inside the pod's own writable state mount, or the
        # bridge's warning path is all the probe would ever see.
        assert configured.startswith(env["CAO_HOME_DIR"] + "/")
        # No liveness probe on the marker: it is absent while the bridge backs
        # off through a server restart, and restarting the pod for that would
        # destroy live terminals to fix a connection that is already retrying.
        assert "livenessProbe" not in container

    def test_an_unwritable_location_warns_instead_of_killing_the_runtime(
        self, tmp_path, monkeypatch, caplog
    ):
        """Reporting is not worth a runtime.

        A read-only mount should make the pod unready, which it does by leaving
        the file absent - it must not take down live terminals.
        """
        blocked = tmp_path / "file-not-a-dir"
        blocked.write_text("")
        monkeypatch.setenv(READY_FILE_ENV, str(blocked / "bridge-connected"))
        with caplog.at_level("WARNING"):
            mark_channel_ready()
        assert "readiness marker" in caplog.text
