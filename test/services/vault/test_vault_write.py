"""Security-focused tests for managed-folder vault writes."""

from __future__ import annotations

import ast
import contextlib
import hashlib
import logging
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from test.fixtures.vault_factory import build_vault_fixture

import pytest
import yaml

from cli_agent_orchestrator.services.memory_service import MemoryPartialWriteError
from cli_agent_orchestrator.services.vault import writer
from cli_agent_orchestrator.services.vault.binding import VaultBinding
from cli_agent_orchestrator.services.vault.parser import parse_note, split_frontmatter


def _binding(fixture) -> VaultBinding:
    mapping = next(mapping for mapping in fixture.vault.mappings if mapping.writable)
    return VaultBinding(
        scope=mapping.scope,
        scope_id=mapping.scope_id,
        vault_id=fixture.vault.id,
        root=fixture.vault.root,
        mapping=mapping,
    )


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _write(fixture, **kwargs):
    defaults = {
        "vault": fixture.vault,
        "binding": _binding(fixture),
        "key": "managed-note",
        "body": "new body\n",
        "cao": {"type": "reference"},
        "expected_content_sha256": None,
    }
    defaults.update(kwargs)
    return writer.write_managed_note(**defaults)


def test_write_preserves_user_frontmatter_bytes_except_cao_block(tmp_path) -> None:
    fixture = build_vault_fixture(tmp_path)
    target = fixture.root / "CAO" / "managed-note.md"
    user_frontmatter = (
        'title: "Keep # quoting"\n'
        "aliases: ['One', \"Two\"] # preserve comment\n"
        "custom_field: value\n"
    )
    original = "---\n" + user_frontmatter + "cao:\n  key: old-key\n  type: project\n---\nold body\n"
    target.write_text(original, encoding="utf-8")

    _write(fixture, expected_content_sha256=_sha256(original))

    written = target.read_text(encoding="utf-8")
    assert user_frontmatter in written
    assert "cao:\n  type: reference\n  key: managed-note\n  managed: true\n" in written
    assert "old-key" not in written
    assert written.endswith("new body\n")


def test_write_seeds_standard_top_level_frontmatter_on_a_new_note(tmp_path) -> None:
    fixture = build_vault_fixture(tmp_path)

    result = _write(
        fixture,
        frontmatter={
            "tags": ["migration", "native"],
            "created": datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        },
    )

    written = (fixture.root / "CAO" / "managed-note.md").read_text(encoding="utf-8")
    assert "tags:\n- migration\n- native\n" in written
    assert "created: 2025-01-02 03:04:05+00:00\n" in written
    assert result.ignored_frontmatter_keys == ()


def test_write_preserves_existing_standard_frontmatter_and_reports_ignored_seed(tmp_path) -> None:
    fixture = build_vault_fixture(tmp_path)
    target = fixture.root / "CAO" / "managed-note.md"
    original = "---\ntags: user-value\n---\nold body\n"
    target.write_text(original, encoding="utf-8")

    result = _write(
        fixture,
        frontmatter={
            "tags": ["migration"],
            "created": datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        },
        expected_content_sha256=_sha256(original),
    )

    written = target.read_text(encoding="utf-8")
    assert "tags: user-value\n" in written
    assert "tags:\n- migration\n" not in written
    assert "created: 2025-01-02 03:04:05+00:00\n" in written
    assert result.ignored_frontmatter_keys == ("tags",)


def test_write_rejects_unsupported_top_level_frontmatter_key_by_name(tmp_path) -> None:
    fixture = build_vault_fixture(tmp_path)

    with pytest.raises(ValueError, match=r"unsupported top-level frontmatter key: 'title'"):
        _write(fixture, frontmatter={"title": "would overwrite a user key"})


