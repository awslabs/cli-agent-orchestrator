"""Nothing in the package writes ``CAO_TERMINAL_ID`` except the one forwarder.

``CAO_TERMINAL_ID`` is an AUTHORIZATION input (#671/#972): both MCP gates read it
to decide whether the caller is a bound terminal, gated on its recorded
``allowed_tools``, or an operator context that is not gated at all. A provider
that lets a profile choose the value therefore lets a profile choose its own
authorization posture -- ``env: {CAO_TERMINAL_ID: <another terminal's id>}``
starts CAO's own MCP server as a VALID FOREIGN terminal and both gates authorize
against *that* terminal's allowlist.

This was found and fixed one writer at a time, twice. Round 1 of #1062 converted
four (claude_code, cursor_cli, omp, kimi_cli); independent review then found
three more still preserving a pinned value (copilot_cli, codex,
kimi_runtime_home) and demonstrated the borrowed-allowlist bypass through each.
Enumerating writers by hand is what let the second three survive the first fix,
so this test ENUMERATES THEM FROM THE SOURCE the way
``test_launch_path_invariant`` and ``test_callsite_lint_rules`` enumerate theirs.

Round 3's version of this guard enumerated *recognised write shapes* under
``providers/`` and ``backends/``. Review 3 executed it and showed that is a game
the guard loses: ``env.update({"CAO_TERMINAL_ID": x})`` was caught, while
``env.update(dict(CAO_TERMINAL_ID=x))``, ``env |= dict(CAO_TERMINAL_ID=x)`` and
``key = "CAO_TERMINAL_ID"; env[key] = x`` all passed, and a helper in
``agent_plugins/mcp_delivery.py`` -- imported by every affected provider -- was
outside the scan root entirely. So the rule is now CONSERVATIVE rather than
clever:

* **Scope is the whole package.** Every ``*.py`` under
  ``src/cli_agent_orchestrator/``, including ``utils/mcp_resolution.py`` itself,
  which is classified in ``ALLOWED`` rather than skipped so a second writer
  added beside the forwarder is still caught.
* **Any OCCURRENCE of the identity key inside a function is a potential write.**
  A string constant ``"CAO_TERMINAL_ID"``, the name ``CAO_TERMINAL_ID_ENV`` (or
  whatever local alias an import gave it), a keyword argument named
  ``CAO_TERMINAL_ID`` (``dict(CAO_TERMINAL_ID=...)``), and any literal or
  f-string rendering ``CAO_TERMINAL_ID=``. No shape list to out-spell: a
  nested ``dict()``, a ``|=``, a ``setdefault``, a ``**{...}`` unpack and a
  variable-held key all put the name somewhere in the function body.
* **Reads are therefore flagged too**, and that is the deliberate cost. A guard
  that distinguishes a read from a write has to recognise the write, which is
  exactly the recognition review 3 defeated. Every read of the caller's OWN
  identity is instead named in ``ALLOWED`` with its reason, and
  ``ALLOWED_OCCURRENCES`` pins how many occurrences each classified site has, so
  a hand-rolled write added beside a legitimate read fails here.
* **Per write, not per function.** A function that forwards is clean only if it
  hand-rolls nothing: a ``forward_terminal_identity`` call elsewhere in the body
  does not excuse an occurrence. Function granularity was round 3's first cut
  and reintroducing copilot's ``env.setdefault`` passed under it.

Docstrings and bare string-expression statements are the one exemption, because
a string Python discards cannot write anything and the name appears in prose all
over these modules. Comments never reach the AST at all.

A site is CLEAN when its enclosing function calls
``mcp_resolution.forward_terminal_identity`` -- the one helper that makes the
launching terminal's own id win and logs a profile that tried to pin one -- and
contains no identity occurrence of its own. Every other site must appear in
``ALLOWED``. A new writer fails this test until it forwards or is classified
here; a classified site that disappears, starts forwarding, or grows an
occurrence fails it too, so the list cannot go stale.

This is a structural guard over the SHAPE of every site. The behavioural
guarantee -- that a pinned ``''``/``other-id``/``deadbeef`` really is replaced by
the launching terminal's id in each provider's real output -- is
``test/providers/test_terminal_identity_forwarding.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "cli_agent_orchestrator"

#: The env var itself, and the module constant that names it.
IDENTITY_NAMES = {"CAO_TERMINAL_ID", "CAO_TERMINAL_ID_ENV"}

#: Calling either of these is what makes a function an MCP-config writer, even
#: when it never names the identity: it is building a launch entry, which is the
#: thing an identity belongs in.
MCP_WRITER_CALLS = {"resolve_mcp_server_config", "resolve_cao_mcp_command"}

#: The one helper that settles the identity.
FORWARDER = "forward_terminal_identity"

# Reasons shared by a whole category of site. Repeating one verbatim twelve
# times would read as twelve separate judgements; naming the category once makes
# it reviewable, and each site is still listed individually so it cannot go
# stale or be joined silently.

READS_OWN_IDENTITY = (
    "reads the CALLER'S OWN identity out of its own process environment to "
    "answer 'which terminal am I', which is the intended consumer of the "
    "variable; it never puts the key into a dict handed to a child process"
)

EXPORTS_REAL_ID = (
    "exports the REAL terminal id into the environment of the pane/process CAO "
    "is itself creating, assigned AFTER any operator-forwarded env so a "
    "forwarded CAO_TERMINAL_ID cannot win; not an MCP server entry"
)

RESOLVES_COMMAND_ONLY = (
    "resolves the bundled cao-mcp-server command/args and never touches `env`; "
    "only the caller knows which terminal it is launching for, so forwarding is "
    "the caller's job and every caller of this does it"
)

#: Sites that may name, read, write or omit the identity without the forwarder,
#: each with the reason. Key = ``<path under cli_agent_orchestrator/>::<function>``.
ALLOWED: Dict[str, str] = {
    # -- the forwarder's own module ---------------------------------------
    "utils/mcp_resolution.py::<module>": (
        "defines CAO_TERMINAL_ID_ENV, the single place the key is spelled; "
        "every other module is expected to import it from here"
    ),
    "utils/mcp_resolution.py::forward_terminal_identity": (
        "IS the forwarder: the one function allowed to decide this value, which "
        "it sets to the launching terminal's own id after logging any pinned one"
    ),
    "utils/mcp_resolution.py::resolve_mcp_server_config": RESOLVES_COMMAND_ONLY,
    # -- MCP-config writers that legitimately do not forward ---------------
    "agent_plugins/mcp_delivery.py::_deliverable": (
        "normalizes a plugin/profile entry's command, args and cwd at the seam "
        "shared by install and launch; it has no terminal id to forward (install "
        "time has no terminal) and every provider forwards downstream of it"
    ),
    "providers/minimax_code.py::_serialize_server": (
        "a staticmethod that assembles one entry's env (including PATH for an "
        "absolute command); the identity is written by `_write_plugin`, which "
        "forwards every stdio entry it serialized"
    ),
    "utils/opencode_config.py::translate_mcp_server_config": (
        "install-time translation into opencode.json, run before any terminal "
        "exists, so there is no real id to forward; OpenCode's MCP children "
        "inherit the identity from the pane env instead"
    ),
    # -- providers that deliberately do not let the forwarder decide -------
    "providers/antigravity_cli.py::_register_mcp_servers": (
        "stricter rule by design (#940/#958): agy's mcp_config.json is SHARED by "
        "every terminal, so the key is DROPPED and inherited from each agy "
        "process instead of pinned to any one terminal"
    ),
    "providers/antigravity_cli.py::_is_pinned_terminal_entry": (
        "the DETECTOR for the rule above: reads whether a shared-config entry "
        "already pins an identity, so that the entry can be rewritten"
    ),
    "providers/codex.py::_build_codex_command": (
        "forwards into the `env` table (the value Codex actually passes), and "
        "additionally names the key in Codex's `env_vars` inheritance list, "
        "which requests the REAL parent value and can hold no foreign one"
    ),
    "providers/kimi_cli.py::_build_kimi_code_command": (
        "exports the REAL id on the launch line (`env CAO_TERMINAL_ID=<self>`) "
        "for Kimi Code's children to inherit; the per-server override lives in "
        "kimi_runtime_home.merge_mcp_servers, which this hands its terminal id to"
    ),
    # -- exports of the real id into a process CAO creates -----------------
    "clients/tmux.py::create_session": EXPORTS_REAL_ID,
    "clients/tmux.py::create_window": EXPORTS_REAL_ID,
    "clients/tmux.py::create_pane": EXPORTS_REAL_ID,
    "backends/herdr_backend.py::_build_env_args": EXPORTS_REAL_ID,
    # -- reads of the caller's own identity --------------------------------
    "cli/commands/workflow.py::_attach_terminal_caller": READS_OWN_IDENTITY,
    "mcp_server/server.py::_attach_workflow_caller": READS_OWN_IDENTITY,
    "mcp_server/server.py::_create_ephemeral_from_arguments": READS_OWN_IDENTITY,
    "mcp_server/server.py::_own_terminal_id_or_error": READS_OWN_IDENTITY,
    "mcp_server/server.py::_send_terminal_input": READS_OWN_IDENTITY,
    "mcp_server/server.py::_send_user_prompt_answer": READS_OWN_IDENTITY,
    "mcp_server/server.py::_workflow_default_root": READS_OWN_IDENTITY,
    "mcp_server/server.py::assign_elastic": READS_OWN_IDENTITY,
    "mcp_server/server.py::emit_ui": READS_OWN_IDENTITY,
    "security/mcp_tool_gate.py::caller_is_bound": READS_OWN_IDENTITY,
    "services/vault/reader.py::_resolve_injection_policy": READS_OWN_IDENTITY,
    "utils/orchestration.py::_current_terminal_id": READS_OWN_IDENTITY,
    "utils/orchestration.py::_local_claim_refusal": READS_OWN_IDENTITY,
    "utils/orchestration.py::_remote_ephemeral_refusal": READS_OWN_IDENTITY,
    "utils/orchestration.py::_send_direct_input": READS_OWN_IDENTITY,
}

#: How many identity occurrences each classified site currently has. A reason on
#: a whole function would otherwise excuse a hand-rolled write added NEXT to a
#: legitimate read -- the per-write rule has to survive the allowlist too. When
#: one of these changes, re-read the site and update the number with its reason.
ALLOWED_OCCURRENCES: Dict[str, int] = {
    "agent_plugins/mcp_delivery.py::_deliverable": 0,
    "backends/herdr_backend.py::_build_env_args": 1,
    "cli/commands/workflow.py::_attach_terminal_caller": 1,
    "clients/tmux.py::create_pane": 1,
    "clients/tmux.py::create_session": 1,
    "clients/tmux.py::create_window": 1,
    "mcp_server/server.py::_attach_workflow_caller": 1,
    "mcp_server/server.py::_create_ephemeral_from_arguments": 1,
    "mcp_server/server.py::_own_terminal_id_or_error": 1,
    "mcp_server/server.py::_send_terminal_input": 1,
    "mcp_server/server.py::_send_user_prompt_answer": 1,
    "mcp_server/server.py::_workflow_default_root": 1,
    "mcp_server/server.py::assign_elastic": 1,
    "mcp_server/server.py::emit_ui": 1,
    "providers/antigravity_cli.py::_is_pinned_terminal_entry": 1,
    "providers/antigravity_cli.py::_register_mcp_servers": 2,
    "providers/codex.py::_build_codex_command": 2,
    "providers/kimi_cli.py::_build_kimi_code_command": 2,
    "providers/minimax_code.py::_serialize_server": 0,
    "security/mcp_tool_gate.py::caller_is_bound": 1,
    "services/vault/reader.py::_resolve_injection_policy": 1,
    "utils/mcp_resolution.py::<module>": 2,
    "utils/mcp_resolution.py::forward_terminal_identity": 3,
    "utils/mcp_resolution.py::resolve_mcp_server_config": 0,
    "utils/opencode_config.py::translate_mcp_server_config": 0,
    "utils/orchestration.py::_current_terminal_id": 1,
    "utils/orchestration.py::_local_claim_refusal": 1,
    "utils/orchestration.py::_remote_ephemeral_refusal": 1,
    "utils/orchestration.py::_send_direct_input": 1,
}

#: Sites that MUST be found, so a broken walker cannot pass vacuously: one of
#: each kind, one of each disposition, and one per module now in scope.
SENTINELS = {
    "agent_plugins/mcp_delivery.py::_deliverable",
    "backends/herdr_backend.py::_build_env_args",
    "cli/commands/workflow.py::_attach_terminal_caller",
    "clients/tmux.py::create_pane",
    "mcp_server/server.py::_own_terminal_id_or_error",
    "providers/antigravity_cli.py::_register_mcp_servers",
    "providers/claude_code.py::_build_claude_command",
    "providers/codex.py::_build_codex_command",
    "providers/copilot_cli.py::_build_runtime_mcp_config",
    "providers/cursor_cli.py::_write_plugin_dir",
    "providers/grok_cli.py::_render_mcp_config",
    "providers/kimi_cli.py::_build_kimi_code_command",
    "providers/kimi_cli.py::_build_kimi_command",
    "providers/kimi_runtime_home.py::merge_mcp_servers",
    "providers/minimax_code.py::_serialize_server",
    "providers/minimax_code.py::_write_plugin",
    "providers/omp.py::_write_extension_root",
    "security/mcp_tool_gate.py::caller_is_bound",
    "services/vault/reader.py::_resolve_injection_policy",
    "utils/mcp_resolution.py::forward_terminal_identity",
    "utils/opencode_config.py::translate_mcp_server_config",
    "utils/orchestration.py::_current_terminal_id",
}


def _callee(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _local_identity_names(tree: ast.Module) -> Set[str]:
    """``IDENTITY_NAMES`` plus any local alias an import gave one of them.

    ``from ...mcp_resolution import CAO_TERMINAL_ID_ENV as TERMINAL_KEY`` leaves
    the recognisable name only on the import statement; the writes spell
    ``TERMINAL_KEY``. Resolving the alias is what keeps "rename the constant"
    from being an escape hatch.
    """
    names = set(IDENTITY_NAMES)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.asname and alias.name.split(".")[-1] in IDENTITY_NAMES:
                    names.add(alias.asname)
    return names


def _discarded_strings(tree: ast.Module) -> Set[int]:
    """Every string that is a bare expression statement -- a docstring.

    Python evaluates and throws these away, so one cannot write anything. This
    is the only exemption in the walker, and it is structural rather than a
    spelling judgement: the name appears in prose throughout these modules and a
    guard that went red on a doc edit would be edited out.
    """
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


def _identity_occurrence(node: ast.AST, names: Set[str]) -> Optional[str]:
    """Classify one node as naming the identity key, or return None.

    Any occurrence counts. The walker does NOT try to tell a read from a write,
    because recognising the write is the part review 3 defeated with four
    spellings of the same assignment.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        if node.value in IDENTITY_NAMES:
            return "key string"
        if any(f"{name}=" in node.value for name in IDENTITY_NAMES):
            return "env string"
    if isinstance(node, ast.Name) and node.id in names:
        return "key constant"
    if isinstance(node, ast.Attribute) and node.attr in names:
        return "key constant"
    if isinstance(node, ast.keyword) and node.arg in IDENTITY_NAMES:
        return "keyword key"
    if isinstance(node, ast.JoinedStr):
        rendered = "".join(
            str(part.value) for part in node.values if isinstance(part, ast.Constant)
        )
        if any(f"{name}=" in rendered for name in IDENTITY_NAMES):
            return "env string"
    return None


