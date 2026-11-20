#!/usr/bin/env python3
"""One-time migration: herdr TUI sessions -> Herdr desktop app (herdr-core).

Reads the herdr TUI (``herdr`` 0.9.x) and recreates every pane as a terminal in
a herdr-core (``hc``) session, placed with the app's ``herdr.space`` /
``herdr.tab`` labels so the Herdr app shows one space per TUI workspace and one
tab per TUI tab. Panes running Claude Code or Codex are relaunched resumed on
their agent session id with their original arguments; every other pane gets a
login shell in its last working directory. The TUI is only read, never changed.

Usage:
    herdr_tui_to_app.py                      # dry run: print the plan
    herdr_tui_to_app.py --include-cao        # also plan the CAO session ("cao")
    herdr_tui_to_app.py --tui-session default --apply
    herdr_tui_to_app.py --rename-only        # finish naming spaces/tabs later
    herdr_tui_to_app.py --self-test          # offline unit checks

Options:
    --tui-session NAME   TUI session to migrate (repeatable). Default: all
                         sessions from ``herdr session list`` except "cao".
    --include-cao        Include the "cao" session in the default selection.
    --workspace ID       Only this TUI workspace id (repeatable).
    --hc-session NAME    Target hc session (default: "default", the core the
                         app connects to).
    --apply              Create the terminals (default is a dry run).
    --shell-only         Start shells everywhere instead of resuming agents.
    --skip-name-matches  Skip TUI tabs whose space+tab names already exist in
                         the app document (e.g. migrated earlier by hand).
    --name-wait SECONDS  How long to wait for the app to adopt new spaces
                         before naming them (default 20).
    --rename-only        Only name spaces/tabs of already migrated terminals.
    --self-test          Run the built-in assert-based checks and exit.

Idempotent: each terminal carries ``migrated.from=herdr-tui:<session>/<workspace
id>/<pane id>``; panes whose label already exists in the target core are skipped.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import select
import shlex
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

CAO_SESSION = "cao"
APP_DOC = "herdr-app"
LABEL_FROM = "migrated.from"
LABEL_TAB_FROM = "migrated.tab_from"
LABEL_SPACE_NAME = "migrated.space_name"
LABEL_TAB_NAME = "migrated.tab_name"
FROM_PREFIX = "herdr-tui:"
# Each would point hc at the caller's core; HC_SOCKET overrides the endpoint
# outright (herdr-core docs/security.md).
HC_SCOPE_VARS = ("HC_TERMINAL_ID", "HC_TERMINAL_SOCKET", "HC_SCOPE", "HC_SOCKET")

# The wrapper the Herdr app itself uses for command terminals (copied from a
# live app-created terminal): run the command in a login shell, then stay in
# a login shell after it exits so the tab does not die with the agent.
LOGIN_WRAPPER = (
    "shell=${SHELL:-/bin/sh}\n"
    'case "${shell##*/}" in\n'
    '  fish) exec "$shell" -lic \'command $argv[2..]; exec $argv[1] -li\' "$shell" "$@" ;;\n'
    '  sh|bash|zsh|dash|ksh) exec "$shell" -lic \'"$@"; exec "$0" -li\' "$shell" "$@" ;;\n'
    '  *) exec sh -c \'"$@"; exec "${SHELL:-sh}" -i\' sh "$@" ;;\n'
    "esac"
)

# --- Claude Code flag grammar (from `claude --help`, 2.1.x) -----------------
CLAUDE_VALUE = {
    "--agent",
    "--agents",
    "--append-system-prompt",
    "--append-system-prompt-file",
    "--autocompact",
    "--debug-file",
    "--effort",
    "--environment",
    "--fallback-model",
    "--input-format",
    "--json-schema",
    "--max-budget-usd",
    "--model",
    "-n",
    "--name",
    "--output-format",
    "--permission-mode",
    "--permission-prompts",
    "--permission-prompt-tool",
    "--plugin-dir",
    "--plugin-url",
    "--remote-control-session-name-prefix",
    "--session-id",
    "--setting-sources",
    "--settings",
    "--system-prompt",
    "--system-prompt-file",
    "--system-prompt-snapshot",
}
CLAUDE_VARIADIC = {
    "--add-dir",
    "--allowedTools",
    "--allowed-tools",
    "--betas",
    "--disallowedTools",
    "--disallowed-tools",
    "--file",
    "--mcp-config",
    "--tools",
}
CLAUDE_OPTIONAL = {
    "-r",
    "--resume",
    "-d",
    "--debug",
    "--cloud",
    "--from-pr",
    "--prompt-suggestions",
    "--remote-control",
    "--teleport",
    "-w",
    "--worktree",
}
# Dropped: other ways to pick/create a session, background/print modes, and
# --worktree (would create a new worktree; the pane cwd already is one).
CLAUDE_DROP = {
    "-r",
    "--resume",
    "-c",
    "--continue",
    "--session-id",
    "--fork-session",
    "--from-pr",
    "--teleport",
    "--cloud",
    "--bg",
    "--background",
    "-p",
    "--print",
    "-w",
    "--worktree",
}

# --- Codex flag grammar (from `codex --help` / `codex resume --help`, 0.169) -
CODEX_VALUE = {
    "-c",
    "--config",
    "--enable",
    "--disable",
    "--remote",
    "--remote-auth-token-env",
    "-m",
    "--model",
    "--local-provider",
    "-p",
    "--profile",
    "-s",
    "--sandbox",
    "-C",
    "--cd",
    "--add-dir",
    "-a",
    "--ask-for-approval",
}
CODEX_VARIADIC = {"-i", "--image"}
CODEX_DROP = {"-i", "--image", "--last", "--all", "--include-non-interactive", "--no-daemon"}
CODEX_SESSION_SUBCOMMANDS = {"resume", "fork"}


class HcError(RuntimeError):
    def __init__(self, kind: str, message: str):
        super().__init__(f"{kind}: {message}")
        self.kind = kind


# =============================================================================
# Pure functions (covered by --self-test)
# =============================================================================


def snapshot_body(raw: Dict[str, Any]) -> Dict[str, Any]:
    """`herdr api snapshot` prints result.snapshot (or the snapshot itself)."""
    body = raw.get("result", raw) if isinstance(raw, dict) else {}
    return body.get("snapshot", body)


def _split_flag(tok: str) -> Tuple[str, bool]:
    if tok.startswith("--") and "=" in tok:
        return tok.split("=", 1)[0], True
    return tok, False


def _walk_args(
    args: Sequence[str], value: set, variadic: set, optional: set
) -> List[Tuple[str, List[str]]]:
    """Group argv tokens into (flag, values) items; positionals have flag ''."""
    items: List[Tuple[str, List[str]]] = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            items.extend(("", [t]) for t in args[i + 1 :])
            break
        if not tok.startswith("-") or tok == "-":
            items.append(("", [tok]))
            i += 1
            continue
        name, inline = _split_flag(tok)
        group = [tok]
        i += 1
        if not inline:
            if name in value and i < len(args):
                group.append(args[i])
                i += 1
            elif name in variadic:
                while i < len(args) and not args[i].startswith("-"):
                    group.append(args[i])
                    i += 1
            elif name in optional and i < len(args) and not args[i].startswith("-"):
                group.append(args[i])
                i += 1
        items.append((name, group))
    return items


def _program_index(argv: Sequence[str], agent: str) -> int:
    for i, tok in enumerate(argv):
        if os.path.basename(tok).startswith(agent):
            return i
    return 0


def rewrite_claude(argv: Sequence[str], session_id: str) -> Tuple[List[str], List[str]]:
    """Original claude argv -> same argv resuming `session_id`. Returns (argv, notes)."""
    idx = _program_index(argv, "claude")
    out = list(argv[: idx + 1])
    notes: List[str] = []
    for name, group in _walk_args(argv[idx + 1 :], CLAUDE_VALUE, CLAUDE_VARIADIC, CLAUDE_OPTIONAL):
        if name == "":
            notes.append(f"dropped positional prompt {group[0]!r}")
        elif name in CLAUDE_DROP:
            if name not in ("-r", "--resume"):
                notes.append(f"dropped {' '.join(group)}")
        else:
            out.extend(group)
    out += ["--resume", session_id]
    return out, notes


def rewrite_codex(argv: Sequence[str], session_id: str) -> Tuple[Optional[List[str]], List[str]]:
    """Original codex argv -> `codex resume <id> <flags> --no-daemon`.

    Returns (None, notes) when the argv is a non-interactive subcommand.
    """
    idx = _program_index(argv, "codex")
    prog = list(argv[: idx + 1])
    flags: List[str] = []
    notes: List[str] = []
    items = _walk_args(argv[idx + 1 :], CODEX_VALUE, CODEX_VARIADIC, set())
    seen_sub: Optional[str] = None
    for name, group in items:
        if name == "":
            tok = group[0]
            if seen_sub is None and tok in CODEX_SESSION_SUBCOMMANDS:
                seen_sub = tok
                continue
            if seen_sub is None and _looks_like_subcommand(tok):
                return None, [f"codex subcommand {tok!r} is not an interactive session"]
            if seen_sub in CODEX_SESSION_SUBCOMMANDS and _is_session_ref(tok):
                seen_sub = seen_sub + ":done"
                continue
            notes.append(f"dropped positional prompt {tok!r}")
        elif name in CODEX_DROP:
            if name != "--no-daemon":
                notes.append(f"dropped {' '.join(group)}")
        else:
            flags.extend(group)
    return prog + ["resume", session_id] + flags + ["--no-daemon"], notes


_CODEX_SUBCOMMANDS = {
    "agents",
    "exec",
    "e",
    "review",
    "login",
    "logout",
    "mcp",
    "plugin",
    "app-server",
    "remote-control",
    "completion",
    "update",
    "doctor",
    "sandbox",
    "debug",
    "apply",
    "a",
    "queue",
    "archive",
    "delete",
    "migrate-rollouts",
    "migrate-nfs-db",
    "unarchive",
    "cloud",
    "exec-server",
    "features",
    "help",
}


def _looks_like_subcommand(tok: str) -> bool:
    return tok in _CODEX_SUBCOMMANDS


def _is_session_ref(tok: str) -> bool:
    return " " not in tok and len(tok) <= 64


def leader_argv(process_info: Optional[Dict[str, Any]]) -> Optional[List[str]]:
    """argv of the pane's foreground process-group leader (what the user ran)."""
    if not process_info:
        return None
    procs = process_info.get("foreground_processes") or []
    pgid = process_info.get("foreground_process_group_id")
    for p in procs:
        if p.get("pid") == pgid and p.get("argv"):
            return list(p["argv"])
    for p in procs:
        if p.get("argv"):
            return list(p["argv"])
    return None


