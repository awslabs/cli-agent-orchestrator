#!/usr/bin/env python3
"""The one place the kit reads its per-install settings.

  workplace.py show                 print the resolved values as KEY=value lines
  workplace.py get KEY              print one value (a show KEY such as LOGIN, or a config key
                                    such as relay.enabled); exit 1 when it is unset
  workplace.py render SRC DST [--profile NAME]
                                    render one {{TOKEN}} template; --profile adds PROVIDER/MODEL

Two files in this kit folder (the example is its own workplace), merged in this order:
  workplace.json        shipped defaults (github_login is null there; repos is a placeholder)
  workplace.local.json  your install's values, kept out of git: at least {"github_login": "<you>"}
                        and, unless you edit workplace.json, "repos": ["OWNER/REPO"].
CAO_WORKPLACE_CONFIG names another local file. It exists for tests and dry runs only.

Everything else is derived, never configured: the workplace root (this kit folder), KIT (the same
folder), PB (KIT/pr-watch, the poll loop), the
home, the hostname, the CAO home ($CAO_HOME_DIR or ~/.aws/cli-agent-orchestrator) and REPO_URL.

Fail closed: an unknown key, a bad value or (when a caller requires it) an unset github_login
raises WorkplaceConfigError, whose .reason is one word such as workplace_login_unset. The keys are
an allow-list, so a secret can't be stored here by mistake. One exception: a bad value in the
optional relay.* block turns the relay off (relay.disabled_reason, a WARNING on stderr) instead of
invalidating everything, so it never stops the PR loop, the gates or the dashboard. Stdlib only; safe under python3 -I.

dashboard_port, conductor_session and relay are reserved for optional local deployment tooling.
"""
import argparse
import json
import os
import re
import socket
import sys

DEFAULTS_NAME = "workplace.json"
LOCAL_NAME = "workplace.local.json"
LOCAL_ENV = "CAO_WORKPLACE_CONFIG"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # the kit folder is the workplace

LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
API_RE = re.compile(r"^http://(?:localhost|127\.0\.0\.1):([0-9]{1,5})$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")
MODE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
PROVIDERS = frozenset({"kiro_cli", "claude_code", "codex", "kimi_cli", "copilot_cli", "opencode_cli",
                       "hermes", "cursor_cli", "antigravity_cli", "omp", "grok_cli", "mcode", "qwen_cli"})
TOP_KEYS = frozenset({"schema", "about", "github_login", "repos", "cao_api", "dashboard_port",
                      "cao_source_checkout", "profiles", "conductor_session", "relay"})
RELAY_KEYS = frozenset({"enabled", "checkout", "secrets_file", "state_dir", "herdr_session",
                        "relay_port", "tunnel_mode"})
PROFILE_KEYS = frozenset({"provider", "model"})
TOKEN_RE = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")
HALF_TOKEN_RE = re.compile(r"\{\{|[A-Za-z][A-Za-z0-9_]*\}\}")  # what a broken token leaves behind
# The tokens a template may use. PROVIDER and MODEL exist only when rendering a named profile.
TOKENS = ("WORKPLACE", "KIT", "PB", "GITHUB_LOGIN", "REPO", "REPO_URL", "CAO_API", "CAO_SOURCE",
          "CAO_HOME", "DASHBOARD_PORT", "CONDUCTOR_SESSION", "PROVIDER", "MODEL")
SHOW = ("WORKPLACE", "KIT", "PB", "LOGIN", "REPO", "REPO_URL", "API", "CAO_SOURCE", "CAO_HOME",
        "DASHBOARD_PORT", "CONDUCTOR_SESSION", "RELAY", "LOCAL_FILE")


class WorkplaceConfigError(ValueError):
    """A config problem. str() is '<reason>' or '<reason>: <detail>'; .reason is the one word."""

    def __init__(self, reason, detail=""):
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


def _read(path, missing_ok):
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        if missing_ok:
            return {}
        raise WorkplaceConfigError("workplace_defaults_missing", path)
    except (OSError, ValueError) as exc:
        raise WorkplaceConfigError("workplace_config_unreadable", f"{path} ({type(exc).__name__})")
    if not isinstance(doc, dict):
        raise WorkplaceConfigError("workplace_config_not_object", path)
    return doc


