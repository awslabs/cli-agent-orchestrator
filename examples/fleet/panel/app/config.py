"""Panel configuration: fleet registry loading, bind address, and optional auth.

The registry has two possible sources.

**A file** (the default). The path defaults to `fleet.json` next to this example.
If you have not created one yet (copy `fleet.example.json` -> `fleet.json`), the
packaged `fleet.example.json` is used so the panel still starts with placeholder
nodes. This is the right source for a fleet of machines you maintain by hand.

**A Kubernetes ConfigMap** (set `CAO_FLEET_CONFIGMAP`). Read straight from the
API server rather than through a mounted volume, because on Kubernetes the
registry is not a static file: the worker broker rewrites it as elastic workers
take and release leases. A mounted ConfigMap only refreshes on the kubelet's sync
period, so a worker that lives for 40 seconds could finish before the panel ever
saw it — the mount's staleness window is longer than the thing it describes. The
API server has no such lag.

Override any of these with environment variables.
"""
import asyncio
import json
import os
import time

import httpx

_APP_DIR = os.path.dirname(os.path.abspath(__file__))       # panel/app
_EXAMPLE_ROOT = os.path.dirname(os.path.dirname(_APP_DIR))  # examples/fleet

# Where a pod finds its own ServiceAccount credentials. Standard paths, not
# configurable: if these are missing the panel is not running in a pod and the
# ConfigMap source cannot work anyway.
_SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
_SA_TOKEN = os.path.join(_SA_DIR, "token")
_SA_CA = os.path.join(_SA_DIR, "ca.crt")
_SA_NAMESPACE = os.path.join(_SA_DIR, "namespace")


def _default_config_path():
    real = os.path.join(_EXAMPLE_ROOT, "fleet.json")
    example = os.path.join(_EXAMPLE_ROOT, "fleet.example.json")
    return real if os.path.exists(real) else example