def test_write_preserves_bom_crlf_and_unusual_user_frontmatter_bytes(tmp_path) -> None:
    fixture = build_vault_fixture(tmp_path)
    target = fixture.root / "CAO" / "managed-note.md"
    user_frontmatter = (
        "# user comment\r\n"
        'title: "quoted value"\r\n'
        "tag: one\r\n"
        "tag: two\r\n"
        "description: |-\r\n"
        "  first line\r\n"
        "  second: line # still text\r\n"
    )
    original = (
        "\ufeff---\r\n"
        + user_frontmatter
        + "cao:\r\n  key: old-key\r\n  type: project\r\n---\r\nold body\r\n"
    )
    target.write_bytes(original.encode("utf-8"))

    _write(
        fixture,
        body="new body\r\n",
        expected_content_sha256=writer._sha256(original),
    )

    written = target.read_bytes().decode("utf-8")
    assert written.startswith("\ufeff---\r\n")
    assert user_frontmatter in written
    assert "cao:\r\n  type: reference\r\n  key: managed-note\r\n  managed: true\r\n" in written
    assert "old-key" not in written
    assert written.endswith("new body\r\n")
    assert "\n" not in written.replace("\r\n", "")


@pytest.mark.parametrize(
    (
        "bom",
        "newline",
        "user_frontmatter",
        "cao_block",
        "retained",
        "body",
        "valid_yaml",
    ),
    [
        (
            "",
            "\n",
            "title: keep\n",
            "cao:\n  key: old-key\n\n",
            "title: keep\n\n",
            "new body\n",
            True,
        ),
        (
            "",
            "\n",
            "tag: one\ntag: two\ndescription: |-\n  first line\n  second: line # text\n",
            "cao:\n  key: old-key\n",
            "tag: one\ntag: two\ndescription: |-\n  first line\n  second: line # text\n",
            "new body\n",
            True,
        ),
        (
            "\ufeff",
            "\n",
            "title: BOM LF\n",
            "cao:\n  key: old-key\n",
            "title: BOM LF\n",
            "new body\n",
            True,
        ),
        (
            "\ufeff",
            "\r\n",
            "title: BOM CRLF\r\n",
            "cao:\r\n  key: old-key\r\n",
            "title: BOM CRLF\r\n",
            "new body\r\n",
            True,
        ),
        (
            "",
            "\n",
            "title: duplicate cao keys\n",
            "cao:\n  key: first\ncao:\n  key: second\n",
            "title: duplicate cao keys\n",
            "new body\n",
            True,
        ),
        (
            "",
            "\n",
            "title: quoted cao key\n",
            '"cao":\n  key: quoted\n',
            "title: quoted cao key\n",
            "new body\n",
            True,
        ),
        (
            "",
            "\n",
            "title: LF frontmatter\n",
            "cao:\n  key: old-key\n",
            "title: LF frontmatter\n",
            "new body\r\n",
            True,
        ),
    ],
)
def test_writer_output_round_trips_through_shared_frontmatter_boundary(
    tmp_path, bom, newline, user_frontmatter, cao_block, retained, body, valid_yaml
) -> None:
    fixture = build_vault_fixture(tmp_path)
    target = fixture.root / "CAO" / "managed-note.md"
    original = bom + "---" + newline + user_frontmatter + cao_block + "---" + newline + "old body"
    target.write_bytes(original.encode("utf-8"))

    _write(
        fixture,
        body=body,
        expected_content_sha256=writer._sha256(original),
    )

    written = target.read_bytes().decode("utf-8")
    region = split_frontmatter(written, 8192)
    expected_cao = writer._render_cao("managed-note", {"type": "reference"}, newline)

    assert written[: region.start] == bom
    assert region.raw == (retained + expected_cao).removesuffix(newline)
    assert retained in region.raw
    assert region.body == body
    assert written.startswith(bom)
    assert len(re.findall(r"""(?m)^(?:"cao"|'cao'|cao)\s*:""", region.raw)) == 1
    if valid_yaml:
        assert yaml.safe_load(region.raw)["cao"]["key"] == "managed-note"

    _write(
        fixture,
        body=body,
        expected_content_sha256=writer._sha256(written),
    )
    assert target.read_bytes() == written.encode("utf-8")


