"""Validate documents against the vendored AG-UI 1.0 JSON Schema (Draft 2020-12).

The schema closes every event and capability object with
``unevaluatedProperties: false``, which is what makes an undescribed key a
failure. The SDK models cannot check that: they are ``extra="allow"``.
"""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

from jsonschema import Draft202012Validator

SCHEMA_PATH = Path(__file__).parent / "fixtures" / "agui_conformance" / "schema.json"


@lru_cache(maxsize=None)
def _schema() -> Dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def _validator(definition: str) -> Draft202012Validator:
    schema = copy.deepcopy(_schema())
    schema["$ref"] = f"#/$defs/{definition}"
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def schema_errors(document: Any, definition: str = "Event") -> List[str]:
    """Every validation error for ``document`` against ``#/$defs/<definition>``."""
    return sorted(
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in _validator(definition).iter_errors(document)
    )
