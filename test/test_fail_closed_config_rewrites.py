"""Every JSON config rewrite must fail closed on an unreadable pre-image (GH #737).

The #737 defect class: a read-modify-write cycle whose READ is error-tolerant.
A loader that answers the same empty default for "file is absent" and for "file
is present but could not be read or parsed" is fine for a reader -- it degrades
to built-in defaults -- but when a WRITER consumes it, the writer adds its own
fragment to that empty default and publishes the result as the new truth. Every
other setting in the file is destroyed, the command exits 0, and the only trace
is a ``logger.warning`` nobody sees at the default level.

That shape is invisible in review (the load and the save are usually different
functions, often different commits) and invisible to tests that assert a write
through its own read path. So it is enumerated here instead: this module walks
the package's AST and, among functions that persist JSON through a recognized
sink API (``_PERSIST_ATTRS``, or a write-mode ``open``/``Path.open``) in a
bounded call shape (two hops of resolvable callers/callees), flags every one
whose pre-image came from a lenient load, and pins the set. It is a structural
heuristic over known shapes, not a proof of the property: a sink outside that
vocabulary, or an unresolvable call, is invisible to it. Adding a recognized
sink -- including re-adding a ``_load()`` / edit / ``_save()`` settings writer
-- fails this test until it is either made fail-closed or allowlisted with a
reason.

A handler that catches ONLY absence (``FileNotFoundError``) is not lenient:
absent-means-defaults is exactly the distinction the fix is built on.
"""

import ast
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "cli_agent_orchestrator"

# A write path whose pre-image may be a lenient load, with the reason it is allowed.
# Keys are "<path relative to src/>::<qualified function name>".
ALLOWLIST: Dict[str, str] = {
    "cli_agent_orchestrator/services/config_service.py::_load_raw": (
        "The legacy config.json migration is a write on a read path. It is "
        "suppressed when the settings.json read actually failed (#737); only an "
        "absent file reaches it. Pinned by test_config_service.py's "
        "legacy-migration tests."
    ),
    "cli_agent_orchestrator/services/config_service.py::_get_from_file": (
        "Read-only accessor; reaches a write only through _load_raw's migration, " "above."
    ),
    "cli_agent_orchestrator/providers/kimi_runtime_home.py::"
    "KimiCodeRuntimeHomeBuilder.build": (
        "read_user_mcp_servers() degrades to {} by design, but the write target is "
        "the CAO-owned runtime home, NOT the user file that was read. An "
        "unparseable user mcp.json loses its servers from the CAO copy; the user's "
        "own file is never rewritten."
    ),
    # --- Live instances of the #737 class, found by this guard. Each rewrites the
    # --- same file it read leniently. Recorded, not fixed here: they are separate
    # --- issues (see .sweep/out/guard.md for GH#737).
    "cli_agent_orchestrator/providers/antigravity_cli.py::"
    "AntigravityCliProvider._register_mcp_servers": (
        "LIVE #737-class defect, separate issue: an unparseable "
        "~/.gemini/config/mcp_config.json falls back to {} and is rewritten, "
        "deleting the user's non-CAO servers the docstring promises to preserve."
    ),
    # (``_unregister_mcp_servers`` rewrites the same file and is NOT flagged: its
    # write sits inside the try that swallows, so a parse failure skips the write.
    # Same file, same module, one method safe and one destructive.)
    "cli_agent_orchestrator/providers/claude_code.py::"
    "ClaudeCodeProvider._ensure_skip_bypass_prompt_setting": (
        "LIVE #737-class defect, separate issue: an unparseable "
        "~/.claude/settings.json is swallowed and replaced with "
        '{"skipDangerousModePermissionPrompt": true}, discarding the user\'s own '
        "permissions/env/hooks."
    ),
    "cli_agent_orchestrator/utils/opencode_config.py::_record_grants": (
        "LIVE #737-class defect, separate issue: read_grant_record() degrades a "
        "corrupt sidecar to {} and _write_grant_record then publishes it, dropping "
        "every other agent's recorded grants."
    ),
}

_ABSENT_ONLY = {"FileNotFoundError", "NotADirectoryError", "IsADirectoryError"}
_READ_ATTRS = {"read_text", "read_bytes"}
_PERSIST_ATTRS = {
    "write_text",
    "write_bytes",
    "locked_atomic_write",
    "locked_atomic_rewrite",
    "replace",
}
_WRITE_MODE_CHARS = {"w", "a", "x"}
_PARSE = {"json.load", "json.loads"}
_RENDER = {"json.dump", "json.dumps"}
_Key = Tuple[str, str]


