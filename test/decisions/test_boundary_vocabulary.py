"""Character-count wording must not hide decision vocabulary in tool text."""

import copy
import json
from pathlib import Path
from test.fixtures.decision_boundary import assert_agent_boundary
from typing import Any

import pytest


@pytest.fixture
def boundary_surface() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    base = json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures/decision_boundary_base.json").read_text()
    )
    tools = [
        {
            "name": "workflow_resume",
            "description": base["workflow_resume_description"],
            "input": {
                "properties": {"decisions": copy.deepcopy(base["workflow_resume_input_decisions"])}
            },
            "output": {},
        },
        {
            "name": "memory_store",
            "description": base["memory_store_description"],
            "input": {},
            "output": {},
        },
        {"name": "fixture_ping", "description": "Ping", "input": {}, "output": {}},
    ]
    return tools, base


@pytest.mark.parametrize("field", ["description", "input", "output"])
@pytest.mark.parametrize("description", ["64 Unicode code points", "64 uNiCoDe cOdE pOiNtS"])
def test_boundary_scan_allows_exact_unicode_code_points(
    boundary_surface: tuple[list[dict[str, Any]], dict[str, Any]], field: str, description: str
) -> None:
    tools, base = boundary_surface
    tools[2][field] = description if field == "description" else {"description": description}

    assert_agent_boundary(tools, base)


@pytest.mark.parametrize("field", ["description", "input", "output"])
@pytest.mark.parametrize(
    "description",
    [
        "decision points",
        "points",
        "Unicode code points; decision points",
        "Unicode code points; points",
        "nonUnicode code points",
        "Unicode code points_suffix",
        "Unicode code points2",
        "Unicode  code points",
        "Unicode code  points",
    ],
)
def test_boundary_scan_rejects_points_outside_exact_unicode_phrase(
    boundary_surface: tuple[list[dict[str, Any]], dict[str, Any]], field: str, description: str
) -> None:
    tools, base = boundary_surface
    tools[2][field] = description if field == "description" else {"description": description}

    with pytest.raises(AssertionError, match="points"):
        assert_agent_boundary(tools, base)