@pytest.mark.parametrize(
    "user_frontmatter",
    [
        'desc: "one\\ncao: fake\\nend"\ntitle: keep\n',
        "items: [\ncao: not-really,\nother]\ntitle: keep\n",
        "m: {\ncao: inner,\nz: 1,\n}\ntitle: keep\n",
        "  cao:\n    key: stale\n  title: keep\n",
        'desc: "one\x85cao: fake\x85end"\ntitle: keep\n',
        'desc: "one\u2028cao: fake\u2028end"\ntitle: keep\n',
        'desc: "one\u2029cao: fake\u2029end"\ntitle: keep\n',
        "? cao\n: \n  key: stale\ntitle: keep\n",
        "!!str cao:\n  key: stale\ntitle: keep\n",
    ],
)
def test_write_preserves_every_non_cao_frontmatter_value(tmp_path, user_frontmatter) -> None:
    fixture = build_vault_fixture(tmp_path)
    target = fixture.root / "CAO" / "managed-note.md"
    original = f"---\n{user_frontmatter}---\nold body\n"
    parsed_before = parse_note(original, max_frontmatter_bytes=8192, secret_gate="reject")
    assert parsed_before.finding_code is None
    user_values_before = dict(parsed_before.frontmatter)
    user_values_before.pop("cao", None)
    target.write_text(original, encoding="utf-8")

    _write(fixture, expected_content_sha256=writer._sha256(original))

    written = target.read_text(encoding="utf-8")
    parsed_after = parse_note(written, max_frontmatter_bytes=8192, secret_gate="reject")
    assert parsed_after.finding_code is None
    user_values_after = dict(parsed_after.frontmatter)
    user_values_after.pop("cao", None)
    assert user_values_after == user_values_before


def test_writer_never_second_guesses_shared_frontmatter_newline() -> None:
    source = Path(writer.__file__).read_text(encoding="utf-8")

    assert '"\\r\\n" in' not in source


def test_write_uses_frontmatter_newline_when_body_has_a_different_ending(
    tmp_path,
) -> None:
    fixture = build_vault_fixture(tmp_path)
    target = fixture.root / "CAO" / "managed-note.md"
    original = "---\ntitle: keep\nother: still here\n---\nbody touched on Windows\r\n"
    target.write_bytes(original.encode("utf-8"))

    _write(fixture, expected_content_sha256=writer._sha256(original))

    written = target.read_bytes().decode("utf-8")
    assert "title: keep\nother: still here\n" in written
    assert written.endswith("new body\n")


def test_write_refuses_existing_non_frontmatter_note_without_changing_it(
    tmp_path,
) -> None:
    fixture = build_vault_fixture(tmp_path)
    target = fixture.root / "CAO" / "managed-note.md"
    original = "A user-owned note without YAML frontmatter.\n"
    target.write_text(original, encoding="utf-8")

    with pytest.raises(writer.VaultWriteConflictError) as caught:
        _write(fixture, expected_content_sha256=writer._sha256(original))

    assert str(caught.value) == (
        f"vault note changed at {str(target)!r}; "
        "run `cao memory vault reconcile --apply` before writing"
    )
    assert target.read_text(encoding="utf-8") == original


def test_write_creates_a_brand_new_managed_note(tmp_path) -> None:
    fixture = build_vault_fixture(tmp_path)
    target = fixture.root / "CAO" / "managed-note.md"

    _write(fixture)

    assert target.read_text(encoding="utf-8") == (
        "---\ncao:\n  type: reference\n  key: managed-note\n  managed: true\n---\nnew body\n"
    )