def choose_command(
    pane: Dict[str, Any], process_info: Optional[Dict[str, Any]], shell: str, shell_only: bool
) -> Dict[str, Any]:
    """Decide what the new terminal runs. Returns {kind, argv, note}."""
    cwd = pane.get("foreground_cwd") or pane.get("cwd") or os.path.expanduser("~")
    shell_argv = [shell, "-l"]
    sess = pane.get("agent_session") or {}
    agent = pane.get("agent") or sess.get("agent")
    sid = sess.get("value") if sess.get("kind") == "id" else None
    argv = leader_argv(process_info)
    fg = os.path.basename(argv[0]) if argv else "unknown"

    def shell_cmd(note: str) -> Dict[str, Any]:
        return {"kind": "shell", "argv": shell_argv, "cwd": cwd, "note": note}

    if shell_only:
        return shell_cmd(f"--shell-only (foreground was {fg})")
    if agent not in ("claude", "codex"):
        return shell_cmd(f"login shell; foreground {fg} is not a resumable agent")
    if not sid:
        return shell_cmd(f"login shell; {agent} pane has no agent session id")
    if not argv or not any(os.path.basename(t).startswith(agent) for t in argv[:3]):
        return shell_cmd(
            f"login shell; {agent} is no longer the foreground ({fg}); "
            f"resume by hand: {agent} {'resume ' + sid + ' --no-daemon' if agent == 'codex' else '--resume ' + sid}"
        )
    if agent == "claude":
        new, notes = rewrite_claude(argv, sid)
    else:
        new, notes = rewrite_codex(argv, sid)
        if new is None:
            return shell_cmd("login shell; " + "; ".join(notes))
    note = f"resume {agent} session {sid}"
    if notes:
        note += " (" + "; ".join(notes) + ")"
    return {"kind": agent, "argv": new, "cwd": cwd, "note": note}


