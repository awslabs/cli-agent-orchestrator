"""Create/update operations for YAML workflow specs (the workflow builder's save path).

Guards ``create_yaml_workflow``, ``update_yaml_workflow`` and ``get_workflow_document``. They
mirror the Python tier's ``create_workflow`` / ``update_workflow``: the same lock, the same
guarded writer, the same index upsert, and the same rule that every refusal leaves the directory
byte-identical. What differs is the validator (the text-only ``models.workflow.validate_only``)
and the existence rules, which span both tiers.

As in ``test_workflow_spec_create_update.py``, every refusal is checked with ``os.listdir`` so a
leftover temp dotfile would show up, and a successful write is followed through the real read
path rather than stopping at "the file exists".
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

import pytest

from cli_agent_orchestrator.clients.database import _migrate_workflow_index
from cli_agent_orchestrator.constants import WORKFLOW_MAX_SPEC_BYTES
from cli_agent_orchestrator.models.workflow import (
    ScriptSpec,
    StaleSpecError,
    TierCollisionError,
    WorkflowSpec,
)
from cli_agent_orchestrator.services import workflow_spec_service as svc


@pytest.fixture(autouse=True)
def isolated_index(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the real index against a fresh DB, independent of the CAO install."""
    db_path = tmp_path_factory.mktemp("wf-yaml-index") / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path)
    _migrate_workflow_index()


def _spec_yaml(name: str, prompt: str = "do it", *, mode: str = "sequential") -> str:
    """A spec in the builder's style: every key and string value JSON-quoted."""
    return (
        f'"name": "{name}"\n'
        '"description": "a builder spec"\n'
        f'"mode": "{mode}"\n'
        '"steps":\n'
        '  - "id": "only-step"\n'
        '    "provider": "claude_code"\n'
        '    "agent": "developer"\n'
        f'    "prompt": "{prompt}"\n'
    )


PYTHON_SOURCE = "INPUTS = {}\n"


def _entries(base: Path) -> list[str]:
    return sorted(os.listdir(base))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# The write target: the service picks the extension from the tier
# ---------------------------------------------------------------------------
def test_the_tier_picks_the_extension(tmp_path: Path) -> None:
    base = os.path.realpath(tmp_path)
    assert svc._validate_write_target("wf", str(tmp_path), "yaml")[1] == os.path.join(
        base, "wf.yaml"
    )
    assert svc._validate_write_target("wf", str(tmp_path), "python")[1] == os.path.join(
        base, "wf.py"
    )
    assert svc._validate_write_target("wf", str(tmp_path))[1] == os.path.join(
        base, "wf.py"
    ), "python stays the default, so every existing caller is unchanged"


def test_an_unknown_tier_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="tier 'toml' is invalid"):
        svc._validate_write_target("wf", str(tmp_path), "toml")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "name,extension",
    [
        ("thing.yaml", ".yaml"),
        ("thing.yml", ".yml"),
        ("THING.YAML", ".yaml"),
        ("thing.py", ".py"),
    ],
)
def test_a_name_with_an_extension_is_refused_in_both_tiers(
    tmp_path: Path, name: str, extension: str
) -> None:
    """Names are bare in both tiers. The refusal no longer claims YAML cannot be written."""
    expected = f"pass a bare workflow name without the '{re.escape(extension)}' extension"
    with pytest.raises(ValueError, match=expected) as python_refusal:
        svc.create_workflow(name, PYTHON_SOURCE, scan_dir=str(tmp_path))
    with pytest.raises(ValueError, match=expected):
        svc.create_yaml_workflow(name, _spec_yaml("thing"), scan_dir=str(tmp_path))
    assert "cannot be created" not in str(python_refusal.value)
    assert _entries(tmp_path) == []


# ---------------------------------------------------------------------------
# Create: happy path and the round trip through the real read path
# ---------------------------------------------------------------------------
def test_create_writes_name_yaml_and_reports_exactly_what_landed(tmp_path: Path) -> None:
    source = _spec_yaml("built")
    written = svc.create_yaml_workflow("built", source, scan_dir=str(tmp_path))

    on_disk = (tmp_path / "built.yaml").read_bytes()
    assert on_disk == source.encode("utf-8"), "the bytes validated are the bytes written"
    assert _entries(tmp_path) == ["built.yaml"], "no temp file, and no .py"
    assert written.path == os.path.realpath(tmp_path / "built.yaml")
    assert written.content_hash == _sha(on_disk)
    assert written.validation.status == "pass"
    assert written.validation.reserved_notes == []
    assert written.spec.name == "built"


