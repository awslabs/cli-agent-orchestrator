"""Stable fingerprints of agent tool definitions and result-model schemas."""

import ast
import hashlib
import json
from typing import Any


def structural(value: Any) -> Any:
    if isinstance(value, ast.AST):
        return [
            type(value).__name__,
            {
                name: structural(item)
                for name, item in ast.iter_fields(value)
                if item is not None and item != []
            },
        ]
    if isinstance(value, list):
        return [structural(item) for item in value]
    return value


def definition_fingerprint(source: str) -> str:
    tree = ast.parse(source)
    definitions = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            tool = any(
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and isinstance(d.func.value, ast.Name)
                and d.func.value.id == "mcp"
                and d.func.attr == "tool"
                for d in node.decorator_list
            )
            if tool:
                definitions.append(
                    [
                        node.name,
                        structural(node.args),
                        structural(node.returns) if node.returns else None,
                        [structural(d) for d in node.decorator_list],
                        ast.get_docstring(node, clean=False),
                    ]
                )
            elif node.name.startswith("_build_") and "desc" in node.name:
                definitions.append([node.name, structural(node)])
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name)
            and (target.id.endswith("_description") or target.id.endswith("_field_desc"))
            for target in node.targets
        ):
            definitions.append([structural(node)])
    return hashlib.sha256(
        json.dumps(sorted(definitions, key=repr), ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
