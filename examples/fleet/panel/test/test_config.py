import json

import pytest

from app import config


# ── The Kubernetes ConfigMap source ───────────────────────────────────────────
# Why it exists, restated because it is the whole justification for the code
# under test: the broker rewrites this registry as elastic workers take and
# release leases, and a mounted ConfigMap only refreshes on the kubelet's sync
# period — so a worker can be created, used and released inside the window in
# which a mounted file still says it does not exist. Reading the API server
# removes that window. Everything below either pins that behaviour or pins the
# fallbacks that keep the panel useful when the read is denied.

_CM = {
    "port": 9889,
    "machines": [
        {"name": "supervisor", "host": "cao-supervisor.svc", "label": "Supervisor",
         "role": "supervisor"},
        {"name": "w-7", "host": "cao-w-7.svc", "label": "worker", "role": "worker",
         "port": 9999},
    ],
}


class _Resp:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    """Records the URL and headers so the request itself can be asserted on."""

    def __init__(self, body=None, status=200, raises=None):
        self._body = body if body is not None else {"data": {"fleet.json": json.dumps(_CM)}}
        self._status = status
        self._raises = raises
        self.calls = []

    async def get(self, url, headers=None):
        self.calls.append((url, headers or {}))
        if self._raises:
            raise self._raises
        return _Resp(self._body, self._status)


@pytest.fixture
def in_cluster(monkeypatch, tmp_path):
    """Make the module look like it is running in a pod, with a rotatable token."""
    token = tmp_path / "token"
    token.write_text("tok-1")
    monkeypatch.setattr(config, "_SA_TOKEN", str(token))
    monkeypatch.setattr(config, "FLEET_CONFIGMAP", "cao-fleet-config")
    monkeypatch.setattr(config, "FLEET_NAMESPACE", "cao-cluster")
    return token


async def test_reads_the_configmap_and_resolves_ports(in_cluster):
    c = _FakeClient()
    machines = await config.read_configmap(c)
    assert [m["name"] for m in machines] == ["supervisor", "w-7"]
    # The top-level default applies where a node omits one; an explicit port wins.
    assert machines[0]["port"] == 9889
    assert machines[1]["port"] == 9999


async def test_requests_the_namespaced_object_with_the_pod_token(in_cluster):
    c = _FakeClient()
    await config.read_configmap(c)
    url, headers = c.calls[0]
    assert url.endswith("/api/v1/namespaces/cao-cluster/configmaps/cao-fleet-config")
    assert headers["Authorization"] == "Bearer tok-1"


async def test_rereads_the_token_each_call(in_cluster):
    """Projected ServiceAccount tokens are rotated in place.

    Caching the token would work for about an hour and then start returning 401
    forever — the worst shape of bug to meet in a workshop, because it looks like
    the panel spontaneously losing its permissions.
    """
    c = _FakeClient()
    await config.read_configmap(c)
    in_cluster.write_text("tok-2")
    await config.read_configmap(c)
    assert [h["Authorization"] for _, h in c.calls] == ["Bearer tok-1", "Bearer tok-2"]


async def test_verifies_the_api_server_against_the_pod_ca(in_cluster, monkeypatch, tmp_path):
    """The bearer token is the ServiceAccount's own credential.

    An unverified connection would offer it to anything that answered on that
    address, so `verify` must be the pod's CA bundle — never False.
    """
    ca = tmp_path / "ca.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\n")
    monkeypatch.setattr(config, "_SA_CA", str(ca))
    seen = {}

    class _Recorder:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        async def __aenter__(self):
            return _FakeClient()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(config.httpx, "AsyncClient", _Recorder)
    await config.read_configmap()
    assert seen["verify"] == str(ca)
    assert seen["verify"] is not False


async def test_a_missing_ca_is_refused_by_name(in_cluster, monkeypatch):
    """Not "FileNotFoundError" from inside httpx.

    httpx opens the CA bundle when the client is CONSTRUCTED, so a missing file
    raises before any request with a message naming nothing at all — in a pod
    whose token is right and whose CA volume is wrong, that is the least helpful
    error available.
    """
    monkeypatch.setattr(config, "_SA_CA", "/no/such/ca.crt")
    with pytest.raises(RuntimeError, match="no ServiceAccount CA at /no/such/ca.crt"):
        await config.read_configmap()