def test_a_created_spec_is_gettable_and_listable(tmp_path: Path) -> None:
    svc.create_yaml_workflow("round-trip", _spec_yaml("round-trip"), scan_dir=str(tmp_path))

    got = svc.get_workflow("round-trip", scan_dir=str(tmp_path))
    assert isinstance(got, WorkflowSpec)
    assert got.steps[0].agent == "developer"

    rows = {row.name: row for row in svc.list_workflows(scan_dir=str(tmp_path))}
    assert rows["round-trip"].source_path.endswith("round-trip.yaml")
    assert rows["round-trip"].mode == "sequential"


def test_a_new_yaml_spec_gets_the_create_mode_not_0600(tmp_path: Path) -> None:
    svc.create_yaml_workflow("moded", _spec_yaml("moded"), scan_dir=str(tmp_path))
    mode = stat.S_IMODE((tmp_path / "moded.yaml").stat().st_mode)
    assert mode == svc._SPEC_FILE_CREATE_MODE


def test_a_reserved_construct_is_written_and_reported_as_pass_reserved(tmp_path: Path) -> None:
    """A reserved spec is valid. It is written, and the caller is told it will not run yet."""
    written = svc.create_yaml_workflow(
        "later", _spec_yaml("later", mode="parallel"), scan_dir=str(tmp_path)
    )

    assert written.validation.status == "pass_reserved"
    assert any("not built yet" in note for note in written.validation.reserved_notes)
    assert (tmp_path / "later.yaml").exists()


# ---------------------------------------------------------------------------
# Create: existence spans every tier, and nothing is written on refusal
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("existing", ["dup.yaml", "dup.yml"])
def test_create_refuses_an_existing_yaml_spec_and_leaves_it_alone(
    tmp_path: Path, existing: str
) -> None:
    original = _spec_yaml("dup", "the original")
    (tmp_path / existing).write_text(original)

    with pytest.raises(FileExistsError, match="already exists"):
        svc.create_yaml_workflow("dup", _spec_yaml("dup", "a replacement"), scan_dir=str(tmp_path))

    assert (tmp_path / existing).read_text() == original
    assert _entries(tmp_path) == [existing]


def test_create_refuses_a_python_sibling_as_a_tier_collision(tmp_path: Path) -> None:
    """Without this check the YAML file would win the index and hide the Python spec."""
    (tmp_path / "clash.py").write_text(PYTHON_SOURCE)

    with pytest.raises(TierCollisionError):
        svc.create_yaml_workflow("clash", _spec_yaml("clash"), scan_dir=str(tmp_path))

    assert _entries(tmp_path) == ["clash.py"], "no .yaml written, no temp file"


def test_python_create_still_refuses_a_builder_written_yaml_sibling(tmp_path: Path) -> None:
    svc.create_yaml_workflow("both", _spec_yaml("both"), scan_dir=str(tmp_path))

    with pytest.raises(TierCollisionError):
        svc.create_workflow("both", PYTHON_SOURCE, scan_dir=str(tmp_path))

    assert _entries(tmp_path) == ["both.yaml"]


@pytest.mark.parametrize(
    "bad_name",
    ["with/slash", "..", ".", "has space", "way-too-long" * 10, "unicodeé", "trailing\n"],
)
def test_create_rejects_an_invalid_name_without_writing(tmp_path: Path, bad_name: str) -> None:
    with pytest.raises(ValueError):
        svc.create_yaml_workflow(bad_name, _spec_yaml("ok"), scan_dir=str(tmp_path))
    assert _entries(tmp_path) == []


@pytest.mark.parametrize(
    "source,fragment",
    [
        pytest.param("name: [unclosed\n", "malformed YAML", id="does-not-parse"),
        pytest.param("- a\n- list\n", "mapping", id="not-a-mapping"),
        pytest.param('"name": "bad-steps"\n"steps": []\n', "at least one step", id="no-steps"),
    ],
)
def test_create_refuses_yaml_that_fails_validation(
    tmp_path: Path, source: str, fragment: str
) -> None:
    with pytest.raises(ValueError, match="is not a valid YAML spec") as excinfo:
        svc.create_yaml_workflow("bad-steps", source, scan_dir=str(tmp_path))
    assert fragment in str(excinfo.value)
    assert _entries(tmp_path) == []