def new_space_id() -> str:
    return "sp" + secrets.token_hex(16)


def new_tab_id() -> str:
    return "tb" + secrets.token_hex(16)


def from_label(session: str, workspace_id: str, pane_id: str) -> str:
    return f"{FROM_PREFIX}{session}/{workspace_id}/{pane_id}"


def tab_from_label(session: str, tab_id: str) -> str:
    return f"{FROM_PREFIX}{session}/{tab_id}"


def existing_placement(terminals: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    """Migrated terminals in the target core -> lookup tables.

    Returns {"from": {migrated.from: address}, "space": {"<session>/<ws>": sp id},
    "tab": {migrated.tab_from: tb id}}.
    """
    out: Dict[str, Dict[str, str]] = {"from": {}, "space": {}, "tab": {}}
    for t in terminals:
        labels = t.get("labels") or {}
        src = labels.get(LABEL_FROM, "")
        if not src.startswith(FROM_PREFIX) or "/" in t.get("address", "/"):
            continue
        out["from"][src] = t.get("address", "?")
        sess_ws = "/".join(src[len(FROM_PREFIX) :].split("/")[:2])
        if labels.get("herdr.space"):
            out["space"].setdefault(sess_ws, labels["herdr.space"])
        if labels.get(LABEL_TAB_FROM) and labels.get("herdr.tab"):
            out["tab"].setdefault(labels[LABEL_TAB_FROM], labels["herdr.tab"])
    return out


def _norm(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def doc_name_index(doc: Any) -> Dict[str, set]:
    """Normalized space name -> normalized tab names present in the app doc."""
    idx: Dict[str, set] = {}
    for sp in (doc or {}).get("spaces", []) if isinstance(doc, dict) else []:
        idx.setdefault(_norm(sp.get("name") or ""), set()).update(
            _norm(t.get("name") or "") for t in sp.get("tabs", [])
        )
    return idx


def build_plan(
    session: str,
    snap: Dict[str, Any],
    process_infos: Dict[str, Optional[Dict[str, Any]]],
    existing: Dict[str, Dict[str, str]],
    shell: str,
    shell_only: bool = False,
    workspaces_filter: Optional[set] = None,
    doc_names: Optional[Dict[str, set]] = None,
    skip_name_matches: bool = False,
) -> List[Dict[str, Any]]:
    """Snapshot -> list of spaces [{workspace, space_id, name, tabs:[{..., panes:[...]}]}]."""
    tabs_by_ws: Dict[str, List[Dict[str, Any]]] = {}
    for t in sorted(snap.get("tabs", []), key=lambda t: t.get("number", 0)):
        tabs_by_ws.setdefault(t["workspace_id"], []).append(t)
    panes_by_tab: Dict[str, List[Dict[str, Any]]] = {}
    for p in snap.get("panes", []):
        panes_by_tab.setdefault(p["tab_id"], []).append(p)

    plan: List[Dict[str, Any]] = []
    for ws in sorted(snap.get("workspaces", []), key=lambda w: w.get("number", 0)):
        wid = ws["workspace_id"]
        if workspaces_filter and wid not in workspaces_filter:
            continue
        ws_name = ws.get("label") or wid
        space = {
            "workspace_id": wid,
            "name": ws_name,
            "space_id": existing["space"].get(f"{session}/{wid}") or new_space_id(),
            "tabs": [],
        }
        for tab in tabs_by_ws.get(wid, []):
            tid = tab["tab_id"]
            tab_name = tab.get("label") or ""
            tfrom = tab_from_label(session, tid)
            name_match = _norm(tab_name) in (doc_names or {}).get(_norm(ws_name), set())
            entry = {
                "tab_id": tid,
                "name": tab_name,
                "tab_from": tfrom,
                "herdr_tab": existing["tab"].get(tfrom) or new_tab_id(),
                "name_match": bool(name_match and tab_name),
                "panes": [],
            }
            for pane in panes_by_tab.get(tid, []):
                src = from_label(session, wid, pane["pane_id"])
                cmd = choose_command(pane, process_infos.get(pane["pane_id"]), shell, shell_only)
                status = "new"
                if src in existing["from"]:
                    status = "exists:" + existing["from"][src]
                elif entry["name_match"] and skip_name_matches:
                    status = "skip:name-match"
                entry["panes"].append(
                    {"pane_id": pane["pane_id"], "from": src, "status": status, **cmd}
                )
            space["tabs"].append(entry)
        plan.append(space)
    return plan


def run_params(space: Dict[str, Any], tab: Dict[str, Any], pane: Dict[str, Any]) -> Dict[str, Any]:
    """terminal.run params for one planned pane."""
    argv = pane["argv"]
    if pane["kind"] != "shell":
        argv = ["sh", "-c", LOGIN_WRAPPER, "sh"] + list(argv)
    return {
        "argv": argv,
        "cwd": pane["cwd"],
        "no_parent": True,
        "no_notify": True,
        "keep": True,
        "labels": {
            "herdr.space": space["space_id"],
            "herdr.tab": tab["herdr_tab"],
            LABEL_FROM: pane["from"],
            LABEL_TAB_FROM: tab["tab_from"],
            LABEL_SPACE_NAME: space["name"],
            LABEL_TAB_NAME: tab["name"],
        },
    }


def apply_names(
    doc: Any, space_names: Dict[str, str], tab_names: Dict[str, str]
) -> Tuple[Any, List[str], set, set]:
    """Set names for owned spaces/tabs in the app doc.

    Returns (new_doc, changes, space ids found, tab ids found). Never touches
    entries outside the two maps; never mutates the input.
    """
    new = json.loads(json.dumps(doc)) if isinstance(doc, dict) else {"spaces": []}
    changes: List[str] = []
    found_sp: set = set()
    found_tb: set = set()
    for sp in new.get("spaces", []):
        sid = sp.get("id")
        if sid in space_names:
            found_sp.add(sid)
            if sp.get("name") != space_names[sid]:
                changes.append(f"space {sid}: {sp.get('name')!r} -> {space_names[sid]!r}")
                sp["name"] = space_names[sid]
        for tb in sp.get("tabs", []):
            tid = tb.get("id")
            if tid in tab_names:
                found_tb.add(tid)
                if tab_names[tid] and tb.get("name") != tab_names[tid]:
                    changes.append(f"tab {tid}: {tb.get('name')!r} -> {tab_names[tid]!r}")
                    tb["name"] = tab_names[tid]
    return new, changes, found_sp, found_tb


def names_from_terminals(
    terminals: Sequence[Dict[str, Any]],
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Migrated terminals' labels -> ({space id: name}, {tab id: name})."""
    spaces: Dict[str, str] = {}
    tabs: Dict[str, str] = {}
    for t in terminals:
        lb = t.get("labels") or {}
        if not lb.get(LABEL_FROM, "").startswith(FROM_PREFIX) or "/" in t.get("address", "/"):
            continue
        if lb.get("herdr.space") and lb.get(LABEL_SPACE_NAME):
            spaces[lb["herdr.space"]] = lb[LABEL_SPACE_NAME]
        if lb.get("herdr.tab") and lb.get(LABEL_TAB_NAME):
            tabs[lb["herdr.tab"]] = lb[LABEL_TAB_NAME]
    return spaces, tabs


# =============================================================================
# External processes
# =============================================================================


def hc_env(session: str) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in HC_SCOPE_VARS}
    env["HC_SESSION"] = session
    return env


def core_socket_exists(session: str) -> Optional[bool]:
    run = os.environ.get("XDG_RUNTIME_DIR")
    if not run:
        return None
    return os.path.exists(os.path.join(run, "herdr-core", f"{session}.sock"))


class Hc:
    """Minimal JSON-RPC 2.0 client over `hc connect` (newline-delimited JSON)."""

    def __init__(self, session: str):
        self.proc = subprocess.Popen(
            ["hc", "connect", "--session", session],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=hc_env(session),
            text=True,
            bufsize=1,
        )
        self._id = 0

    def call(self, method: str, params: Dict[str, Any], timeout: float = 60.0) -> Any:
        self._id += 1
        rid = self._id
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n"
        )
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise HcError("timeout", f"{method} got no answer in {timeout}s")
            ready, _, _ = select.select([self.proc.stdout], [], [], left)
            if not ready:
                continue
            line = self.proc.stdout.readline()
            if not line:
                err = self.proc.stderr.read() if self.proc.stderr else ""
                raise HcError("disconnected", f"hc connect ended: {err.strip()}")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") != rid:
                continue
            if "error" in msg:
                e = msg["error"] or {}
                raise HcError((e.get("data") or {}).get("kind", "error"), e.get("message", ""))
            return msg.get("result")

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def herdr_json(args: List[str]) -> Any:
    out = subprocess.run(["herdr"] + args, capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(f"herdr {' '.join(args)} failed: {out.stderr.strip()}")
    return json.loads(out.stdout)


def tui_sessions() -> List[Tuple[str, str]]:
    out = subprocess.run(["herdr", "session", "list"], capture_output=True, text=True, timeout=30)
    rows = []
    for line in out.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            rows.append((parts[0], parts[1]))
    return rows


def process_info(session: str, pane_id: str) -> Optional[Dict[str, Any]]:
    try:
        data = herdr_json(["--session", session, "pane", "process-info", "--pane", pane_id])
    except Exception:
        return None
    body = data.get("result", data)
    return body.get("process_info", body)


# =============================================================================
# Commands
# =============================================================================


def list_all(hc: Hc) -> List[Dict[str, Any]]:
    return (hc.call("terminal.list", {"all": True}) or {}).get("terminals", [])


def name_spaces(
    hc: Hc, space_names: Dict[str, str], tab_names: Dict[str, str], wait: float
) -> bool:
    """CAS-write names into the app doc, polling until owned spaces appear."""
    deadline = time.time() + wait
    while True:
        doc = hc.call("doc.get", {"name": APP_DOC})
        new, changes, found_sp, found_tb = apply_names(doc.get("value"), space_names, tab_names)
        if changes:
            try:
                hc.call(
                    "doc.set", {"name": APP_DOC, "value": new, "version": doc.get("version", 0)}
                )
                for c in changes:
                    print(f"  named {c}")
            except HcError as e:
                if e.kind == "conflict" and time.time() < deadline + 10:
                    time.sleep(0.1)
                    continue  # someone (the app) wrote in between; re-read
                raise
        missing_sp = set(space_names) - found_sp
        missing_tb = {t for t, n in tab_names.items() if n} - found_tb
        if not missing_sp and not missing_tb:
            return True
        if time.time() >= deadline:
            print(
                f"  app has not adopted {len(missing_sp)} space(s) / {len(missing_tb)} tab(s) yet"
            )
            return False
        time.sleep(1.0)


def print_plan(session: str, plan: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {
        "spaces": len(plan),
        "tabs": 0,
        "panes": 0,
        "claude": 0,
        "codex": 0,
        "shell": 0,
        "exists": 0,
        "skip": 0,
        "name_match_tabs": 0,
    }
    for sp in plan:
        print(f"  space {sp['space_id']}  {sp['name']!r}   <- workspace {sp['workspace_id']}")
        for tb in sp["tabs"]:
            counts["tabs"] += 1
            warn = (
                "   [WARN: app already has a space/tab with these names]"
                if tb["name_match"]
                else ""
            )
            counts["name_match_tabs"] += int(tb["name_match"])
            print(f"    tab {tb['herdr_tab']}  {tb['name']!r}   <- tab {tb['tab_id']}{warn}")
            for p in tb["panes"]:
                counts["panes"] += 1
                if p["status"].startswith("exists"):
                    counts["exists"] += 1
                elif p["status"].startswith("skip"):
                    counts["skip"] += 1
                else:
                    counts[p["kind"]] += 1
                print(f"      pane {p['pane_id']}  [{p['status']}]  cwd={p['cwd']}")
                print(f"        {p['note']}")
                print(f"        $ {shlex.join(p['argv'])}")
    return counts


def cmd_migrate(args: argparse.Namespace) -> int:
    rows = tui_sessions()
    running = {name for name, status in rows if status == "running"}
    if args.tui_session:
        sessions = args.tui_session
    else:
        sessions = [n for n, _ in rows if n != CAO_SESSION or args.include_cao]
    shell = os.environ.get("SHELL") or "/bin/bash"

    hc: Optional[Hc] = None
    existing_terms: List[Dict[str, Any]] = []
    doc_value: Any = None
    if args.apply or core_socket_exists(args.hc_session) is not False:
        hc = Hc(args.hc_session)
        existing_terms = list_all(hc)
        doc_value = (hc.call("doc.get", {"name": APP_DOC}) or {}).get("value")
    else:
        print(
            f"note: hc core {args.hc_session!r} is not running; not checking what already exists there"
        )
    existing = existing_placement(existing_terms)
    doc_names = doc_name_index(doc_value)

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"== {mode}: herdr TUI -> hc core {args.hc_session!r} ==")
    totals: List[Tuple[str, Dict[str, int]]] = []
    created_names: Tuple[Dict[str, str], Dict[str, str]] = ({}, {})
    for s in sessions:
        if s not in running:
            print(f"\nTUI session {s!r}: not running; skipped")
            continue
        snap = snapshot_body(herdr_json(["--session", s, "api", "snapshot"]))
        infos = {p["pane_id"]: process_info(s, p["pane_id"]) for p in snap.get("panes", [])}
        plan = build_plan(
            s,
            snap,
            infos,
            existing,
            shell,
            args.shell_only,
            set(args.workspace) if args.workspace else None,
            doc_names,
            args.skip_name_matches,
        )
        print(f"\nTUI session {s!r}:")
        counts = print_plan(s, plan)
        totals.append((s, counts))
        if args.apply and hc:
            for sp in plan:
                for tb in sp["tabs"]:
                    for p in tb["panes"]:
                        if p["status"] != "new":
                            continue
                        try:
                            res = hc.call("terminal.run", run_params(sp, tb, p))
                            print(
                                f"  created {res.get('address')} for {p['pane_id']} ({p['kind']})"
                            )
                            created_names[0][sp["space_id"]] = sp["name"]
                            created_names[1][tb["herdr_tab"]] = tb["name"]
                        except HcError as e:
                            print(f"  FAILED {p['pane_id']}: {e}")

    print("\n== Summary ==")
    for s, c in totals:
        print(
            f"  {s}: {c['spaces']} spaces, {c['tabs']} tabs, {c['panes']} panes -> "
            f"{c['claude']} resume claude, {c['codex']} resume codex, {c['shell']} shell, "
            f"{c['exists']} already migrated, {c['skip']} skipped by name"
            + (
                f"; {c['name_match_tabs']} tab(s) already named in the app"
                if c["name_match_tabs"]
                else ""
            )
        )
    if any(c["claude"] + c["codex"] for _, c in totals):
        print(
            "  NOTE: resumed agents run a second copy of the same session while the TUI copy is still"
            " alive; exit the TUI copy before you type into the app copy."
        )

    if args.apply and hc:
        if created_names[0]:
            print("\n== Naming spaces and tabs ==")
            ok = name_spaces(hc, created_names[0], created_names[1], args.name_wait)
            if not ok:
                print("  Open the Herdr app on this core, then finish naming with:")
                print(
                    f"    {shlex.join([sys.argv[0], '--rename-only', '--hc-session', args.hc_session])}"
                )
        hc.close()
    elif not args.apply:
        print("\nDry run only. Re-run with --apply to create the terminals.")
        if hc:
            hc.close()
    if totals:
        print(
            "\nWhen the app looks right, close the TUI sessions yourself (this ends their programs):"
        )
        for s, _ in totals:
            print(f"    herdr session stop {s}        # or: herdr --session {s} server stop")
    return 0


def cmd_rename(args: argparse.Namespace) -> int:
    hc = Hc(args.hc_session)
    try:
        spaces, tabs = names_from_terminals(list_all(hc))
        if not spaces:
            print("no migrated terminals found in this core")
            return 1
        print(f"naming {len(spaces)} space(s) and {len(tabs)} tab(s)")
        ok = name_spaces(hc, spaces, tabs, args.name_wait)
        return 0 if ok else 2
    finally:
        hc.close()


# =============================================================================
# Self-test
# =============================================================================


def self_test() -> int:
    sid = "c4c618a8-c1a2-48f0-a12a-98d465d3b3db"
    # claude: keeps flags, replaces --resume, drops -c and prompt
    argv, notes = rewrite_claude(
        [
            "/x/claude",
            "--settings",
            "/s.json",
            "--effort",
            "medium",
            "--dangerously-skip-permissions",
            "--resume",
            "old",
            "--agent",
            "A",
        ],
        sid,
    )
    assert argv == [
        "/x/claude",
        "--settings",
        "/s.json",
        "--effort",
        "medium",
        "--dangerously-skip-permissions",
        "--agent",
        "A",
        "--resume",
        sid,
    ], argv
    assert notes == [], notes
    argv, notes = rewrite_claude(
        ["claude", "-c", "--add-dir", "a", "b", "--model=opus", "fix it"], sid
    )
    assert argv == ["claude", "--add-dir", "a", "b", "--model=opus", "--resume", sid], argv
    assert any("-c" in n for n in notes) and any("fix it" in n for n in notes), notes
    argv, _ = rewrite_claude(["claude", "-r", "--verbose"], sid)
    assert argv == ["claude", "--verbose", "--resume", sid], argv
    argv, _ = rewrite_claude(["node", "/p/claude.js", "--session-id", "u", "-w", "wt"], sid)
    assert argv == ["node", "/p/claude.js", "--resume", sid], argv
    # codex
    argv, _ = rewrite_codex(["codex", "-m", "gpt-5", "--search", "do x"], sid)
    assert argv == ["codex", "resume", sid, "-m", "gpt-5", "--search", "--no-daemon"], argv
    argv, _ = rewrite_codex(["/b/codex", "resume", "old-id", "--no-daemon", "-a", "never"], sid)
    assert argv == ["/b/codex", "resume", sid, "-a", "never", "--no-daemon"], argv
    argv, _ = rewrite_codex(["codex", "resume", "--last", "-c", "k=v"], sid)
    assert argv == ["codex", "resume", sid, "-c", "k=v", "--no-daemon"], argv
    argv, notes = rewrite_codex(["codex", "exec", "hello"], sid)
    assert argv is None and notes, notes
    # leader argv
    pi = {
        "foreground_process_group_id": 2,
        "foreground_processes": [
            {"pid": 3, "argv": ["/inner/claude"]},
            {"pid": 2, "argv": ["/outer/claude", "--x"]},
        ],
    }
    assert leader_argv(pi) == ["/outer/claude", "--x"]
    assert leader_argv(None) is None
    # choose_command
    pane = {
        "pane_id": "w:p1",
        "cwd": "/a",
        "foreground_cwd": "/b",
        "agent": "claude",
        "agent_session": {"agent": "claude", "kind": "id", "value": sid},
    }
    c = choose_command(pane, pi, "/bin/bash", False)
    assert c["kind"] == "claude" and c["argv"][-2:] == ["--resume", sid] and c["cwd"] == "/b", c
    assert choose_command(pane, pi, "/bin/bash", True)["kind"] == "shell"
    bash = {
        "foreground_process_group_id": 9,
        "foreground_processes": [{"pid": 9, "argv": ["-bash"]}],
    }
    c = choose_command(pane, bash, "/bin/bash", False)
    assert c["kind"] == "shell" and "--resume " + sid in c["note"], c
    c = choose_command({"pane_id": "p", "cwd": "/a"}, bash, "/bin/zsh", False)
    assert c == {
        "kind": "shell",
        "argv": ["/bin/zsh", "-l"],
        "cwd": "/a",
        "note": "login shell; foreground -bash is not a resumable agent",
    }, c
    c = choose_command({**pane, "agent_session": None}, pi, "/bin/bash", False)
    assert c["kind"] == "shell" and "no agent session id" in c["note"]
    c = choose_command(
        {**pane, "agent": "codex", "agent_session": {"kind": "id", "value": sid}},
        {
            "foreground_process_group_id": 1,
            "foreground_processes": [{"pid": 1, "argv": ["codex", "--search"]}],
        },
        "/bin/bash",
        False,
    )
    assert c["argv"] == ["codex", "resume", sid, "--search", "--no-daemon"], c
    # snapshot -> plan
    snap = snapshot_body(
        {
            "result": {
                "snapshot": {
                    "workspaces": [
                        {"workspace_id": "wB", "label": "two", "number": 2},
                        {"workspace_id": "wA", "label": "one", "number": 1},
                    ],
                    "tabs": [
                        {"tab_id": "wA:t2", "workspace_id": "wA", "label": "y", "number": 2},
                        {"tab_id": "wA:t1", "workspace_id": "wA", "label": "x", "number": 1},
                        {"tab_id": "wB:t1", "workspace_id": "wB", "label": "z", "number": 1},
                    ],
                    "panes": [
                        {"pane_id": "wA:p1", "tab_id": "wA:t1", "workspace_id": "wA", "cwd": "/1"},
                        {"pane_id": "wA:p2", "tab_id": "wA:t1", "workspace_id": "wA", "cwd": "/2"},
                        {"pane_id": "wA:p3", "tab_id": "wA:t2", "workspace_id": "wA", "cwd": "/3"},
                        {"pane_id": "wB:p1", "tab_id": "wB:t1", "workspace_id": "wB", "cwd": "/4"},
                    ],
                }
            }
        }
    )
    empty = existing_placement([])
    plan = build_plan("s", snap, {}, empty, "/bin/bash")
    assert [sp["name"] for sp in plan] == ["one", "two"]
    assert [t["name"] for t in plan[0]["tabs"]] == ["x", "y"]
    assert len(plan[0]["tabs"][0]["panes"]) == 2
    assert plan[0]["space_id"].startswith("sp") and len(plan[0]["space_id"]) == 34
    assert (
        plan[0]["tabs"][0]["herdr_tab"].startswith("tb")
        and len(plan[0]["tabs"][0]["herdr_tab"]) == 34
    )
    assert plan[0]["space_id"] != plan[1]["space_id"]
    assert plan[0]["tabs"][0]["panes"][0]["from"] == "herdr-tui:s/wA/wA:p1"
    assert all(p["status"] == "new" for sp in plan for t in sp["tabs"] for p in t["panes"])
    assert [
        sp["name"] for sp in build_plan("s", snap, {}, empty, "/bin/bash", workspaces_filter={"wB"})
    ] == ["two"]
    rp = run_params(plan[0], plan[0]["tabs"][0], plan[0]["tabs"][0]["panes"][0])
    assert (
        rp["argv"] == ["/bin/bash", "-l"]
        and rp["no_parent"]
        and rp["no_notify"]
        and rp["cwd"] == "/1"
    )
    assert rp["labels"]["herdr.space"] == plan[0]["space_id"]
    assert rp["labels"][LABEL_FROM] == "herdr-tui:s/wA/wA:p1"
    agent_pane = {"kind": "claude", "argv": ["claude", "--resume", sid], "cwd": "/1", "from": "f"}
    assert run_params(plan[0], plan[0]["tabs"][0], agent_pane)["argv"][:4] == [
        "sh",
        "-c",
        LOGIN_WRAPPER,
        "sh",
    ]
    # idempotency: existing labels reused / skipped
    terms = [
        {
            "address": "t9",
            "labels": {
                LABEL_FROM: "herdr-tui:s/wA/wA:p1",
                LABEL_TAB_FROM: "herdr-tui:s/wA:t1",
                "herdr.space": "spEXIST",
                "herdr.tab": "tbEXIST",
            },
        },
        {"address": "m/t1", "labels": {LABEL_FROM: "herdr-tui:s/wB/wB:p1"}},
        {"address": "t3", "labels": {"herdr.space": "spOTHER"}},
    ]
    ex = existing_placement(terms)
    assert ex["from"] == {"herdr-tui:s/wA/wA:p1": "t9"}, ex
    plan2 = build_plan("s", snap, {}, ex, "/bin/bash")
    assert plan2[0]["space_id"] == "spEXIST" and plan2[0]["tabs"][0]["herdr_tab"] == "tbEXIST"
    st = [p["status"] for p in plan2[0]["tabs"][0]["panes"]]
    assert st == ["exists:t9", "new"], st
    assert plan2[1]["tabs"][0]["panes"][0]["status"] == "new"  # remote machine terminal ignored
    # name-match detection
    names = doc_name_index({"spaces": [{"name": "One", "tabs": [{"name": "x"}]}]})
    plan3 = build_plan("s", snap, {}, empty, "/bin/bash", doc_names=names, skip_name_matches=True)
    assert plan3[0]["tabs"][0]["name_match"] and not plan3[0]["tabs"][1]["name_match"]
    assert plan3[0]["tabs"][0]["panes"][0]["status"] == "skip:name-match"
    # doc naming mutation
    doc = {
        "spaces": [
            {
                "id": "spA",
                "name": "folder",
                "folder": "/f",
                "tabs": [
                    {
                        "id": "tbA",
                        "name": "",
                        "root": {"type": "block", "content": {"terminal": "t5"}},
                    },
                    {"id": "tbU", "name": "user", "root": {}},
                ],
            },
            {"id": "spU", "name": "theirs", "tabs": [{"id": "tbX", "name": "keep"}]},
        ]
    }
    before = json.dumps(doc, sort_keys=True)
    new, changes, fsp, ftb = apply_names(
        doc, {"spA": "one", "spMissing": "m"}, {"tbA": "x", "tbGone": "g"}
    )
    assert json.dumps(doc, sort_keys=True) == before, "input mutated"
    assert new["spaces"][0]["name"] == "one" and new["spaces"][0]["tabs"][0]["name"] == "x"
    assert new["spaces"][0]["tabs"][1]["name"] == "user" and new["spaces"][1]["name"] == "theirs"
    assert new["spaces"][0]["tabs"][0]["root"] == doc["spaces"][0]["tabs"][0]["root"]
    assert len(changes) == 2 and fsp == {"spA"} and ftb == {"tbA"}, (changes, fsp, ftb)
    _, changes, _, _ = apply_names(new, {"spA": "one"}, {"tbA": "x"})
    assert changes == []
    assert apply_names(None, {"spA": "one"}, {})[2] == set()
    sn, tn = names_from_terminals(
        [
            {
                "address": "t1",
                "labels": {
                    LABEL_FROM: "herdr-tui:s/wA/wA:p1",
                    "herdr.space": "spA",
                    "herdr.tab": "tbA",
                    LABEL_SPACE_NAME: "one",
                    LABEL_TAB_NAME: "x",
                },
            },
            {"address": "t2", "labels": {"herdr.space": "spZ"}},
        ]
    )
    assert sn == {"spA": "one"} and tn == {"tbA": "x"}

    # CAS loop: a concurrent writer causes one conflict, then the retry wins
    class FakeHc:
        def __init__(self) -> None:
            self.doc = {"spaces": [{"id": "spA", "name": "", "tabs": [{"id": "tbA", "name": ""}]}]}
            self.version, self.sets, self.raced = 3, 0, False

        def call(self, method: str, params: Dict[str, Any]) -> Any:
            if method == "doc.get":
                return {"value": json.loads(json.dumps(self.doc)), "version": self.version}
            self.sets += 1
            if not self.raced:  # the app writes first
                self.raced, self.version = True, self.version + 1
                self.doc["spaces"].append({"id": "spNew", "name": "app", "tabs": []})
            if params["version"] != self.version:
                raise HcError("conflict", "stale")
            self.doc, self.version = params["value"], self.version + 1
            return {"version": self.version}

    fake = FakeHc()
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()):
        assert name_spaces(fake, {"spA": "one"}, {"tbA": "x"}, 0)  # type: ignore[arg-type]
    assert fake.sets == 2 and fake.doc["spaces"][0]["name"] == "one", fake.doc
    assert (
        fake.doc["spaces"][0]["tabs"][0]["name"] == "x" and fake.doc["spaces"][1]["name"] == "app"
    )
    # env scrub
    saved = {k: os.environ.get(k) for k in HC_SCOPE_VARS}
    for k in HC_SCOPE_VARS:
        os.environ[k] = "/run/user/default.sock"
    try:
        env = hc_env("mig")
    finally:
        for k, v in saved.items():
            if v is None:
                del os.environ[k]
            else:
                os.environ[k] = v
    assert not set(HC_SCOPE_VARS) & set(env) and env["HC_SESSION"] == "mig", env
    assert "HC_SOCKET" in HC_SCOPE_VARS
    print("self-test: all checks passed")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--tui-session", action="append", metavar="NAME")
    ap.add_argument("--include-cao", action="store_true")
    ap.add_argument("--workspace", action="append", metavar="ID")
    ap.add_argument("--hc-session", default="default")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--shell-only", action="store_true")
    ap.add_argument("--skip-name-matches", action="store_true")
    ap.add_argument("--name-wait", type=float, default=20.0)
    ap.add_argument("--rename-only", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    if args.rename_only:
        return cmd_rename(args)
    return cmd_migrate(args)


if __name__ == "__main__":
    sys.exit(main())
