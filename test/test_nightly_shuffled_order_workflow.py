"""Structural guard for the non-blocking shuffled-order nightly (#1350)."""

from pathlib import Path

import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "nightly-shuffled-order.yml"
)


def test_nightly_workflow_is_isolated_and_reproducible():
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = document["jobs"]["test"]
    test_step = next(step for step in job["steps"] if step["name"] == "Run shuffled unit tests")
    triggers = document[True]

    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert document["permissions"] == {"contents": "read"}
    assert document["concurrency"]["cancel-in-progress"] is True
    assert job["if"] == "github.repository == 'awslabs/cli-agent-orchestrator'"
    assert job["timeout-minutes"] == 60
    assert "uv python install 3.12" in "\n".join(step.get("run", "") for step in job["steps"])
    run_lines = test_step["run"].splitlines()
    seed_line = next(
        i for i, line in enumerate(run_lines) if "export CAO_TEST_SHUFFLE_SEED" in line
    )
    pytest_line = next(i for i, line in enumerate(run_lines) if "uv run pytest" in line)

    assert seed_line < pytest_line
    assert "examples/remote/ec2/tests/" in test_step["run"]
    assert 'export HOME="$test_home"' not in test_step["run"]
    assert 'CAO_*) unset "$name"' in test_step["run"]