def test_a_grammar_refusal_lists_every_error(tmp_path: Path) -> None:
    """The 400 detail carries the aggregated grammar errors, not just the first."""
    source = (
        '"name": "agg"\n'
        '"steps":\n'
        '  - {"id": "dup", "provider": "p", "agent": "a", "prompt": "x"}\n'
        '  - {"id": "dup", "provider": "p", "agent": "a", "prompt": "y"}\n'
        '  - {"id": "bad id!", "provider": "p", "agent": "a", "prompt": "z"}\n'
    )
    with pytest.raises(ValueError) as excinfo:
        svc.create_yaml_workflow("agg", source, scan_dir=str(tmp_path))
    detail = str(excinfo.value)
    assert "duplicate step id 'dup'" in detail
    assert "step id 'bad id!' is invalid" in detail
    assert _entries(tmp_path) == []


def test_create_refuses_a_spec_whose_name_is_not_the_workflow_name(tmp_path: Path) -> None:
    """The index is keyed by ``name:`` and the file by the route name, so they must agree."""
    with pytest.raises(ValueError, match="declares name 'other'"):
        svc.create_yaml_workflow("mine", _spec_yaml("other"), scan_dir=str(tmp_path))
    assert _entries(tmp_path) == []


def test_create_refuses_an_oversize_source_with_the_stable_message(tmp_path: Path) -> None:
    padding = "#" + "x" * WORKFLOW_MAX_SPEC_BYTES + "\n"
    with pytest.raises(
        ValueError, match=re.escape(f"spec exceeds {WORKFLOW_MAX_SPEC_BYTES} bytes")
    ):
        svc.create_yaml_workflow("huge", padding + _spec_yaml("huge"), scan_dir=str(tmp_path))
    assert _entries(tmp_path) == []


def test_create_refuses_source_that_is_not_utf8_encodable(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="valid UTF-8"):
        svc.create_yaml_workflow(
            "surrogate", _spec_yaml("surrogate", "\ud800"), scan_dir=str(tmp_path)
        )
    assert _entries(tmp_path) == []


def test_create_refuses_a_dangling_symlink_at_the_target(tmp_path: Path) -> None:
    """The guarded writer refuses to publish through a symlinked name (SR-3A2-2)."""
    (tmp_path / "linked.yaml").symlink_to(tmp_path / "missing-target.yaml")

    with pytest.raises(svc.SpecPathRefusedError, match="symlink"):
        svc.create_yaml_workflow("linked", _spec_yaml("linked"), scan_dir=str(tmp_path))

    assert not (tmp_path / "missing-target.yaml").exists()
    assert _entries(tmp_path) == ["linked.yaml"]


