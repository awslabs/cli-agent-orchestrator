"""Strict agent-surface vocabulary scan with three immutable base fields."""

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


def assert_no_decision_imports(source: str, package: str) -> None:
    for node in ast.walk(ast.parse(source)):
        targets = []
        if isinstance(node, ast.Import):
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
