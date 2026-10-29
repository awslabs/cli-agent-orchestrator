"""GET /workflows/runs/{run_id}/artifacts[/{input_name}] - reading a run's report file.

These routes exist so the web UI can show the Markdown a workflow wrote to its
``report`` input. Their whole value is in what they REFUSE: the caller names an input,
never a path, and the recorded path must resolve to a regular ``.md`` file directly in
the artifact root. Each refusal below is one way the route could otherwise become a
general file-read primitive. The artifact root is monkeypatched to ``tmp_path`` so the
tests never depend on, or touch, the real system temp dir.
"""

from __future__ import annotations

import json
import os

import pytest

import cli_agent_orchestrator.api.main as api_main
from cli_agent_orchestrator.services import workflow_journal


@pytest.fixture(autouse=True)
def journal_db(tmp_path, monkeypatch):
    from cli_agent_orchestrator.clients.database import (
        _migrate_workflow_run,
        _migrate_workflow_run_step,
    )

    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    return db_path


@pytest.fixture()
def root(tmp_path, monkeypatch):
    """The artifact root: a fresh directory, resolved, standing in for /tmp."""
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    resolved = os.path.realpath(artifact_root)
    monkeypatch.setattr(api_main, "_workflow_artifact_root", lambda: resolved)
    return artifact_root


def _run(run_id: str, inputs: dict) -> None:
    workflow_journal.insert_run(
        run_id,
        "wf",
        json.dumps({"source": "print(1)", "path": None, "content_hash": "sha256:abc"}),
        json.dumps(inputs),
        "completed",
        "2026-10-26T00:00:00+00:00",
        "script",
    )


def test_reads_a_report_the_run_was_given(client, root):
    report = root / "report.md"
    report.write_text("# Verdict\n\nGo.\n", encoding="utf-8")
    _run("r1", {"topic": "parallel agents", "report": str(report)})

    response = client.get("/workflows/runs/r1/artifacts/report")

    assert response.status_code == 200
    assert response.json() == {
        "run_id": "r1",
        "input": "report",
        "path": str(report),
        "size": len("# Verdict\n\nGo.\n"),
        "content": "# Verdict\n\nGo.\n",
    }


def test_list_names_only_inputs_that_point_into_the_root(client, root, tmp_path):
    present = root / "present.md"
    present.write_text("x", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("x", encoding="utf-8")
    _run(
        "r2",
        {
            "report": str(present),
            "draft": str(root / "absent.md"),
            "notes": str(root / "notes.txt"),
            "elsewhere": str(outside),
            "relative": "present.md",
            "count": 3,
        },
    )

    response = client.get("/workflows/runs/r2/artifacts")

    assert response.status_code == 200
    assert response.json() == {
        "run_id": "r2",
        "artifacts": [
            {"input": "report", "path": str(present), "available": True, "size": 1},
            {"input": "draft", "path": str(root / "absent.md"), "available": False, "size": None},
        ],
    }


def test_unknown_run_is_404(client, root):
    assert client.get("/workflows/runs/nope/artifacts").status_code == 404
    assert client.get("/workflows/runs/nope/artifacts/report").status_code == 404


def test_an_input_the_run_never_declared_is_400(client, root):
    report = root / "r.md"
    report.write_text("x", encoding="utf-8")
    _run("r3", {"report": str(report), "count": 3})

    assert client.get("/workflows/runs/r3/artifacts/other").status_code == 400
    # A declared but non-string input names no file either.
    assert client.get("/workflows/runs/r3/artifacts/count").status_code == 400


def test_a_symlink_in_the_root_pointing_outside_is_refused(client, root, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("not yours", encoding="utf-8")
    link = root / "innocent.md"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    _run("r4", {"report": str(link)})

    response = client.get("/workflows/runs/r4/artifacts/report")

    assert response.status_code == 403
    assert "not yours" not in response.text
    # ...and the listing does not advertise it.
    assert client.get("/workflows/runs/r4/artifacts").json()["artifacts"] == []


def test_a_dotdot_path_that_leaves_the_root_is_refused(client, root, tmp_path):
    (tmp_path / "escaped.md").write_text("x", encoding="utf-8")
    _run("r5", {"report": str(root / ".." / "escaped.md")})

    assert client.get("/workflows/runs/r5/artifacts/report").status_code == 403


def test_a_file_that_is_not_markdown_is_refused(client, root):
    other = root / "passwd"
    other.write_text("root:x:0:0", encoding="utf-8")
    _run("r6", {"report": str(other)})

    response = client.get("/workflows/runs/r6/artifacts/report")

    assert response.status_code == 403
    assert "root:x" not in response.text


def test_a_relative_path_is_never_resolved(client, root, monkeypatch):
    (root / "rel.md").write_text("x", encoding="utf-8")
    monkeypatch.chdir(root)
    _run("r7", {"report": "rel.md"})

    assert client.get("/workflows/runs/r7/artifacts/report").status_code == 403


def test_a_declared_report_that_was_never_written_is_404(client, root):
    _run("r8", {"report": str(root / "never.md")})

    assert client.get("/workflows/runs/r8/artifacts/report").status_code == 404


def test_a_directory_named_like_markdown_is_404(client, root):
    (root / "dir.md").mkdir()
    _run("r9", {"report": str(root / "dir.md")})

    assert client.get("/workflows/runs/r9/artifacts/report").status_code == 404


def test_an_oversized_report_is_refused_not_truncated(client, root, monkeypatch):
    monkeypatch.setattr(api_main, "WORKFLOW_ARTIFACT_MAX_BYTES", 16)
    big = root / "big.md"
    big.write_text("x" * 17, encoding="utf-8")
    _run("r10", {"report": str(big)})

    response = client.get("/workflows/runs/r10/artifacts/report")

    assert response.status_code == 413
    listing = client.get("/workflows/runs/r10/artifacts").json()["artifacts"]
    assert listing == [{"input": "report", "path": str(big), "available": False, "size": 17}]


def test_invalid_utf8_is_replaced_not_an_error(client, root):
    report = root / "bytes.md"
    report.write_bytes(b"ok \xff end")
    _run("r11", {"report": str(report)})

    response = client.get("/workflows/runs/r11/artifacts/report")

    assert response.status_code == 200
    assert response.json()["content"] == "ok � end"