def test_a_failed_index_upsert_logs_and_still_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The file is canonical; the index rebuilds from disk on the next list (BR-3A3-12)."""

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected index failure")

    original_upsert = svc.upsert_index
    monkeypatch.setattr(svc, "upsert_index", _boom)
    with caplog.at_level("WARNING"):
        written = svc.create_yaml_workflow(
            "resilient", _spec_yaml("resilient"), scan_dir=str(tmp_path)
        )

    assert written.spec.name == "resilient"
    assert any("index row could not be updated" in r.getMessage() for r in caplog.records)
    monkeypatch.setattr(svc, "upsert_index", original_upsert)
    assert "resilient" in [row.name for row in svc.list_workflows(scan_dir=str(tmp_path))]


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("extension", [".yaml", ".yml"])
def test_update_replaces_the_file_at_its_own_path(tmp_path: Path, extension: str) -> None:
    """A ``.yml`` spec stays ``.yml``: an update never renames the file."""
    target = tmp_path / f"edit-me{extension}"
    target.write_text(_spec_yaml("edit-me", "first"))
    new_source = _spec_yaml("edit-me", "second")

    written = svc.update_yaml_workflow(
        "edit-me", new_source, _sha(target.read_bytes()), scan_dir=str(tmp_path)
    )

    assert target.read_text() == new_source
    assert _entries(tmp_path) == [f"edit-me{extension}"]
    assert written.path == os.path.realpath(target)
    assert written.content_hash == _sha(target.read_bytes())
    assert svc.get_workflow("edit-me", scan_dir=str(tmp_path)).steps[0].prompt == "second"


def test_the_returned_hash_is_accepted_by_the_next_update(tmp_path: Path) -> None:
    created = svc.create_yaml_workflow("chain", _spec_yaml("chain", "v1"), scan_dir=str(tmp_path))
    second = svc.update_yaml_workflow(
        "chain", _spec_yaml("chain", "v2"), created.content_hash, scan_dir=str(tmp_path)
    )
    svc.update_yaml_workflow(
        "chain", _spec_yaml("chain", "v3"), second.content_hash, scan_dir=str(tmp_path)
    )
    assert svc.get_workflow("chain", scan_dir=str(tmp_path)).steps[0].prompt == "v3"


def test_update_preserves_the_existing_file_mode(tmp_path: Path) -> None:
    target = tmp_path / "moded.yaml"
    target.write_text(_spec_yaml("moded", "first"))
    os.chmod(target, 0o640)

    svc.update_yaml_workflow(
        "moded", _spec_yaml("moded", "second"), _sha(target.read_bytes()), scan_dir=str(tmp_path)
    )

    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_update_refuses_a_stale_hash_and_keeps_the_newer_content(tmp_path: Path) -> None:
    created = svc.create_yaml_workflow("raced", _spec_yaml("raced", "v1"), scan_dir=str(tmp_path))
    svc.update_yaml_workflow(
        "raced", _spec_yaml("raced", "v2"), created.content_hash, scan_dir=str(tmp_path)
    )

    with pytest.raises(StaleSpecError):
        svc.update_yaml_workflow(
            "raced", _spec_yaml("raced", "v3"), created.content_hash, scan_dir=str(tmp_path)
        )
    assert svc.get_workflow("raced", scan_dir=str(tmp_path)).steps[0].prompt == "v2"


def test_a_non_ascii_expected_hash_is_stale_rather_than_a_type_error(tmp_path: Path) -> None:
    """``hmac.compare_digest`` raises TypeError on a non-ASCII str; that must not escape as a 500."""
    svc.create_yaml_workflow("ascii", _spec_yaml("ascii"), scan_dir=str(tmp_path))
    with pytest.raises(StaleSpecError):
        svc.update_yaml_workflow("ascii", _spec_yaml("ascii"), "é" * 64, scan_dir=str(tmp_path))


def test_update_of_a_missing_workflow_is_not_found_and_creates_nothing(tmp_path: Path) -> None:
    with pytest.raises(svc.WorkflowNotFoundError, match="does not exist"):
        svc.update_yaml_workflow("absent", _spec_yaml("absent"), "0" * 64, scan_dir=str(tmp_path))
    assert _entries(tmp_path) == []


def test_update_of_a_python_workflow_is_a_conflict_that_says_so(tmp_path: Path) -> None:
    (tmp_path / "scripted.py").write_text(PYTHON_SOURCE)

    with pytest.raises(svc.WorkflowWriteConflictError, match="is a Python workflow"):
        svc.update_yaml_workflow(
            "scripted",
            _spec_yaml("scripted"),
            _sha(PYTHON_SOURCE.encode("utf-8")),
            scan_dir=str(tmp_path),
        )
    assert _entries(tmp_path) == ["scripted.py"]
    assert (tmp_path / "scripted.py").read_text() == PYTHON_SOURCE


@pytest.mark.parametrize("filename", ["built.yaml", "built.yml"])
def test_python_update_of_a_yaml_workflow_is_a_conflict_that_says_so(
    tmp_path: Path, filename: str
) -> None:
    """The mirror of the case above (review of #1291): not "does not exist"."""
    yaml_file = tmp_path / filename
    yaml_file.write_text(_spec_yaml("built"))

    with pytest.raises(
        svc.WorkflowWriteConflictError,
        match=r"^workflow 'built' is a YAML workflow; update it with tier 'yaml'$",
    ):
        svc.update_workflow(
            "built", PYTHON_SOURCE, _sha(yaml_file.read_bytes()), scan_dir=str(tmp_path)
        )
    assert _entries(tmp_path) == [filename]
    assert yaml_file.read_text() == _spec_yaml("built")


def test_python_update_of_a_missing_workflow_is_still_not_found(tmp_path: Path) -> None:
    with pytest.raises(svc.WorkflowNotFoundError, match="does not exist"):
        svc.update_workflow("absent", PYTHON_SOURCE, "0" * 64, scan_dir=str(tmp_path))
    assert _entries(tmp_path) == []


def test_update_with_a_python_sibling_is_a_tier_collision(tmp_path: Path) -> None:
    yaml_file = tmp_path / "both.yaml"
    yaml_file.write_text(_spec_yaml("both", "v1"))
    (tmp_path / "both.py").write_text(PYTHON_SOURCE)

    with pytest.raises(TierCollisionError):
        svc.update_yaml_workflow(
            "both", _spec_yaml("both", "v2"), _sha(yaml_file.read_bytes()), scan_dir=str(tmp_path)
        )
    assert yaml_file.read_text() == _spec_yaml("both", "v1")


def test_update_with_both_yaml_and_yml_is_refused_as_ambiguous(tmp_path: Path) -> None:
    (tmp_path / "twin.yaml").write_text(_spec_yaml("twin", "a"))
    (tmp_path / "twin.yml").write_text(_spec_yaml("twin", "b"))

    with pytest.raises(svc.WorkflowWriteConflictError, match="both 'twin.yaml' and 'twin.yml'"):
        svc.update_yaml_workflow(
            "twin",
            _spec_yaml("twin", "c"),
            _sha((tmp_path / "twin.yaml").read_bytes()),
            scan_dir=str(tmp_path),
        )
    assert (tmp_path / "twin.yaml").read_text() == _spec_yaml("twin", "a")
    assert (tmp_path / "twin.yml").read_text() == _spec_yaml("twin", "b")


@pytest.mark.parametrize(
    "new_source,fragment",
    [
        pytest.param("name: [unclosed\n", "malformed YAML", id="does-not-parse"),
        pytest.param(_spec_yaml("someone-else"), "declares name 'someone-else'", id="renamed"),
    ],
)
def test_update_refuses_invalid_source_and_leaves_the_original(
    tmp_path: Path, new_source: str, fragment: str
) -> None:
    created = svc.create_yaml_workflow("keep", _spec_yaml("keep"), scan_dir=str(tmp_path))

    with pytest.raises(ValueError) as excinfo:
        svc.update_yaml_workflow("keep", new_source, created.content_hash, scan_dir=str(tmp_path))

    assert fragment in str(excinfo.value)
    assert (tmp_path / "keep.yaml").read_text() == _spec_yaml("keep")
    assert _entries(tmp_path) == ["keep.yaml"]


def test_update_refuses_to_write_through_a_symlink_to_another_spec(tmp_path: Path) -> None:
    """Both paths are contained, so only the writer's symlink refusal protects ``real.yaml``."""
    real = tmp_path / "real.yaml"
    real.write_text(_spec_yaml("alias", "the real spec"))
    (tmp_path / "alias.yaml").symlink_to(real)

    with pytest.raises(svc.SpecPathRefusedError, match="symlink"):
        svc.update_yaml_workflow(
            "alias",
            _spec_yaml("alias", "clobbered"),
            _sha(real.read_bytes()),
            scan_dir=str(tmp_path),
        )

    assert real.read_text() == _spec_yaml("alias", "the real spec")


# ---------------------------------------------------------------------------
# The GET document
# ---------------------------------------------------------------------------
def test_the_yaml_document_adds_path_source_and_hash_from_one_read(tmp_path: Path) -> None:
    source = _spec_yaml("doc")
    svc.create_yaml_workflow("doc", source, scan_dir=str(tmp_path))

    document = svc.get_workflow_document("doc", scan_dir=str(tmp_path))

    spec_fields = svc.get_workflow("doc", scan_dir=str(tmp_path)).model_dump()
    assert {key: document[key] for key in spec_fields} == spec_fields
    assert set(document) == set(spec_fields) | {"path", "source", "content_hash"}
    assert document["path"] == os.path.realpath(tmp_path / "doc.yaml")
    assert document["source"] == source
    assert document["content_hash"] == _sha((tmp_path / "doc.yaml").read_bytes())


def test_the_document_hash_is_the_one_update_compares(tmp_path: Path) -> None:
    (tmp_path / "hand-written.yml").write_text(_spec_yaml("hand-written", "v1"))

    document = svc.get_workflow_document("hand-written", scan_dir=str(tmp_path))
    svc.update_yaml_workflow(
        "hand-written",
        _spec_yaml("hand-written", "v2"),
        document["content_hash"],
        scan_dir=str(tmp_path),
    )

    assert svc.get_workflow_document("hand-written", scan_dir=str(tmp_path))["source"] == (
        _spec_yaml("hand-written", "v2")
    )


def test_the_python_document_is_unchanged(tmp_path: Path) -> None:
    svc.create_workflow("scripted", PYTHON_SOURCE, scan_dir=str(tmp_path))

    document = svc.get_workflow_document("scripted", scan_dir=str(tmp_path))

    assert document == svc.get_workflow("scripted", scan_dir=str(tmp_path)).model_dump()
    assert set(document) == set(ScriptSpec.model_fields)


def test_the_document_raises_what_get_workflow_raises(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        svc.get_workflow_document("never-written", scan_dir=str(tmp_path))
    with pytest.raises(ValueError):
        svc.get_workflow_document("bad name!", scan_dir=str(tmp_path))
