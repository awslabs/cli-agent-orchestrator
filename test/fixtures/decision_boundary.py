"""Strict agent-surface vocabulary scan with three immutable base fields.

The scan covers every agent-facing tool, so it is coupled to tools outside the decision package
on purpose: `VOCABULARY` holds generic words, and the base pins the unrelated descriptions of
`workflow_resume` and `memory_store`. If an unrelated tool's text changes and trips the scan,
first confirm that no decision surface reached the agent server, then re-pin the base JSON.
"""

import ast
import copy
import json
import re
from importlib.util import resolve_name
from typing import Any, Mapping, Sequence

VOCABULARY = (
    "decisions",
    "points",
    "deciders",
    "tiers",
    "model_tiers",
    "exclude_profiles",
    "model.route",
    "effort.route",
)


def assert_agent_boundary(tools: Sequence[Mapping[str, Any]], base: Mapping[str, Any]) -> None:
    names = {tool["name"] for tool in tools}
    assert {"workflow_resume", "memory_store"} <= names
    for tool in tools:
        item = copy.deepcopy(dict(tool))
        if item["name"] == "workflow_resume":
            # Workflow resume choices are unrelated to launch routing.
            assert (
                item["input"]["properties"]["decisions"] == base["workflow_resume_input_decisions"]
            )
            del item["input"]["properties"]["decisions"]
            # This unchanged description explains workflow recovery choices.
            assert item["description"] == base["workflow_resume_description"]
            item["description"] = None
        if item["name"] == "memory_store":
            # This unchanged description explains persisting knowledge.
            assert item["description"] == base["memory_store_description"]
            item["description"] = None
        text = json.dumps(item).lower()
        for word in VOCABULARY:
            assert not re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", text), (
                item["name"],
                word,
            )


def _literal(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _dynamic_targets(call: ast.Call, package: str) -> list[str]:
    func = call.func
    kind = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if kind not in ("import_module", "__import__"):
        return []
    keywords = {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg}
    name = _literal(call.args[0] if call.args else keywords.get("name"))
    if name is None:
        return []
    if kind == "import_module" and name.startswith("."):
        anchor = call.args[1] if len(call.args) > 1 else keywords.get("package")
        # A non-literal anchor such as `__package__` is the scanned module's own package.
        name = resolve_name(name, _literal(anchor) or package)
    targets = [name]
    fromlist = call.args[3] if len(call.args) > 3 else keywords.get("fromlist")
    if kind == "__import__" and isinstance(fromlist, (ast.List, ast.Tuple)):
        targets.extend(f"{name}.{item}" for item in map(_literal, fromlist.elts) if item)
    return targets


def assert_no_decision_imports(source: str, package: str) -> None:
    """Reject static imports, and dynamic imports whose module name is a string literal.

    Literal names are checked whether they are absolute or relative, positional or passed as
    `name=`, including `__import__` `fromlist` entries. A name computed at runtime is not caught
    here, and the boundary test's import-time `sys.modules` check does not see an import inside
    a function body that has not run.
    """
    for node in ast.walk(ast.parse(source)):
        targets = []
        if isinstance(node, ast.Call):
            targets.extend(_dynamic_targets(node, package))
        elif isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = resolve_name("." * node.level + base, package)
            targets.append(base)
            targets.extend(f"{base}.{alias.name}" if base else alias.name for alias in node.names)
        assert not any(
            target == "cli_agent_orchestrator.decisions"
            or target.startswith("cli_agent_orchestrator.decisions.")
            for target in targets
        ), "forbidden decision import"