def test_write_uses_complete_line_frontmatter_fence(tmp_path) -> None:
    fixture = build_vault_fixture(tmp_path)
    target = fixture.root / "CAO" / "managed-note.md"
    user_frontmatter = "title: keep\n---suffix\nmore: also mine\n"
    original = "---\n" + user_frontmatter + "cao:\n  key: old-key\n---\nbody\n"
    target.write_text(original, encoding="utf-8")

    with pytest.raises(writer.VaultWriteConflictError) as caught:
        _write(fixture, expected_content_sha256=writer._sha256(original))

    assert str(caught.value) == (
        f"vault note changed at {str(target)!r}; "
        "run `cao memory vault reconcile --apply` before writing"
    )
    assert target.read_text(encoding="utf-8") == original


def test_write_rejects_secret_rendered_in_cao_frontmatter(tmp_path) -> None:
    fixture = build_vault_fixture(tmp_path)

    with pytest.raises(writer.VaultSecretWriteError) as caught:
        _write(fixture, cao={"note": "Authorization: Bearer " + "a" * 16})

    assert (
        str(caught.value) == "vault write rejected: cao matched credential pattern 'bearer_token'"
    )
    assert not (fixture.root / "CAO" / "managed-note.md").exists()


def test_write_preserves_blank_line_after_cao_block(tmp_path) -> None:
    fixture = build_vault_fixture(tmp_path)
    target = fixture.root / "CAO" / "managed-note.md"
    user_frontmatter = "title: keep\n"
    original = "---\n" + user_frontmatter + "cao:\n  key: old-key\n\nother: retained\n---\nbody\n"
    target.write_text(original, encoding="utf-8")

    _write(fixture, expected_content_sha256=writer._sha256(original))

    assert "title: keep\n\nother: retained\n" in target.read_text(encoding="utf-8")


def test_write_never_touches_an_unmanaged_note(tmp_path) -> None:
    fixture = build_vault_fixture(tmp_path)
    unmanaged = fixture.root / "Projects" / "CAO Design" / "Don't Panic.md"
    before = unmanaged.read_bytes()

    _write(fixture)

    assert unmanaged.read_bytes() == before


def test_write_detects_modification_after_lock_acquisition(tmp_path, monkeypatch) -> None:
    fixture = build_vault_fixture(tmp_path)
    target = fixture.root / "CAO" / "managed-note.md"
    original = "---\ntitle: before\n---\nbody\n"
    target.write_text(original, encoding="utf-8")

    @contextlib.contextmanager
    def mutate_after_lock(lock_path, timeout):
        del lock_path, timeout
        target.write_text("---\ntitle: concurrent\n---\nbody\n", encoding="utf-8")
        yield

    monkeypatch.setattr(writer, "_file_lock", mutate_after_lock)

    with pytest.raises(writer.VaultWriteConflictError) as caught:
        _write(fixture, expected_content_sha256=_sha256(original))

    assert str(caught.value) == (
        f"vault note changed at {str(target)!r}; "
        "run `cao memory vault reconcile --apply` before writing"
    )
    assert target.read_text(encoding="utf-8") == "---\ntitle: concurrent\n---\nbody\n"


def test_reject_secret_gate_refuses_without_creating_note(tmp_path) -> None:
    fixture = build_vault_fixture(tmp_path)
    secret_body = "Authorization: Bearer " + "a" * 16

    with pytest.raises(writer.VaultSecretWriteError) as caught:
        _write(fixture, body=secret_body)

    assert (
        str(caught.value) == "vault write rejected: body matched credential pattern 'bearer_token'"
    )
    assert not (fixture.root / "CAO" / "managed-note.md").exists()


def test_write_allows_secret_shaped_retained_frontmatter(tmp_path) -> None:
    fixture = build_vault_fixture(tmp_path)
    target = fixture.root / "CAO" / "managed-note.md"
    original = (
        "---\n"
        "notes: 'see password: hunter2sixteen in the runbook'\n"
        "cao:\n  key: old-key\n"
        "---\n"
        "old body\n"
    )
    target.write_text(original, encoding="utf-8")

    _write(fixture, expected_content_sha256=writer._sha256(original))

    written = target.read_text(encoding="utf-8")
    assert "notes: 'see password: hunter2sixteen in the runbook'\n" in written
    assert written.endswith("new body\n")


