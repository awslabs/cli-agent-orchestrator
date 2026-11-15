"""Safety contract for ``cao memory doctor --fixtures``."""

from pathlib import Path

from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands import memory as command_module
from cli_agent_orchestrator.cli.commands.memory import memory


def _fixture_home(tmp_path: Path, monkeypatch) -> tuple[Path, tuple[Path, ...]]:
    base = tmp_path / "memory"
    topic = base / "fixture-project" / "wiki" / "project" / "topic-x.md"
    lesson = base / "global" / "wiki" / "agent" / "transformer" / "lesson.md"
    audit = base / "logs" / "memory" / "2026-01-02.md"
    for path in (topic, lesson, audit):
        path.parent.mkdir(parents=True, exist_ok=True)
    topic.write_text(
        "# topic-x\n\n" "## 2026-01-02T00:00:00Z\nalpha\n\n" "## 2026-01-02T00:00:01Z\nbeta\n",
        encoding="utf-8",
    )
    lesson.write_text(
        "# lesson\n\n## 2026-01-02T00:00:00Z\n" "Use X. Applies when: transforming Y.\n",
        encoding="utf-8",
    )
    audit.write_text(
        "- 2026-01-02T00:00:00Z [relationship_mutation] "
        "scope_id=project source_key=b-0123abcd — `relationship created`\n"
        "- 2026-01-02T00:00:01Z [instruction_promotion] "
        "profile=transformer — `promoted lessons`\n"
        "- 2026-01-02T00:00:02Z [memory_stored] "
        "scope=global — `real operator event`\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(command_module, "MEMORY_BASE_DIR", base)
    return base, (topic, lesson, audit)


def test_doctor_fixtures_is_dry_run_by_default(tmp_path, monkeypatch):
    base, files = _fixture_home(tmp_path, monkeypatch)
    before = {path: path.read_bytes() for path in files}

    result = CliRunner().invoke(memory, ["doctor", "--fixtures"])

    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output
    assert "audit_events=2" in result.output
    assert "audit_bytes=" in result.output
    assert {path: path.read_bytes() for path in files} == before
    assert not (base / ".quarantine").exists()


def test_doctor_requires_each_fixture_key_and_content_pair(tmp_path, monkeypatch):
    base = tmp_path / "memory"
    wiki = base / "global" / "wiki"
    wiki.mkdir(parents=True)
    notes = {
        "topic-x.md": "# topic-x\n\nReal operator content.\n",
        "real-topic.md": "# real-topic\n\nalpha\n\nbeta\n",
        "lesson.md": "# lesson\n\nA real retrospective lesson.\n",
        "real-lesson.md": "# real-lesson\n\nUse X. Applies when: transforming Y.\n",
    }
    for name, content in notes.items():
        (wiki / name).write_text(content, encoding="utf-8")
    monkeypatch.setattr(command_module, "MEMORY_BASE_DIR", base)

    result = CliRunner().invoke(memory, ["doctor", "--fixtures"])

    assert result.exit_code == 0, result.output
    assert "fixture_files=0" in result.output
    assert {path.name for path in wiki.glob("*.md")} == set(notes)


def test_doctor_apply_requires_backup(tmp_path, monkeypatch):
    _fixture_home(tmp_path, monkeypatch)

    result = CliRunner().invoke(memory, ["doctor", "--fixtures", "--apply"])

    assert result.exit_code != 0
    assert "--backup" in result.output


def test_doctor_apply_quarantines_topics_but_only_reports_audit_log(tmp_path, monkeypatch):
    base, files = _fixture_home(tmp_path, monkeypatch)
    original = {path.relative_to(base): path.read_bytes() for path in files}
    backup = tmp_path / "backup"

    result = CliRunner().invoke(
        memory,
        ["doctor", "--fixtures", "--apply", "--backup", str(backup)],
    )

    assert result.exit_code == 0, result.output
    assert "audit_events=2" in result.output
    assert "audit_bytes=" in result.output
    assert "report-only fixture_relationship_audit,transformer_promotion_audit" in result.output
    assert "moved=2" in result.output

    topic_files = files[:2]
    assert all(not path.exists() for path in topic_files)
    for path in topic_files:
        relative = path.relative_to(base)
        content = original[relative]
        assert (backup / relative).read_bytes() == content
        quarantined = list((base / ".quarantine").glob(f"*/{relative}"))
        assert len(quarantined) == 1
        assert quarantined[0].read_bytes() == content

    audit = files[2]
    audit_relative = audit.relative_to(base)
    assert audit.read_bytes() == original[audit_relative]
    assert not (backup / audit_relative).exists()
    assert not list((base / ".quarantine").glob(f"*/{audit_relative}"))