def _default_namespace():
    try:
        with open(_SA_NAMESPACE, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


FLEET_CONFIG = os.environ.get("CAO_FLEET_CONFIG") or _default_config_path()

# ── The Kubernetes ConfigMap source ───────────────────────────────────────────
# Unset by default, which is what keeps `examples/fleet/panel` runnable from a
# checkout with nothing but `uv run`: no cluster, no token, no behaviour change.
FLEET_CONFIGMAP = os.environ.get("CAO_FLEET_CONFIGMAP") or None
FLEET_CONFIGMAP_KEY = os.environ.get("CAO_FLEET_CONFIGMAP_KEY", "fleet.json")
FLEET_NAMESPACE = os.environ.get("CAO_FLEET_NAMESPACE") or _default_namespace()
# How often the background task re-reads it. Seconds, and deliberately small: the
# whole point of this source is that it is not subject to the kubelet's sync
# period. One GET of one ConfigMap is cheap, and it is one request per panel
# regardless of how many browsers are watching.
FLEET_CONFIGMAP_INTERVAL = float(os.environ.get("CAO_FLEET_CONFIGMAP_INTERVAL", "5"))
# In-cluster API server. The DNS name rather than KUBERNETES_SERVICE_HOST so the
# certificate's SAN matches without extra handling.
KUBE_API = os.environ.get("KUBERNETES_API_URL", "https://kubernetes.default.svc")

# Bind the panel to loopback by default; set CAO_PANEL_HOST to your private-network
# address (e.g. the coordinator's Tailscale/WireGuard/LAN IP) to reach it from
# other devices.
PANEL_HOST = os.environ.get("CAO_PANEL_HOST", "127.0.0.1")
PANEL_PORT = int(os.environ.get("CAO_PANEL_PORT", "9888"))
# Optional shared secret. When set, every panel request must present it (HTTP Basic
# password — any username — or `Authorization: Bearer <token>`). Unset (the default)
# leaves the panel open, which is fine on loopback but NOT once you bind CAO_PANEL_HOST
# to a network address. See README "Security".
PANEL_TOKEN = os.environ.get("CAO_PANEL_TOKEN") or None


# Last good ConfigMap read. `machines` is None until one has succeeded, which is
# what makes the file a fallback rather than a race: `load_machines()` only
# prefers this snapshot once there is something in it.
_snapshot = {"machines": None, "at": None, "error": None, "reads": 0}


def _parse(text, source):
    """Registry JSON -> node dicts, each with a concrete int `port`."""
    cfg = json.loads(text)
    default_port = int(cfg.get("port", 9889))
    machines = cfg.get("machines")
    if not isinstance(machines, list):
        raise ValueError(f"{source} has no `machines` list")
    return [{**m, "port": int(m.get("port", default_port))} for m in machines]


def load_machines():
    """Return the fleet nodes, each with a concrete int `port`.

    Stays synchronous, and never touches the network, because it is called from
    inside request handlers — including one per proxied route. The ConfigMap is
    read by the background task below and served from the snapshot here, so a
    slow or unreachable API server delays the registry rather than blocking the
    event loop or failing a request that was already in flight.
    """
    if _snapshot["machines"] is not None:
        return _snapshot["machines"]
    try:
        with open(FLEET_CONFIG, encoding="utf-8") as f:
            return _parse(f.read(), FLEET_CONFIG)
    except FileNotFoundError as exc:
        if FLEET_CONFIGMAP:
            # Configured for the API server, which has not answered yet or is
            # refusing to. Say which, because the two need opposite fixes and the
            # file this mentions may not be expected to exist at all.
            raise RuntimeError(
                f"fleet registry unavailable: ConfigMap '{FLEET_CONFIGMAP}' has not been "
                f"read yet"
                + (f" (last error: {_snapshot['error']})" if _snapshot["error"] else "")
                + f", and no fallback file at {FLEET_CONFIG}."
            ) from exc
        raise RuntimeError(
            f"fleet registry not found at {FLEET_CONFIG}. Copy "
            f"{os.path.join(_EXAMPLE_ROOT, 'fleet.example.json')} to fleet.json (or set "
            "CAO_FLEET_CONFIG) and list your nodes."
        ) from exc


def configmap_status():
    """What the ConfigMap source is doing, for /api/fleet's `source` field.

    Worth surfacing: a panel that has quietly fallen back to a mounted file still
    works, but its view of elastic workers is up to a kubelet sync period stale —
    which looks exactly like workers failing to register. This is how an operator
    tells those apart without reading logs.
    """
    if not FLEET_CONFIGMAP:
        return {"kind": "file", "path": FLEET_CONFIG}
    return {
        "kind": "configmap",
        "name": FLEET_CONFIGMAP,
        "namespace": FLEET_NAMESPACE,
        "live": _snapshot["machines"] is not None,
        "age": None if _snapshot["at"] is None else round(time.monotonic() - _snapshot["at"], 1),
        "reads": _snapshot["reads"],
        "error": _snapshot["error"],
    }


def _tls_kwargs():
    """`verify` for the API server connection — the pod's CA, never False.

    The token this request carries is a bearer credential for the whole
    ServiceAccount, so an unverified TLS connection would hand it to anything that
    answered on that address.

    Two details worth keeping:

    * httpx opens the CA bundle when the client is CONSTRUCTED, not when a request
      is made, so a missing file raises a bare FileNotFoundError naming nothing.
      Checked here instead, where the message can say which file and why it is
      needed.
    * `verify` is meaningless for a plain-HTTP URL, and passing it there would make
      a non-TLS endpoint require a CA bundle it will never use. This is not a
      downgrade path: KUBE_API is https unless someone sets KUBERNETES_API_URL to
      something else on purpose.
    """
    if not KUBE_API.lower().startswith("https:"):
        return {}
    if not os.path.isfile(_SA_CA):
        raise RuntimeError(
            f"cannot verify {KUBE_API}: no ServiceAccount CA at {_SA_CA}. The panel "
            "reads the fleet ConfigMap with its pod's bearer token and will not send "
            "it over an unverified connection."
        )
    return {"verify": _SA_CA}


async def read_configmap(client_=None):
    """GET the registry ConfigMap from the API server. Returns node dicts.

    Uses httpx and the pod's own ServiceAccount rather than the `kubernetes`
    client library. That library would be by far the panel's heaviest dependency
    — it pulls in its own HTTP stack and several auth backends — to issue one
    authenticated GET of one object, against an API this example already talks to
    with httpx for everything else. The token is re-read on every call because
    projected ServiceAccount tokens are rotated in place.
    """
    if not FLEET_CONFIGMAP:
        raise RuntimeError("CAO_FLEET_CONFIGMAP is not set")
    if not FLEET_NAMESPACE:
        raise RuntimeError(
            "namespace unknown: set CAO_FLEET_NAMESPACE, or run in a pod where "
            f"{_SA_NAMESPACE} exists"
        )
    with open(_SA_TOKEN, encoding="utf-8") as f:
        token = f.read().strip()
    url = f"{KUBE_API}/api/v1/namespaces/{FLEET_NAMESPACE}/configmaps/{FLEET_CONFIGMAP}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    async def _get(c):
        r = await c.get(url, headers=headers)
        r.raise_for_status()
        return r.json()

    if client_ is not None:
        body = await _get(client_)
    else:
        async with httpx.AsyncClient(timeout=10.0, **_tls_kwargs()) as c:
            body = await _get(c)

    data = body.get("data") or {}
    if FLEET_CONFIGMAP_KEY not in data:
        raise ValueError(
            f"ConfigMap '{FLEET_CONFIGMAP}' has no key '{FLEET_CONFIGMAP_KEY}' "
            f"(keys: {sorted(data) or 'none'})"
        )
    return _parse(data[FLEET_CONFIGMAP_KEY], f"ConfigMap '{FLEET_CONFIGMAP}'")


async def refresh_configmap(client_=None):
    """One refresh cycle. Returns the error string, or None on success.

    A failure deliberately leaves the previous snapshot in place. The registry
    describes long-lived nodes plus whichever workers hold a lease right now, so
    briefly-stale entries are far better than an empty fleet — an API server blip
    would otherwise blank the panel and read as the whole cluster going away.
    """
    try:
        machines = await read_configmap(client_)
    except Exception as exc:
        _snapshot["error"] = f"{type(exc).__name__}: {exc}"
        return _snapshot["error"]
    _snapshot["machines"] = machines
    _snapshot["at"] = time.monotonic()
    _snapshot["reads"] += 1
    _snapshot["error"] = None
    return None


async def watch_configmap():
    """Refresh forever. Cancelled at shutdown by the caller.

    Errors are swallowed on purpose: `refresh_configmap` has already recorded
    them where `configmap_status()` will report them, and a task that dies on the
    first transient failure would freeze the registry for the life of the pod.
    """
    while True:
        await asyncio.sleep(FLEET_CONFIGMAP_INTERVAL)
        await refresh_configmap()


def base_url(machine):
    """http://<host>:<port> for a node dict."""
    return f"http://{machine['host']}:{machine['port']}"


def ws_url(machine):
    """ws://<host>:<port> for a node dict — the terminal socket's upstream."""
    return f"ws://{machine['host']}:{machine['port']}"