async def test_plain_http_endpoint_needs_no_ca(in_cluster, monkeypatch):
    """`verify` is meaningless for http:// and must not be required there.

    Only reachable by setting KUBERNETES_API_URL deliberately — the default is
    https, so this is not a downgrade path. It exists because requiring a CA
    bundle for a connection that will never use one is a bug, and it is what makes
    the source testable against a local stand-in.
    """
    monkeypatch.setattr(config, "KUBE_API", "http://127.0.0.1:9891")
    monkeypatch.setattr(config, "_SA_CA", "/no/such/ca.crt")
    assert config._tls_kwargs() == {}

    seen = {}

    class _Recorder:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        async def __aenter__(self):
            return _FakeClient()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(config.httpx, "AsyncClient", _Recorder)
    await config.read_configmap()
    assert "verify" not in seen


async def test_a_missing_key_names_the_keys_that_are_there(in_cluster):
    c = _FakeClient(body={"data": {"nodes.json": "{}"}})
    with pytest.raises(ValueError, match="has no key 'fleet.json'"):
        await config.read_configmap(c)


async def test_snapshot_takes_precedence_over_the_mounted_file(in_cluster):
    assert {m["name"] for m in config.load_machines()} == {"node-a", "node-b", "node-c"}
    assert await config.refresh_configmap(_FakeClient()) is None
    assert [m["name"] for m in config.load_machines()] == ["supervisor", "w-7"]


async def test_a_failed_refresh_keeps_the_last_good_snapshot(in_cluster):
    """The registry must not blank out on an API server blip.

    Losing it would empty the panel, which reads as the entire cluster going
    away rather than as one failed GET.
    """
    await config.refresh_configmap(_FakeClient())
    before = config.load_machines()

    error = await config.refresh_configmap(_FakeClient(raises=OSError("connection reset")))
    assert "OSError" in error
    assert config.load_machines() == before
    assert config.configmap_status()["error"] == error
    # Still reported live: the snapshot is real, just no longer being refreshed.
    assert config.configmap_status()["live"] is True


async def test_falls_back_to_the_file_when_the_first_read_is_denied(in_cluster):
    """An RBAC binding that is missing or wrong must degrade, not crash.

    A panel that is up and one kubelet sync period stale is worth more than a pod
    that CrashLoops while someone fixes a Role.
    """
    assert await config.refresh_configmap(_FakeClient(status=403)) is not None
    assert {m["name"] for m in config.load_machines()} == {"node-a", "node-b", "node-c"}
    assert config.configmap_status()["live"] is False


def test_neither_source_available_names_the_configmap(monkeypatch):
    """The error has to point at the ConfigMap, not only at a file.

    On Kubernetes the fallback path is not expected to exist, so the original
    "copy fleet.example.json" advice would send an operator to fix the wrong
    thing entirely.
    """
    monkeypatch.setattr(config, "FLEET_CONFIGMAP", "cao-fleet-config")
    monkeypatch.setattr(config, "FLEET_CONFIG", "/no/such/fleet.json")
    monkeypatch.setitem(config._snapshot, "error", "HTTPStatusError: 403")
    with pytest.raises(RuntimeError) as exc:
        config.load_machines()
    assert "cao-fleet-config" in str(exc.value)
    assert "403" in str(exc.value)


def test_status_reports_the_file_source_when_unconfigured():
    """The default path must not claim to be watching a ConfigMap."""
    assert config.configmap_status() == {"kind": "file", "path": config.FLEET_CONFIG}


async def test_read_requires_a_namespace(in_cluster, monkeypatch):
    """Outside a pod there is no namespace file, and guessing one is worse than
    saying so: a wrong namespace returns 404, which reads as a missing ConfigMap."""
    monkeypatch.setattr(config, "FLEET_NAMESPACE", None)
    with pytest.raises(RuntimeError, match="namespace unknown"):
        await config.read_configmap(_FakeClient())


def test_missing_registry_raises_clear_error(monkeypatch):
    monkeypatch.setattr(config, "FLEET_CONFIG", "/no/such/fleet.json")
    with pytest.raises(RuntimeError, match="fleet registry not found"):
        config.load_machines()


def test_load_machines_has_three_with_ports():
    machines = config.load_machines()
    assert len(machines) == 3
    names = {m["name"] for m in machines}
    assert {"node-a", "node-b", "node-c"} <= names
    # every node resolves a port (falls back to the top-level default)
    assert all(isinstance(m["port"], int) for m in machines)


def test_base_url_format():
    node = next(m for m in config.load_machines() if m["name"] == "node-a")
    assert config.base_url(node) == "http://100.64.0.11:9889"