def _dotted(node: ast.AST) -> Optional[str]:
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _call_names(node: ast.AST) -> List[Tuple[str, ast.Call]]:
    out = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = _dotted(child.func)
            if name:
                out.append((name, child))
    return out


def _is_read(name: str) -> bool:
    return name.rsplit(".", 1)[-1] in _READ_ATTRS or name in _PARSE or name == "open"


def _is_persist(name: str) -> bool:
    return name.rsplit(".", 1)[-1] in _PERSIST_ATTRS


def _open_mode(name: str, call: ast.Call) -> Optional[str]:
    # Builtin `open(file, mode, ...)`: mode is the 2nd positional. Method call
    # `path.open(mode, ...)`: the implicit `self` isn't in `call.args`, so mode
    # is the 1st positional.
    mode_index = 1 if "." not in name else 0
    if len(call.args) > mode_index:
        arg = call.args[mode_index]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            return kw.value.value if isinstance(kw.value.value, str) else None
    return None


def _is_write_mode_open(name: str, call: ast.Call) -> bool:
    """``open(path, "w")`` / ``path.open("w")`` -- a sink the named helpers miss."""
    if name.rsplit(".", 1)[-1] != "open":
        return False
    mode = _open_mode(name, call)
    return mode is not None and any(c in _WRITE_MODE_CHARS for c in mode)


def _is_persist_call(name: str, call: ast.Call) -> bool:
    return _is_persist(name) or _is_write_mode_open(name, call)


def _caught(handler: ast.ExceptHandler) -> Set[str]:
    if handler.type is None:
        return {"BaseException"}
    items = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {_dotted(i) or "?" for i in items}


def _is_lenient_handler(handler: ast.ExceptHandler) -> bool:
    """Swallows the failure AND catches more than mere absence."""
    if any(isinstance(s, ast.Raise) for s in ast.walk(handler)):
        return False
    return not (_caught(handler) <= _ABSENT_ONLY)


def _collect_functions() -> Dict[_Key, ast.AST]:
    funcs: Dict[_Key, ast.AST] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - not in-tree
            continue
        module = str(path.relative_to(PACKAGE.parent))

        def visit(body, prefix=""):
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    funcs[(module, prefix + node.name)] = node
                elif isinstance(node, ast.ClassDef):
                    visit(node.body, node.name + ".")

        visit(tree.body)
    return funcs


def _resolver(funcs: Dict[_Key, ast.AST]):
    by_qual: Dict[Tuple[str, str], List[_Key]] = defaultdict(list)
    by_stem: Dict[Tuple[str, str], List[_Key]] = defaultdict(list)
    for key in funcs:
        module, qual = key
        by_qual[(module, qual)].append(key)
        by_qual[(module, qual.rsplit(".", 1)[-1])].append(key)
        by_stem[(Path(module).stem, qual.rsplit(".", 1)[-1])].append(key)

    def resolve(module: str, name: str) -> List[_Key]:
        parts = name.split(".")
        if len(parts) == 1:
            return [k for k in by_qual.get((module, parts[0]), []) if k[1] == parts[0]]
        if parts[0] == "self":
            return [
                k for k in by_qual.get((module, parts[-1]), []) if k[1].endswith("." + parts[-1])
            ]
        return list(by_stem.get((parts[-2], parts[-1]), []))

    return resolve


def _lenient_read_tries(func: ast.AST, readers: Set[_Key], module: str, resolve) -> List[ast.Try]:
    """try blocks that read a file (directly or via a project reader) and swallow."""
    found = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Try):
            continue
        reads = False
        for name, _call in [c for stmt in node.body for c in _call_names(stmt)]:
            if _is_read(name) or any(r in readers for r in resolve(module, name)):
                reads = True
                break
        if reads and any(_is_lenient_handler(h) for h in node.handlers):
            found.append(node)
    return found


def _grow(funcs, resolve, seed: Set[_Key], call_index, hops: int) -> Set[_Key]:
    """Seed plus callers of the seed, *hops* levels deep.

    Bounded on purpose: an unbounded closure makes every HTTP endpoint a
    "persister" because something downstream eventually writes a file, which
    buries the signal. Two hops covers the real shapes -- a setter calling a
    module ``_save`` helper that calls the shared atomic writer.
    """
    current = set(seed)
    for _ in range(hops):
        grown = set(current)
        for key in funcs:
            if key in grown:
                continue
            if any(r in current for name in call_index[key] for r in resolve(key[0], name)):
                grown.add(key)
        if grown == current:
            break
        current = grown
    return current