def _owner_map(tree: ast.Module) -> Dict[int, str]:
    """Every node's nearest enclosing function name (``<module>`` if none)."""
    owner: Dict[int, str] = {}

    def visit(node: ast.AST, current: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = (
                child.name
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                else current
            )
            owner[id(child)] = inner
            visit(child, inner)

    visit(tree, "<module>")
    return owner


def sites_in_source(source: str, rel: str) -> Dict[str, dict]:
    """Every identity-naming and MCP-writer site in one module's source."""
    tree = ast.parse(source, filename=rel)
    owner = _owner_map(tree)
    names = _local_identity_names(tree)
    discarded = _discarded_strings(tree)
    forwarding: Set[str] = {
        owner.get(id(node), "<module>")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _callee(node) == FORWARDER
    }
    found: Dict[str, dict] = {}
    for node in ast.walk(tree):
        kind = None if id(node) in discarded else _identity_occurrence(node, names)
        is_occurrence = kind is not None
        if kind is None and isinstance(node, ast.Call):
            called = _callee(node)
            # A forwarding call is itself a site, so every function that settles
            # the identity is enumerated and SENTINELS can pin one that would
            # otherwise vanish from the walk.
            if called in MCP_WRITER_CALLS or called == FORWARDER:
                kind = f"{called}()"
        if kind is None:
            continue
        function = owner.get(id(node), "<module>")
        entry = found.setdefault(
            f"{rel}::{function}",
            {
                "kinds": set(),
                "occurrences": 0,
                "line": getattr(node, "lineno", 0),
                "forwards": function in forwarding,
            },
        )
        entry["kinds"].add(kind)
        if is_occurrence:
            entry["occurrences"] += 1
        entry["line"] = min(entry["line"], getattr(node, "lineno", entry["line"]))
    return found


def identity_sites() -> Dict[str, dict]:
    """Every site in the whole package, keyed ``<path>::<function>``."""
    sites: Dict[str, dict] = {}
    for path in sorted(SRC.rglob("*.py")):
        sites.update(sites_in_source(path.read_text(encoding="utf-8"), str(path.relative_to(SRC))))
    return sites


def _is_clean(info: dict) -> bool:
    """Clean = the forwarder settles the identity and nothing else names it.

    A hand-rolled write is NOT excused by a forwarding call elsewhere in the same
    function. Function-level granularity was the first version of this guard and
    it was too coarse: reintroducing the ``env.setdefault`` bypass into
    ``copilot_cli._build_runtime_mcp_config`` still passed, because the same
    function also forwards for CAO's own built-in entry. Nothing but
    ``forward_terminal_identity`` decides this value.
    """
    return not info["occurrences"] and info["forwards"]


SITES = identity_sites()


def _describe(key: str, info: dict) -> str:
    return (
        f"{key} (line {info['line']}: {', '.join(sorted(info['kinds']))}; "
        f"{info['occurrences']} identity occurrence(s))"
    )


def test_the_walker_finds_the_known_sites():
    missing = SENTINELS - set(SITES)
    assert not missing, f"walker lost known MCP identity sites: {sorted(missing)}"
    assert len(SITES) >= 37, sorted(SITES)


def test_every_mcp_identity_writer_forwards_the_real_terminal_id():
    unclassified = sorted(
        _describe(key, info)
        for key, info in SITES.items()
        if not _is_clean(info) and key not in ALLOWED
    )
    assert not unclassified, (
        "site(s) that name CAO_TERMINAL_ID, or build an MCP server entry, "
        f"without letting mcp_resolution.{FORWARDER} settle the identity, and "
        "are not classified in ALLOWED. A profile that pins CAO_TERMINAL_ID "
        "here chooses the authorization identity CAO's own MCP server runs "
        "under (#1062). Route the write through the forwarder, or add the site "
        "to ALLOWED and ALLOWED_OCCURRENCES with a one-line reason:\n  " + "\n  ".join(unclassified)
    )


def test_the_allowlist_is_real_and_still_needed():
    stale = sorted(key for key in ALLOWED if key not in SITES)
    assert not stale, f"ALLOWED names sites that no longer exist: {stale}"
    now_forwarding = sorted(key for key in ALLOWED if _is_clean(SITES[key]))
    assert not now_forwarding, (
        "these sites now forward the identity; drop them from ALLOWED so the "
        f"classification stays honest: {now_forwarding}"
    )
    for key, why in ALLOWED.items():
        assert len(why.split()) >= 8, f"{key}: justification too short"


def test_an_allowlisted_site_may_not_grow_a_new_identity_occurrence():
    assert sorted(ALLOWED_OCCURRENCES) == sorted(ALLOWED), (
        "every ALLOWED site needs a pinned occurrence count, and vice versa: "
        f"{sorted(set(ALLOWED) ^ set(ALLOWED_OCCURRENCES))}"
    )
    drifted = sorted(
        f"{key}: pinned {expected}, found {SITES[key]['occurrences']} "
        f"({', '.join(sorted(SITES[key]['kinds']))})"
        for key, expected in ALLOWED_OCCURRENCES.items()
        if SITES[key]["occurrences"] != expected
    )
    assert not drifted, (
        "an allowlisted site's reason covers the occurrences it had when it was "
        "written; one of these gained or lost one, so a hand-rolled identity "
        "write may now be hiding behind a reason given for a read. Re-read the "
        "site and update the count:\n  " + "\n  ".join(drifted)
    )


# One snippet per shape review 3 showed the round-3 walker missing, plus the
# shapes that would be the next thing to try. Each must produce a site that is
# NOT clean, so the conservative rule is tested rather than asserted.
MISSED_SHAPES: List[Tuple[str, str, str]] = [
    (
        "dict literal passed to update",
        "snippet.py::f",
        "def f(env, x):\n    env.update({'CAO_TERMINAL_ID': x})\n",
    ),
    (
        "dict() keyword passed to update",
        "snippet.py::f",
        "def f(env, x):\n    env.update(dict(CAO_TERMINAL_ID=x))\n",
    ),
    (
        "in-place merge of a dict() keyword",
        "snippet.py::f",
        "def f(env, x):\n    env |= dict(CAO_TERMINAL_ID=x)\n",
    ),
    (
        "variable-held key",
        "snippet.py::f",
        "def f(env, x):\n    key = 'CAO_TERMINAL_ID'\n    env[key] = x\n",
    ),
    (
        "double-star unpack of a dict literal",
        "snippet.py::f",
        "def f(env, x):\n    env.update(**{'CAO_TERMINAL_ID': x})\n",
    ),
    (
        "setdefault, the original bypass",
        "snippet.py::f",
        "def f(env, x):\n    env.setdefault('CAO_TERMINAL_ID', x)\n",
    ),
    (
        "f-string env pair",
        "snippet.py::f",
        "def f(parts, x):\n    parts.append(f'CAO_TERMINAL_ID={x}')\n",
    ),
    (
        "percent-formatted env pair",
        "snippet.py::f",
        "def f(parts, x):\n    parts.append('CAO_TERMINAL_ID=%s' % x)\n",
    ),
    (
        "str.format env pair",
        "snippet.py::f",
        "def f(parts, x):\n    parts.append('CAO_TERMINAL_ID={}'.format(x))\n",
    ),
    (
        "the module constant",
        "snippet.py::f",
        "def f(env, x):\n    env[CAO_TERMINAL_ID_ENV] = x\n",
    ),
    (
        "the constant imported under another name",
        "snippet.py::f",
        "from cli_agent_orchestrator.utils.mcp_resolution import (\n"
        "    CAO_TERMINAL_ID_ENV as TERMINAL_KEY,\n"
        ")\n\n\ndef f(env, x):\n    env[TERMINAL_KEY] = x\n",
    ),
    (
        "the constant reached through getattr",
        "snippet.py::f",
        "def f(env, x, mod):\n    env[getattr(mod, 'CAO_TERMINAL_ID_ENV')] = x\n",
    ),
    (
        "a helper in a scanned module that writes the key",
        "snippet.py::_apply_identity",
        "def _apply_identity(env, x):\n    env['CAO_TERMINAL_ID'] = x\n\n\n"
        "def f(cfg, x):\n    _apply_identity(cfg.setdefault('env', {}), x)\n",
    ),
    (
        "a hand-rolled write beside a forwarding call",
        "snippet.py::f",
        "def f(cfg, tid):\n    cfg = forward_terminal_identity(cfg, tid)\n"
        "    cfg['env'] |= dict(CAO_TERMINAL_ID='someone-elses-id')\n",
    ),
]


@pytest.mark.parametrize(
    "shape,expected_key,source",
    MISSED_SHAPES,
    ids=[shape for shape, _, _ in MISSED_SHAPES],
)
def test_the_walker_flags_every_hand_rolled_identity_write(shape, expected_key, source):
    sites = sites_in_source(source, "snippet.py")
    assert expected_key in sites, f"{shape}: walker found no site at all ({sorted(sites)})"
    assert not _is_clean(sites[expected_key]), (
        f"{shape}: walker found the site but called it clean -- "
        f"{_describe(expected_key, sites[expected_key])}"
    )
    assert expected_key not in ALLOWED, f"{shape}: the snippet key collides with ALLOWED"


def test_prose_about_the_identity_is_not_a_site():
    """Otherwise the guard goes red on a doc edit and gets edited out."""
    source = (
        "def f(env):\n"
        '    """Resolves the caller from CAO_TERMINAL_ID.\n\n'
        "    A profile that pinned CAO_TERMINAL_ID=deadbeef used to win here.\n"
        '    """\n'
        "    return env\n"
    )
    assert sites_in_source(source, "snippet.py") == {}


def test_a_function_that_only_forwards_is_clean():
    """The negative control: the rule must still have something to pass."""
    source = (
        "def f(cfg, tid):\n"
        "    cfg = resolve_mcp_server_config(cfg)\n"
        "    return forward_terminal_identity(cfg, tid)\n"
    )
    sites = sites_in_source(source, "snippet.py")
    assert "snippet.py::f" in sites
    assert _is_clean(sites["snippet.py::f"])