def _merge(base, local):
    out = dict(base)
    for k, v in local.items():
        if k == "relay" and isinstance(v, dict) and isinstance(out.get("relay"), dict):
            out["relay"] = {**out["relay"], **v}
        else:
            out[k] = v  # profiles and repos are replaced whole, so a local file can drop one
    return out


def _path(value, key, home, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or any(c in value for c in "\n\r\0{}"):
        raise WorkplaceConfigError("workplace_bad_value", key)
    if value == "~" or value.startswith("~/"):
        value = home + value[1:]
    if not os.path.isabs(value):
        raise WorkplaceConfigError("workplace_path_not_absolute", key)
    return os.path.normpath(value)


def _port(value, key):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 65536:
        raise WorkplaceConfigError("workplace_bad_value", key)
    return value


def validate(doc, home):
    """A cleaned copy of the merged config, or WorkplaceConfigError."""
    unknown = sorted(set(doc) - TOP_KEYS)
    if unknown:
        raise WorkplaceConfigError("workplace_unknown_key", unknown[0])
    if doc.get("schema") != 1:
        raise WorkplaceConfigError("workplace_bad_schema")
    out = {"schema": 1}
    login = doc.get("github_login")
    if login is not None and (not isinstance(login, str) or not LOGIN_RE.match(login)):
        raise WorkplaceConfigError("workplace_bad_value", "github_login")
    out["github_login"] = login
    repos = doc.get("repos")
    if (not isinstance(repos, list) or not repos or len(set(map(str, repos))) != len(repos)
            or not all(isinstance(r, str) and REPO_RE.match(r) and r.split("/")[1] not in (".", "..")
                       for r in repos)):
        raise WorkplaceConfigError("workplace_bad_value", "repos")
    out["repos"] = list(repos)
    api = doc.get("cao_api")
    m = API_RE.match(api) if isinstance(api, str) else None
    if not m or not 0 < int(m.group(1)) < 65536:
        raise WorkplaceConfigError("workplace_bad_value", "cao_api (http://localhost:<port> or http://127.0.0.1:<port>)")
    out["cao_api"] = api
    out["dashboard_port"] = _port(doc.get("dashboard_port"), "dashboard_port")
    out["cao_source_checkout"] = _path(doc.get("cao_source_checkout"), "cao_source_checkout", home)
    profiles = doc.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise WorkplaceConfigError("workplace_bad_value", "profiles")
    out["profiles"] = {}
    for name, spec in profiles.items():
        if not isinstance(name, str) or not NAME_RE.match(name) or not isinstance(spec, dict):
            raise WorkplaceConfigError("workplace_bad_value", f"profiles.{name}")
        extra = sorted(set(spec) - PROFILE_KEYS)
        if extra:
            raise WorkplaceConfigError("workplace_unknown_key", f"profiles.{name}.{extra[0]}")
        if spec.get("provider") not in PROVIDERS:
            raise WorkplaceConfigError("workplace_bad_value", f"profiles.{name}.provider")
        model = spec.get("model")
        if model is not None and (not isinstance(model, str) or not MODEL_RE.match(model)):
            raise WorkplaceConfigError("workplace_bad_value", f"profiles.{name}.model")
        out["profiles"][name] = {"provider": spec["provider"], "model": model}
    session = doc.get("conductor_session")
    if not isinstance(session, str) or not NAME_RE.match(session):
        raise WorkplaceConfigError("workplace_bad_value", "conductor_session")
    out["conductor_session"] = session
    relay = doc.get("relay")
    if isinstance(relay, dict):
        extra = sorted(set(relay) - RELAY_KEYS)
        if extra:  # an unknown key stays fatal everywhere: the allow-list keeps secrets out
            raise WorkplaceConfigError("workplace_unknown_key", f"relay.{extra[0]}")
    try:
        out["relay"] = _relay(relay, home)
    except WorkplaceConfigError as exc:
        # The relay is optional: a bad relay value turns the relay off (with the reason), and never
        # invalidates the login, repo or CAO API the PR loop, the gates and the dashboard need.
        out["relay"] = {"enabled": False, "checkout": None, "secrets_file": None, "state_dir": None,
                        "herdr_session": None, "relay_port": None, "tunnel_mode": None,
                        "disabled_reason": str(exc)}
        out["warnings"] = [f"relay disabled: {exc}"]
    return out


def _relay(relay, home):
    if not isinstance(relay, dict):
        raise WorkplaceConfigError("workplace_bad_value", "relay")
    if not isinstance(relay.get("enabled"), bool):
        raise WorkplaceConfigError("workplace_bad_value", "relay.enabled")
    hs = relay.get("herdr_session")
    if hs is not None and (not isinstance(hs, str) or not NAME_RE.match(hs)):
        raise WorkplaceConfigError("workplace_bad_value", "relay.herdr_session")
    mode = relay.get("tunnel_mode")
    if not isinstance(mode, str) or not MODE_RE.match(mode):
        raise WorkplaceConfigError("workplace_bad_value", "relay.tunnel_mode")
    return {
        "enabled": relay["enabled"],
        "checkout": _path(relay.get("checkout"), "relay.checkout", home),
        "secrets_file": _path(relay.get("secrets_file"), "relay.secrets_file", home),
        "state_dir": _path(relay.get("state_dir"), "relay.state_dir", home),
        "herdr_session": hs,
        "relay_port": _port(relay.get("relay_port"), "relay.relay_port"),
        "tunnel_mode": mode,
        "disabled_reason": None,
    }


_WARNED = set()


def _warn_once(note):
    """One stderr line per process and note: a disabled relay is logged, never fatal."""
    if note not in _WARNED:
        _WARNED.add(note)
        print(f"WARNING workplace: {note}", file=sys.stderr)


def override_warning():
    """The visible line for an active CAO_WORKPLACE_CONFIG override, or None."""
    path = os.environ.get(LOCAL_ENV)
    if not path:
        return None
    return (f"WARNING {LOCAL_ENV} is set: this install's values come from {path} "
            "(meant for tests and dry runs only; unset it in a live shell)")


def local_path(root=None):
    return os.environ.get(LOCAL_ENV) or os.path.join(root or ROOT, LOCAL_NAME)


def load(require=("github_login",), root=None, home=None, local=None):
    """The merged, validated config plus the derived values. Raises WorkplaceConfigError.

    require: config keys that must be set (not null). The default fails closed on an unset login;
    callers that never need the login (the dashboard, the repo defaults) pass require=().
    local: a dict to use instead of reading the local file (configure validates with it)."""
    root = os.path.abspath(root or ROOT)
    home = os.path.abspath(home or os.path.expanduser("~"))
    base = _read(os.path.join(root, DEFAULTS_NAME), missing_ok=False)
    lpath = local_path(root)
    over = local if local is not None else _read(lpath, missing_ok=True)
    if "schema" in over and over["schema"] != 1:
        raise WorkplaceConfigError("workplace_bad_schema", lpath)
    cfg = validate(_merge(base, {k: v for k, v in over.items() if k != "about"}), home)
    cfg.setdefault("warnings", [])
    for note in cfg["warnings"]:
        _warn_once(note)
    for key in require:
        if cfg.get(key) is None:
            raise WorkplaceConfigError("workplace_login_unset" if key == "github_login" else "workplace_value_unset", key)
    cao_home = os.environ.get("CAO_HOME_DIR", "").strip()
    cfg.update({
        "workplace": root,
        "kit": root,
        "pb": os.path.join(root, "pr-watch"),
        "home": home,
        "hostname": socket.gethostname(),
        "cao_home": os.path.abspath(os.path.expanduser(cao_home)) if cao_home
        else os.path.join(home, ".aws", "cli-agent-orchestrator"),
        "repo": cfg["repos"][0],
        "repo_url": f"https://github.com/{cfg['repos'][0]}.git",
        "local_file": lpath,
    })
    return cfg


def github_login(**kw):
    """The operator's GitHub login. Fails closed with workplace_login_unset."""
    return load(**kw)["github_login"]


def values(cfg, profile=None):
    """The token -> text map for render(). profile adds PROVIDER and MODEL from cfg['profiles']."""
    v = {"WORKPLACE": cfg["workplace"], "KIT": cfg["kit"], "PB": cfg["pb"],
         "GITHUB_LOGIN": cfg["github_login"], "REPO": cfg["repo"], "REPO_URL": cfg["repo_url"],
         "CAO_API": cfg["cao_api"], "CAO_SOURCE": cfg["cao_source_checkout"], "CAO_HOME": cfg["cao_home"],
         "DASHBOARD_PORT": str(cfg["dashboard_port"]), "CONDUCTOR_SESSION": cfg["conductor_session"]}
    if profile is not None:
        spec = cfg["profiles"].get(profile)
        if spec is None:
            raise WorkplaceConfigError("workplace_profile_not_configured", profile)
        v["PROVIDER"], v["MODEL"] = spec["provider"], spec["model"]
    return v


def render(text, vals):
    """Replace every {{TOKEN}}. Strict: an unknown or unset token, a {{ left, or a NAME}} left raises.

    A bare }} (the end of a JSON object such as {"prs": {}}) is ordinary text."""
    def sub(m):
        name = m.group(1)
        if name not in TOKENS:
            raise WorkplaceConfigError("render_unknown_token", name)
        value = vals.get(name)
        if value is None:
            raise WorkplaceConfigError("render_token_unset", name)
        value = str(value)
        if "{" in value or "}" in value or "\n" in value:
            raise WorkplaceConfigError("render_bad_value", name)
        return value
    out = TOKEN_RE.sub(sub, text)
    left = HALF_TOKEN_RE.search(out)
    if left:
        raise WorkplaceConfigError("render_token_left", out[left.start():left.start() + 40])
    return out


def render_file(src, cfg, profile=None):
    with open(src, encoding="utf-8") as fh:
        return render(fh.read(), values(cfg, profile))


def show_items(cfg):
    return {"WORKPLACE": cfg["workplace"], "KIT": cfg["kit"], "PB": cfg["pb"],
            "LOGIN": cfg["github_login"] or "", "REPO": cfg["repo"], "REPO_URL": cfg["repo_url"],
            "API": cfg["cao_api"], "CAO_SOURCE": cfg["cao_source_checkout"], "CAO_HOME": cfg["cao_home"],
            "DASHBOARD_PORT": str(cfg["dashboard_port"]), "CONDUCTOR_SESSION": cfg["conductor_session"],
            "RELAY": "on" if cfg["relay"]["enabled"] else
            ("off (invalid: %s)" % cfg["relay"]["disabled_reason"] if cfg["relay"]["disabled_reason"] else "off"),
            "LOCAL_FILE": cfg["local_file"]}


def _lookup(cfg, key):
    shown = show_items(cfg)
    if key.upper() in shown:
        return shown[key.upper()] or None
    node = cfg
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            raise WorkplaceConfigError("workplace_unknown_key", key)
        node = node[part]
    if isinstance(node, bool):
        return "true" if node else "false"
    if isinstance(node, (dict, list)):
        return json.dumps(node, sort_keys=True)
    return None if node is None else str(node)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("show", help="print the resolved values as KEY=value lines")
    g = sub.add_parser("get", help="print one value")
    g.add_argument("key")
    r = sub.add_parser("render", help="render one template")
    r.add_argument("src")
    r.add_argument("dst")
    r.add_argument("--profile", help="the profile name, for {{PROVIDER}} and {{MODEL}}")
    a = ap.parse_args(argv)
    try:
        if a.command == "show":
            if override_warning():
                print(override_warning(), file=sys.stderr)
            cfg = load(require=())
            for k, v in show_items(cfg).items():
                print(f"{k}={v}")
            if cfg["github_login"] is None:
                print("workplace_login_unset: set github_login in workplace.local.json (see README)", file=sys.stderr)
                return 1
            return 0
        if a.command == "get":
            cfg = load(require=("github_login",) if a.key.upper() in ("LOGIN", "GITHUB_LOGIN") else ())
            value = _lookup(cfg, a.key)
            if value is None:
                print(f"workplace_value_unset: {a.key}", file=sys.stderr)
                return 1
            print(value)
            return 0
        cfg = load()
        out = render_file(a.src, cfg, a.profile)
        tmp = f"{a.dst}.tmp-{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(out)
        os.replace(tmp, a.dst)
        print(f"RENDERED {a.dst}")
        return 0
    except WorkplaceConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"workplace_io_error: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