def test_warn_secret_gate_logs_each_introduced_region(tmp_path, caplog) -> None:
    fixture = build_vault_fixture(tmp_path)
    binding = _binding(fixture)
    warn_binding = VaultBinding(
        scope=binding.scope,
        scope_id=binding.scope_id,
        vault_id=binding.vault_id,
        root=binding.root,
        mapping=binding.mapping.model_copy(update={"secret_gate": "warn"}),
    )

    with caplog.at_level(logging.WARNING, logger=writer.__name__):
        _write(
            fixture,
            binding=warn_binding,
            body="Authorization: Bearer " + "a" * 16,
            cao={"note": "password: hunter2sixteen"},
        )

    secret_messages = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("vault_write_secret_warn")
    ]
    assert secret_messages == [
        "vault_write_secret_warn pattern=bearer_token region=body",
        "vault_write_secret_warn pattern=secret_assignment region=cao",
    ]


def test_write_refuses_read_only_mapping(tmp_path) -> None:
    fixture = build_vault_fixture(tmp_path)
    binding = _binding(fixture)
    read_only_binding = VaultBinding(
        scope=binding.scope,
        scope_id=binding.scope_id,
        vault_id=binding.vault_id,
        root=binding.root,
        mapping=binding.mapping.model_copy(update={"writable": False}),
    )

    with pytest.raises(ValueError) as caught:
        _write(fixture, binding=read_only_binding)

    assert str(caught.value) == "vault mapping 'CAO' is not writable"


@pytest.mark.parametrize(
    ("key", "message"),
    [
        ("../escape", "vault key must not contain a path separator: '../escape'"),
        ("/absolute", "vault key must not contain a path separator: '/absolute'"),
        ("nested/note", "vault key must not contain a path separator: 'nested/note'"),
        ("..", "vault key must not be '.' or '..': '..'"),
        (
            r"nested\note",
            "vault key must not contain a path separator: 'nested\\\\note'",
        ),
    ],
)
def test_write_rejects_every_key_path_escape_before_writing(tmp_path, key, message) -> None:
    fixture = build_vault_fixture(tmp_path)

    with pytest.raises(ValueError) as caught:
        _write(fixture, key=key)

    assert str(caught.value) == message
    assert not (tmp_path / "escape.md").exists()


def test_write_refuses_symlinked_managed_folder(tmp_path) -> None:
    fixture = build_vault_fixture(tmp_path)
    managed = fixture.root / "CAO"
    outside = tmp_path / "outside"
    outside.mkdir()
    (managed / ".keep").unlink()
    managed.rmdir()
    managed.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError) as caught:
        _write(fixture)

    assert "escapes base directory" in str(caught.value)
    assert list(outside.iterdir()) == []


def test_write_refuses_symlinked_managed_folder_ancestor(tmp_path) -> None:
    fixture = build_vault_fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_ancestor = fixture.root / "managed-link"
    linked_ancestor.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError) as caught:
        _write(
            fixture,
            vault=fixture.vault.model_copy(update={"managed_folder": "managed-link/CAO"}),
        )

    assert "escapes base directory" in str(caught.value)
    assert list(outside.iterdir()) == []


