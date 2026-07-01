"""Panel configuration: fleet registry loading and bind address.

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


def load_machines():
    """Return the fleet nodes, each with a concrete int `port`."""
    cfg = json.load(open(FLEET_CONFIG))
    default_port = int(cfg.get("port", 9889))
    machines = []
    for m in cfg["machines"]:
        machines.append({**m, "port": int(m.get("port", default_port))})
    return machines


def base_url(machine):
    """http://<host>:<port> for a node dict."""
    return f"http://{machine['host']}:{machine['port']}"