def find_lenient_rewrites() -> Dict[str, str]:
    """Map "module::func" -> why it was flagged, for every lenient-read-then-write."""
    funcs = _collect_functions()
    resolve = _resolver(funcs)
    call_index = {k: [n for n, _c in _call_names(v)] for k, v in funcs.items()}
    calls_with_nodes = {k: _call_names(v) for k, v in funcs.items()}

    # Readers are direct only; _lenient_read_tries already allows one hop, which
    # is what a lenient wrapper over a strict loader (``_load`` over
    # ``_load_or_raise``) needs.
    readers = {k for k, names in call_index.items() if any(_is_read(n) for n in names)}
    persisters = _grow(
        funcs,
        resolve,
        {
            k
            for k, calls in calls_with_nodes.items()
            if any(_is_persist_call(n, c) for n, c in calls)
            and any(n in _RENDER for n, _c in calls)
        },
        call_index,
        hops=2,
    )
    lenient: Set[_Key] = set()
    for key, func in funcs.items():
        if not _lenient_read_tries(func, readers, key[0], resolve):
            continue
        if any(isinstance(s, ast.Return) and s.value is not None for s in ast.walk(func)):
            lenient.add(key)

    flagged: Dict[str, str] = {}
    for key, func in funcs.items():
        if key not in persisters:
            continue
        module = key[0]
        persist_calls = [c for n, c in calls_with_nodes[key] if _is_persist_call(n, c)] or [
            c for n, c in calls_with_nodes[key] if any(r in persisters for r in resolve(module, n))
        ]
        reasons = []
        # (a) a lenient loader's result is written: `data = _load(); ...; _save(data)`
        for name, call in calls_with_nodes[key]:
            if not any(r in lenient for r in resolve(module, name)):
                continue
            # `strict=True` opts out of the lenient contract -- trusted syntactically,
            # not by checking the callee honours it. That is a known limitation of
            # this *structural* guard (it would miss a regression where the callee
            # stops respecting `strict`), but it is not a coverage hole on this
            # branch: `_load_raw`'s own behavior under `strict=True` is pinned by
            # the executing tests in test_config_service.py (e.g.
            # test_unparseable_file_is_refused_and_preserved and the migration
            # tests), which go red if `strict` stops being honoured.
            if any(
                kw.arg == "strict" and isinstance(kw.value, ast.Constant) and kw.value.value
                for kw in call.keywords
            ):
                continue
            reasons.append(f"writes the result of lenient load {name}()")
        # (b) the lenient read is inline, and a write happens outside that try --
        #     a swallow that also covers the write cannot publish a fallback.
        for node in _lenient_read_tries(func, readers, module, resolve):
            inside = {id(c) for stmt in node.body for c in ast.walk(stmt)}
            if any(id(c) not in inside for c in persist_calls):
                reasons.append(f"swallows a read failure at line {node.lineno}, then writes")
        if reasons:
            flagged[f"{module}::{key[1]}"] = "; ".join(sorted(set(reasons)))
    return flagged


def test_no_json_rewrite_consumes_a_lenient_load():
    """#737: every JSON rewrite's pre-image must be read fail-closed."""
    flagged = find_lenient_rewrites()
    new = {k: v for k, v in flagged.items() if k not in ALLOWLIST}
    assert not new, (
        "These functions publish JSON derived from a load that treats an "
        "unreadable file like an absent one, so a single syntax error in the file "
        "makes the write destroy its other contents (GH #737). Read fail-closed "
        "(settings_service._mutate / config_service._mutate_raw are the shape), or "
        "add an ALLOWLIST entry in this file explaining why losing the file's other "
        "contents is acceptable:\n" + "\n".join(f"  {k}\n      {v}" for k, v in sorted(new.items()))
    )


def test_allowlist_has_no_stale_entries():
    """A fixed or deleted write path must leave the allowlist, or it stops guarding."""
    flagged = find_lenient_rewrites()
    stale = sorted(set(ALLOWLIST) - set(flagged))
    assert not stale, (
        "ALLOWLIST entries that no longer match any lenient-read-then-write path. "
        "Delete them:\n" + "\n".join(f"  {k}" for k in stale)
    )
