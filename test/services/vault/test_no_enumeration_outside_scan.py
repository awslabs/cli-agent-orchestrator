"""Structural guards for vault filesystem boundaries."""

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[3] / "src" / "cli_agent_orchestrator"


def _is_memory_metadata_file_path(node: ast.AST) -> bool:
    """Recognise the reader's metadata-row file path, not generic file paths."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "file_path"
        and (
            (isinstance(node.value, ast.Name) and node.value.id in {"metadata", "memory_metadata"})
            or (isinstance(node.value, ast.Attribute) and node.value.attr == "metadata")
        )
    )


def test_vault_read_boundaries_do_not_enumerate_directories():
    """Reader and binding resolve SQLite/config state, never vault trees."""
    forbidden = {"rglob", "glob", "iterdir", "walk", "scandir"}
    sinks: list[str] = []
    violations: list[str] = []
    for name in ("scan.py", "reader.py", "binding.py"):
        path = SOURCE_ROOT / "services" / "vault" / name
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in forbidden:
                    sink = f"{name}:{node.lineno}:{node.func.attr}"
                    sinks.append(sink)
                    if name != "scan.py":
                        violations.append(sink)
    assert sinks
    assert violations == []


def test_vault_reader_is_the_only_metadata_file_path_read_sink():
    """A metadata file path must never be turned into bytes outside reader.py."""
    sources: list[Path] = []
    violations: list[str] = []
    for path in SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        metadata_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if value is not None and any(
                    _is_memory_metadata_file_path(descendant) for descendant in ast.walk(value)
                ):
                    sources.append(path)
                    metadata_names.update(
                        target.id for target in targets if isinstance(target, ast.Name)
                    )
            if not isinstance(node, ast.Call):
                continue
            is_open = isinstance(node.func, ast.Name) and node.func.id == "open"
            is_read_text = isinstance(node.func, ast.Attribute) and node.func.attr == "read_text"
            if not (is_open or is_read_text) or not node.args:
                continue
            argument = node.func.value if is_read_text else node.args[0]
            is_metadata_name = isinstance(argument, ast.Name) and argument.id in metadata_names
            if is_metadata_name or _is_memory_metadata_file_path(argument):
                if path.name != "reader.py":
                    violations.append(str(path.relative_to(SOURCE_ROOT)))
    assert {path.name for path in sources} <= {"reader.py"}
    assert violations == []


def test_reviewed_vault_files_own_every_nonempty_read_sink_set():
    """Vault reads stay in containment-reviewed modules.

    ``scan.py`` discovers and hashes candidate bytes, ``reader.py`` serves
    recall through its chokepoint, ``writer.py`` reads an existing managed note
    under its lock for conflict checks and frontmatter preservation, and
    ``migrate.py`` reads native memories under ``MEMORY_BASE_DIR`` only, never
    a vault note.
    """
    vault_root = SOURCE_ROOT / "services" / "vault"
    entitled = {"scan.py", "reader.py", "writer.py", "migrate.py"}
    sinks: list[str] = []
    violations: list[str] = []

    for path in vault_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_read_sink(node):
                continue
            location = f"{path.name}:{node.lineno}"
            sinks.append(location)
            if path.name not in entitled:
                violations.append(location)

    assert sinks, "vault read-sink matcher must find reviewed reads"
    assert violations == []


def test_vault_candidate_chokepoint_requires_explicit_injection_policy():
    """Candidate construction and loading require explicit policy and consumer identity."""
    reader_path = SOURCE_ROOT / "services" / "vault" / "reader.py"
    reader_tree = ast.parse(reader_path.read_text(encoding="utf-8"), filename=str(reader_path))
    resolve_definition = next(
        node
        for node in ast.walk(reader_tree)
        if isinstance(node, ast.FunctionDef) and node.name == "resolve_candidates"
    )
    terminal_parameter = next(
        argument for argument in resolve_definition.args.kwonlyargs if argument.arg == "terminal_id"
    )
    assert (
        resolve_definition.args.kw_defaults[
            resolve_definition.args.kwonlyargs.index(terminal_parameter)
        ]
        is None
    )
    consumer_parameter = next(
        argument for argument in resolve_definition.args.kwonlyargs if argument.arg == "consumer"
    )
    assert (
        resolve_definition.args.kw_defaults[
            resolve_definition.args.kwonlyargs.index(consumer_parameter)
        ]
        is None
    )
    load_definition = next(
        node
        for node in ast.walk(reader_tree)
        if isinstance(node, ast.FunctionDef) and node.name == "load_candidate"
    )
    require_parameter = next(
        argument
        for argument in load_definition.args.kwonlyargs
        if argument.arg == "require_injectable"
    )
    assert require_parameter.arg == "require_injectable"
    assert len(load_definition.args.kw_defaults) == len(load_definition.args.kwonlyargs)
    assert (
        load_definition.args.kw_defaults[load_definition.args.kwonlyargs.index(require_parameter)]
        is None
    )

    call_sites: list[str] = []
    violations: list[str] = []
    resolver_call_sites: list[str] = []
    resolver_violations: list[str] = []
    candidate_constructors: list[str] = []
    for path in SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (
                (isinstance(node.func, ast.Name) and node.func.id == "load_candidate")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "load_candidate")
            ):
                location = f"{path.name}:{node.lineno}"
                call_sites.append(location)
                if not any(keyword.arg == "require_injectable" for keyword in node.keywords):
                    violations.append(location)
            if isinstance(node, ast.Call) and (
                (isinstance(node.func, ast.Name) and node.func.id == "resolve_candidates")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "resolve_candidates")
            ):
                location = f"{path.name}:{node.lineno}"
                resolver_call_sites.append(location)
                if not {keyword.arg for keyword in node.keywords} >= {"terminal_id", "consumer"}:
                    resolver_violations.append(location)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "VaultCandidate"
                and path.name != "reader.py"
            ):
                candidate_constructors.append(f"{path.name}:{node.lineno}")

    assert call_sites, "load_candidate call-site matcher must find consumers"
    assert violations == []
    assert resolver_call_sites, "resolver call-site matcher must find consumers"
    assert resolver_violations == []
    assert candidate_constructors == []


def test_injected_context_call_sites_explicitly_name_their_consumer():
    """Every injection entry point must opt into the non-waivable consumer gate."""
    path = SOURCE_ROOT / "services" / "memory_service.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    renderer = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "get_memory_context_for_terminal"
    )
    injected_calls: list[str] = []
    violations: list[str] = []
    for node in ast.walk(renderer):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"_vault_candidates", "_load_related_vault_memory"}:
            continue
        location = f"memory_service.py:{node.lineno}"
        injected_calls.append(node.func.attr)
        consumer = next((item.value for item in node.keywords if item.arg == "consumer"), None)
        if not isinstance(consumer, ast.Constant) or consumer.value != "injected_context":
            violations.append(location)

    assert len(injected_calls) >= 2
    assert violations == []


def test_vault_candidate_consumers_stay_behind_memory_service_boundary():
    """Direct candidate resolution remains in the two reviewed service helpers.

    BM25 may build an ungated corpus through ``_vault_candidates`` for IDF,
    but result selection still goes through the caller policy in that helper.
    """
    allowed_resolver_functions = {"_vault_candidates", "_load_related_vault_memory"}
    violations: list[str] = []
    consumer_violations: list[str] = []

    for path in SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr if isinstance(node.func, ast.Attribute) else None
            )
            if name == "resolve_candidates" and path.name != "reader.py":
                parent = node
                while parent in parents and not isinstance(
                    parent, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    parent = parents[parent]
                allowed_memory_service_call = (
                    path.name == "memory_service.py"
                    and getattr(parent, "name", None) in allowed_resolver_functions
                )
                allowed_graph_projection = (
                    path.name == "memory.py"
                    and path.parent.name == "providers"
                    and getattr(parent, "name", None) == "_build"
                )
                if not (allowed_memory_service_call or allowed_graph_projection):
                    violations.append(f"{path.name}:{node.lineno}")
            if name == "load_candidate" and path.name not in {"reader.py", "memory_service.py"}:
                violations.append(f"{path.name}:{node.lineno}")
            if name in {"_vault_candidates", "_load_related_vault_memory"}:
                consumer = next(
                    (item.value for item in node.keywords if item.arg == "consumer"), None
                )
                if not isinstance(consumer, ast.Constant) or not isinstance(consumer.value, str):
                    consumer_violations.append(f"{path.name}:{node.lineno}")

    assert violations == []
    assert consumer_violations == []


def test_graph_provider_never_reads_vault_candidate_content():
    """U9 projects candidate metadata only; reader.py remains the sole content sink."""
    path = SOURCE_ROOT / "graph" / "providers" / "memory.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls = [
        f"memory.py:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "load_candidate")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "load_candidate")
        )
    ]
    assert calls == []


def test_production_vault_policy_instances_are_resolver_owned():
    """Only the resolver and named identity-free BM25 corpus policy construct policies."""
    violations: list[str] = []
    for path in SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "VaultInjectionPolicy":
                    parent = node
                    while parent in parents and not isinstance(
                        parent, (ast.FunctionDef, ast.AsyncFunctionDef)
                    ):
                        parent = parents[parent]
                    outside_resolver = (
                        path.name != "reader.py"
                        or getattr(parent, "name", None) != "_resolve_injection_policy"
                    )
                    is_bm25_corpus_policy = (
                        path.name == "memory_service.py"
                        and isinstance(parents.get(node), ast.Assign)
                        and any(
                            isinstance(target, ast.Name) and target.id == "_BM25_CORPUS_POLICY"
                            for target in parents[node].targets
                        )
                    )
                    if outside_resolver and not is_bm25_corpus_policy:
                        violations.append(f"{path.name}:{node.lineno}")

    assert violations == [], (
        "VaultInjectionPolicy construction is resolver-owned; the only exception is "
        "_BM25_CORPUS_POLICY, the named identity-free BM25 corpus policy."
    )


def test_bm25_corpus_policy_is_confined_to_corpus_population():
    """The identity-free policy must never reach a returning vault read path."""
    path = SOURCE_ROOT / "services" / "memory_service.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    uses: list[tuple[str, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        policy = next((item.value for item in node.keywords if item.arg == "policy"), None)
        if not isinstance(policy, ast.Name) or policy.id != "_BM25_CORPUS_POLICY":
            continue
        parent = node
        while parent in parents and not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parent = parents[parent]
        call_name = node.func.attr if isinstance(node.func, ast.Attribute) else ""
        uses.append((getattr(parent, "name", ""), call_name))

    assert set(uses) == {
        ("_bm25_relevance", "_vault_candidates"),
        ("_bm25_search", "_vault_candidates"),
    }
    assert len(uses) == 2


def _is_read_sink(node: ast.Call) -> bool:
    """Match all supported open/read forms, then subtract explicit write modes."""
    if isinstance(node.func, ast.Name):
        name = node.func.id
        owner = ""
    elif isinstance(node.func, ast.Attribute):
        name = node.func.attr
        owner = node.func.value.id if isinstance(node.func.value, ast.Name) else ""
    else:
        return False

    if name in {"read_text", "read_bytes"}:
        return True
    if name != "open":
        return False
    if owner not in {"", "os", "io"} and not isinstance(node.func, ast.Attribute):
        return False

    if owner == "os":
        flags = _call_argument(node, "flags", 1)
        return not _os_flags_write(flags)
    mode = _call_argument(node, "mode", 1)
    return not (
        isinstance(mode, ast.Constant)
        and isinstance(mode.value, str)
        and any(flag in mode.value for flag in ("w", "a", "x", "+"))
    )


def _call_argument(node: ast.Call, keyword: str, index: int) -> ast.expr | None:
    for item in node.keywords:
        if item.arg == keyword:
            return item.value
    return node.args[index] if len(node.args) > index else None


def _os_flags_write(value: ast.expr | None) -> bool:
    """Classify literal/bitwise os.open flag expressions without allowlisting reads."""
    if value is None:
        return False
    rendered = ast.unparse(value)
    return any(flag in rendered for flag in ("O_WRONLY", "O_RDWR", "O_APPEND", "O_CREAT"))
