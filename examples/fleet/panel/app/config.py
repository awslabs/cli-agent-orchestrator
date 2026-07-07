"""Panel configuration: fleet registry loading, bind address, and optional auth.

The registry path defaults to `fleet.json` next to this example. If you have not
created one yet (copy `fleet.example.json` -> `fleet.json`), the packaged
`fleet.example.json` is used so the panel still starts with placeholder nodes.
Override any of these with environment variables.
"""
import json
import os

_APP_DIR = os.path.dirname(os.path.abspath(__file__))       # panel/app
_EXAMPLE_ROOT = os.path.dirname(os.path.dirname(_APP_DIR))  # examples/fleet


def _default_config_path():
    real = os.path.join(_EXAMPLE_ROOT, "fleet.json")
    example = os.path.join(_EXAMPLE_ROOT, "fleet.example.json")
    return real if os.path.exists(real) else example


FLEET_CONFIG = os.environ.get("CAO_FLEET_CONFIG") or _default_config_path()
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


def load_machines():
    """Return the fleet nodes, each with a concrete int `port`."""
    try:
        with open(FLEET_CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"fleet registry not found at {FLEET_CONFIG}. Copy "
            f"{os.path.join(_EXAMPLE_ROOT, 'fleet.example.json')} to fleet.json (or set "
            "CAO_FLEET_CONFIG) and list your nodes."
        ) from exc
    default_port = int(cfg.get("port", 9889))
    machines = []
    for m in cfg["machines"]:
        machines.append({**m, "port": int(m.get("port", default_port))})
    return machines


def base_url(machine):
    """http://<host>:<port> for a node dict."""
    return f"http://{machine['host']}:{machine['port']}"
