"""The New workflow templates of the CAO window validate against the real route.

``examples/herdr-app-cao-window/mods/cao-window-panel/templates.js`` holds the two starting points the
window offers: a Python script and a sequential YAML spec (spec ``cao-window-workflows``, "New
workflow and edit"). Each is posted to ``POST /workflows/validate`` in its source form, with its tier,
so the window cannot ship a template the server refuses (task 11.4).

The templates are printed by ``node``. The tests skip without it, and fail instead on the CI leg that
owns the ``unit`` gate (``test/_must_run.py``).
"""

from __future__ import annotations

import functools
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app

PANEL_DIR = (
    Path(__file__).resolve().parents[2]
    / "examples"
    / "herdr-app-cao-window"
    / "mods"
    / "cao-window-panel"
)
NODE = shutil.which("node")
TIERS = ("python", "yaml")
# The window sends a request body as one curl argument, up to this many bytes (lib/cao.js ARG_BODY_MAX).
ARG_BODY_MAX = 122880

pytestmark = [
    pytest.mark.must_run("unit"),
    pytest.mark.skipif(NODE is None, reason="needs node"),
]


@functools.cache
def _templates() -> tuple[dict[str, Any], ...]:
    """Print ``TEMPLATES`` of ``templates.js`` with node and parse it.

    Raises:
        AssertionError: node could not load the module, or it did not print a JSON list. The message
            carries node's own error so a missing file reads as a missing file.
    """
    assert NODE is not None
    done = subprocess.run(
        [
            NODE,
            "--input-type=module",
            "-e",
            "import { TEMPLATES } from './templates.js'; console.log(JSON.stringify(TEMPLATES));",
        ],
        cwd=PANEL_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, f"cannot load templates.js: {done.stderr.strip()[:600]}"
    loaded = json.loads(done.stdout)
    assert isinstance(loaded, list), "TEMPLATES must be a list"
    return tuple(loaded)


def _template(tier: str) -> dict[str, Any]:
    matches = [t for t in _templates() if t.get("tier") == tier]
    assert len(matches) == 1, f"expected exactly one {tier} template, found {len(matches)}"
    return matches[0]


@pytest.fixture
def client() -> TestClient:
    """A fresh client per test; the validate route is read-only."""
    return TestClient(app, base_url="http://localhost")


def test_there_is_one_python_and_one_yaml_template() -> None:
    assert sorted(t["tier"] for t in _templates()) == ["python", "yaml"]


def test_each_template_has_an_id_a_label_a_bare_name_and_a_source() -> None:
    ids = [t["id"] for t in _templates()]
    assert len(set(ids)) == len(ids), "template ids must be unique"
    for template in _templates():
        assert template["label"].strip(), template
        assert re.fullmatch(r"[A-Za-z0-9_-]+", template["name"]), template["name"]
        assert template["source"].strip(), template["id"]


@pytest.mark.parametrize("tier", TIERS)
def test_template_fits_in_one_request_from_the_window(tier: str) -> None:
    template = _template(tier)
    body = json.dumps(
        {"name": template["name"], "source": template["source"], "tier": tier}, ensure_ascii=False
    )
    assert len(body.encode("utf-8")) < ARG_BODY_MAX // 4


def test_the_yaml_template_is_a_sequential_spec_with_two_or_more_steps() -> None:
    spec = yaml.safe_load(_template("yaml")["source"])
    assert spec["mode"] == "sequential"
    assert len(spec["steps"]) >= 2


@pytest.mark.parametrize("tier", TIERS)
def test_template_passes_the_real_validate_route(client: TestClient, tier: str) -> None:
    template = _template(tier)
    response = client.post("/workflows/validate", json={"source": template["source"], "tier": tier})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] in ("pass", "pass_reserved"), body
    assert body["errors"] == [], body
    blocking = [f for f in body.get("findings", []) if f.get("severity") == "error"]
    assert blocking == [], body


def test_the_route_does_refuse_a_broken_spec_so_the_check_above_can_fail(
    client: TestClient,
) -> None:
    broken_yaml = client.post(
        "/workflows/validate",
        json={"source": "name: x\nsteps: not-a-list\n", "tier": "yaml"},
    )
    assert broken_yaml.status_code == 200, broken_yaml.text
    assert broken_yaml.json()["status"] == "fail"
    broken_python = client.post(
        "/workflows/validate",
        json={"source": 'def main():\n    step("do-thing")\n', "tier": "python"},
    )
    assert broken_python.status_code == 200, broken_python.text
    assert broken_python.json()["status"] == "fail"