def test_read_sink_guard_raises_writer_containment_error(tmp_path) -> None:
    fixture = build_vault_fixture(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")

    with pytest.raises(ValueError, match="^vault write target escapes managed_folder$"):
        writer._read_contained_text(str(fixture.root / "CAO"), str(outside))


def test_mkstemp_sink_guard_raises_writer_containment_error(tmp_path) -> None:
    fixture = build_vault_fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(ValueError, match="^vault write target escapes managed_folder$"):
        writer._publish_managed_note(
            str(fixture.root),
            str(outside),
            str(outside / "target.md"),
            "content",
            0o644,
        )


def test_temp_open_sink_guard_raises_writer_containment_error(tmp_path, monkeypatch) -> None:
    fixture = build_vault_fixture(tmp_path)
    outside = tmp_path / "outside.tmp"
    outside.write_text("", encoding="utf-8")

    def escaped_mkstemp(*_args, **_kwargs):
        return os.open(outside, os.O_WRONLY), str(outside)

    def escaped_open(*_args, **_kwargs):
        raise RuntimeError("escaped temp open reached")

    monkeypatch.setattr(writer.tempfile, "mkstemp", escaped_mkstemp)
    monkeypatch.setattr(writer, "open", escaped_open, raising=False)

    with pytest.raises(ValueError, match="^vault write target escapes managed_folder$"):
        writer._publish_managed_note(
            str(fixture.root),
            str(fixture.root / "CAO"),
            str(fixture.root / "CAO" / "managed-note.md"),
            "content",
            0o644,
        )


def test_replace_sink_guard_raises_writer_containment_error(tmp_path) -> None:
    fixture = build_vault_fixture(tmp_path)
    outside = tmp_path / "outside.md"

    with pytest.raises(ValueError, match="^vault write target escapes managed_folder$"):
        writer._publish_managed_note(
            str(fixture.root),
            str(fixture.root / "CAO"),
            str(outside),
            "content",
            0o644,
        )


def test_cleanup_path_does_not_unlink_an_escaped_temp(tmp_path, monkeypatch) -> None:
    fixture = build_vault_fixture(tmp_path)
    outside = tmp_path / "outside.tmp"
    outside.write_text("do not unlink", encoding="utf-8")

    def escaped_mkstemp(*_args, **_kwargs):
        return os.open(outside, os.O_WRONLY), str(outside)

    monkeypatch.setattr(writer.tempfile, "mkstemp", escaped_mkstemp)

    with pytest.raises(ValueError, match="^vault write target escapes managed_folder$"):
        writer._publish_managed_note(
            str(fixture.root),
            str(fixture.root / "CAO"),
            str(fixture.root / "CAO" / "managed-note.md"),
            "content",
            0o644,
        )

    assert outside.read_text(encoding="utf-8") == "do not unlink"


def test_write_leaves_no_vault_debris_and_only_transient_reserved_temp(
    tmp_path, monkeypatch
) -> None:
    fixture = build_vault_fixture(tmp_path)
    before = {path.relative_to(fixture.root) for path in fixture.root.rglob("*")}
    observed: set[Path] = set()
    real_replace = writer.os.replace

    def inspect_then_replace(source: str, destination: str) -> None:
        current = {path.relative_to(fixture.root) for path in fixture.root.rglob("*")}
        observed.update(current - before)
        assert Path(destination).name == "managed-note.md"
        transient = {path for path in current - before if path.name.startswith("_cao-")}
        assert len(transient) == 1
        assert all(path.parent == Path("CAO") for path in transient)
        real_replace(source, destination)

    monkeypatch.setattr(writer.os, "replace", inspect_then_replace)

    _write(fixture)

    after = {path.relative_to(fixture.root) for path in fixture.root.rglob("*")}
    assert observed
    assert after - before == {Path("CAO/managed-note.md")}
    assert not [path for path in (fixture.root / "CAO").glob("_cao-*")]


def test_write_cleans_reserved_temp_when_publish_raises(tmp_path, monkeypatch) -> None:
    fixture = build_vault_fixture(tmp_path)

    def fail_replace(_source: str, _destination: str) -> None:
        raise OSError("injected publish failure")

    monkeypatch.setattr(writer.os, "replace", fail_replace)

    with pytest.raises(OSError, match="^injected publish failure$"):
        _write(fixture)

    assert not list((fixture.root / "CAO").glob("_cao-*"))
    assert not (fixture.root / "CAO" / "managed-note.md").exists()


def test_write_cleanup_does_not_mask_publish_failure(tmp_path, monkeypatch) -> None:
    fixture = build_vault_fixture(tmp_path)

    def fail_replace(_source: str, _destination: str) -> None:
        raise OSError("injected publish failure")

    def fail_unlink(_path: str) -> None:
        raise OSError("injected cleanup failure")

    monkeypatch.setattr(writer.os, "replace", fail_replace)
    monkeypatch.setattr(writer.os, "unlink", fail_unlink)

    with pytest.raises(OSError, match="^injected publish failure$"):
        _write(fixture)


def test_write_fsyncs_managed_folder_after_replace(tmp_path, monkeypatch) -> None:
    fixture = build_vault_fixture(tmp_path)
    observed_modes: list[int] = []
    real_fsync = writer.os.fsync

    def record_fsync(fd: int) -> None:
        observed_modes.append(stat.S_IFMT(os.fstat(fd).st_mode))
        real_fsync(fd)

    monkeypatch.setattr(writer.os, "fsync", record_fsync)

    _write(fixture)

    assert stat.S_IFDIR in observed_modes


def test_write_raises_partial_write_after_durable_publish(tmp_path) -> None:
    fixture = build_vault_fixture(tmp_path)

    def fail_refresh(_target: str) -> None:
        raise RuntimeError("injected refresh failure")

    with pytest.raises(MemoryPartialWriteError) as caught:
        _write(fixture, refresh=fail_refresh)

    assert str(caught.value) == (
        "Memory content and index were saved, but SQLite metadata could not be updated. "
        "Run `cao memory repair --apply`."
    )
    assert (
        (fixture.root / "CAO" / "managed-note.md")
        .read_text(encoding="utf-8")
        .endswith("new body\n")
    )


def test_write_preserves_existing_mode_and_uses_umask_for_new_note(tmp_path) -> None:
    fixture = build_vault_fixture(tmp_path)
    target = fixture.root / "CAO" / "managed-note.md"
    original = "---\ntitle: before\n---\nbody\n"
    target.write_text(original, encoding="utf-8")
    target.chmod(0o640)

    _write(fixture, expected_content_sha256=writer._sha256(original))

    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    target.unlink()
    old_umask = os.umask(0o022)
    try:
        _write(fixture)
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_vault_writer_owns_nonempty_vault_write_sink_set() -> None:
    source_root = (
        Path(__file__).parents[3] / "src" / "cli_agent_orchestrator" / "services" / "vault"
    )
    violations: list[str] = []
    writer_sinks: list[str] = []

    for path in source_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                name = node.func.id
                qualified_owner = ""
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
                qualified_owner = (
                    node.func.value.id if isinstance(node.func.value, ast.Name) else ""
                )
            else:
                continue
            open_mode = (
                node.args[1]
                if len(node.args) > 1
                else next(
                    (keyword.value for keyword in node.keywords if keyword.arg == "mode"),
                    None,
                )
            )
            writes_via_open = (
                name == "open"
                and isinstance(open_mode, ast.Constant)
                and isinstance(open_mode.value, str)
                and any(mode in open_mode.value for mode in ("w", "a", "x", "+"))
            )
            known_write_sink = (
                name in {"write_text", "write_bytes"}
                # This focused sweep guards direct vault filesystem mutation. It
                # intentionally does not classify every stdlib write helper:
                # shutil.copy, os.symlink, os.truncate and Path.open are outside
                # this direct-sink allowlist.
                or (qualified_owner == "os" and name in {"makedirs", "rename", "replace", "unlink"})
                or (qualified_owner == "tempfile" and name == "mkstemp")
            )
            if not known_write_sink and not writes_via_open:
                continue
            location = f"{path.name}:{node.lineno}:{name}"
            if path.name == "writer.py":
                writer_sinks.append(location)
            else:
                violations.append(location)

    assert writer_sinks, "writer.py must own at least one vault write sink"
    assert violations == []
