"""Behavior tests for the FUTURE_IMPROVEMENTS migration importer.

``future_improvements_import`` is the digest-bound, transactional importer for
the feature-request system. Its parser, manifest validator, dry-run planner, and
atomic applier each carry explicit refusal contracts that were previously
exercised only by a handful of dry-run / high-watermark checks. These tests pin
the real behavior of the genuinely uncovered mainline branches: validation
refusals, file/encoding/JSON error paths, the wrapped-title parser, supplement
de-duplication, and every ``apply_manifest`` action (create-feature,
create-terminal-feature, map-existing, relate-existing, skip-invalid) including
idempotent replay and conflicting-bytes refusal.
"""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy import text as sa_text
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services import future_improvements_import as fii
from cli_agent_orchestrator.services import issue_tracker as tracker
from cli_agent_orchestrator.services.issue_tracker import TrackerError


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    """Isolated in-process SQLite for the tracker + importer."""
    engine = create_engine(
        f"sqlite:///{tmp_path}/fii.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(tracker, "SessionLocal", sessionmaker(bind=engine))
    import cli_agent_orchestrator.clients.database as dbmod

    monkeypatch.setattr(dbmod, "engine", engine)
    yield engine
    engine.dispose()


@pytest.fixture
def cao_system(tmp_path):
    conductor = tmp_path / "cao-conductor"
    fork = tmp_path / "cli-agent-orchestrator"
    conductor.mkdir()
    fork.mkdir()
    tracker.create_project(
        name="CAO System",
        project_id="cao-system",
        issue_prefix="cond",
        scopes=[
            {"kind": "path", "value": str(conductor)},
            {"kind": "path", "value": str(fork)},
        ],
    )


def _write_source(tmp_path, body="# Roadmap P2\n\n- **a feature**\n  body text\n"):
    source = tmp_path / "FUTURE.md"
    source.write_text(body, encoding="utf-8")
    return source


def _plan_for(source, **kwargs):
    return fii.dry_run(source_path=str(source), project_id="cao-system", **kwargs)


def _manifest_from_plan(plan, candidates):
    """Build an adjudicated manifest (explicit actions) from a dry-run plan."""
    return {
        "source_sha256": plan["source_sha256"],
        "project": "cao-system",
        "target_project": "cao-system",
        "candidates": candidates,
    }


def _candidate(plan, idx=0, **overrides):
    cand = dict(plan["candidates"][idx])
    cand["action"] = "create-feature"
    cand["priority"] = "P2"
    cand["status"] = "open"
    cand["labels"] = ["roadmap", "source:future-improvements"]
    cand["target_project"] = "cao-system"
    cand.update(overrides)
    return cand


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class TestParser:
    def test_heading_binds_priority_to_following_bullets(self, tmp_path):
        text = "# Section P1\n\n- **alpha**\n\n# Other P3\n\n- **beta**\n"
        cands = fii.parse_future_improvements_markdown(text, "d" * 64)
        assert [c["priority"] for c in cands] == ["P1", "P3"]
        assert [c["title"] for c in cands] == ["alpha", "beta"]

    def test_non_priority_heading_resets_to_unset(self):
        text = "# P2 work\n\n- **alpha**\n\n## Deferred notes\n\n- **beta**\n"
        cands = fii.parse_future_improvements_markdown(text, "d" * 64)
        assert [c["priority"] for c in cands] == ["P2", "unset"]

    def test_wrapped_bold_title_spanning_lines_is_joined(self):
        text = "# P2\n\n- **a title that\nspans more than\none line**\n  body\n"
        cands = fii.parse_future_improvements_markdown(text, "d" * 64)
        assert len(cands) == 1
        assert cands[0]["title"] == "a title that spans more than one line"
        assert cands[0]["body"] == "body"

    def test_prose_and_nested_bullets_are_not_candidates(self):
        text = "# P2\n\nSome prose line.\n\n- **real**\n\n  - nested explanatory bullet\n"
        cands = fii.parse_future_improvements_markdown(text, "d" * 64)
        assert [c["title"] for c in cands] == ["real"]

    def test_multiline_body_preserved_verbatim(self):
        text = "# P2\n\n- **real**\nfirst body line\nsecond body line\n\n- **next**\n"
        cands = fii.parse_future_improvements_markdown(text, "d" * 64)
        assert cands[0]["body"] == "first body line\nsecond body line"


# ---------------------------------------------------------------------------
# parse_source_file / _load_manifest error paths
# ---------------------------------------------------------------------------


class TestSourceAndManifestLoading:
    def test_parse_source_not_a_regular_file_refuses(self, tmp_path):
        with pytest.raises(TrackerError) as exc:
            fii.parse_source_file(str(tmp_path / "missing.md"))
        assert exc.value.code == "invalid"

    def test_parse_source_non_utf8_refuses(self, tmp_path):
        source = tmp_path / "bad.md"
        source.write_bytes(b"# P1\n\n- **\xff\xfe bad**\n")
        with pytest.raises(TrackerError) as exc:
            fii.parse_source_file(str(source))
        assert exc.value.code == "invalid"

    def test_load_manifest_missing_file_refuses(self, tmp_path):
        with pytest.raises(TrackerError) as exc:
            fii._load_manifest(str(tmp_path / "missing.json"))
        assert exc.value.code == "invalid"

    def test_load_manifest_non_utf8_refuses(self, tmp_path):
        manifest = tmp_path / "bad.json"
        manifest.write_bytes(b"\xff\xfe not utf8")
        with pytest.raises(TrackerError) as exc:
            fii._load_manifest(str(manifest))
        assert exc.value.code == "invalid"

    def test_load_manifest_invalid_json_refuses(self, tmp_path):
        manifest = tmp_path / "bad.json"
        manifest.write_text("{not json", encoding="utf-8")
        with pytest.raises(TrackerError) as exc:
            fii._load_manifest(str(manifest))
        assert exc.value.code == "invalid"

    def test_load_manifest_non_object_refuses(self, tmp_path):
        manifest = tmp_path / "arr.json"
        manifest.write_text("[]", encoding="utf-8")
        with pytest.raises(TrackerError) as exc:
            fii._load_manifest(str(manifest))
        assert exc.value.code == "invalid"


# ---------------------------------------------------------------------------
# validate_manifest refusal contracts
# ---------------------------------------------------------------------------


def _base_valid_manifest(source_sha, **candidate_overrides):
    cand = {
        "migration_id": "x" * 8,
        "ordinal": 1,
        "title": "t",
        "body": "b",
        "priority": "P2",
        "status": "open",
        "action": "create-feature",
        "labels": ["roadmap"],
    }
    cand.update(candidate_overrides)
    return {"source_sha256": source_sha, "candidates": [cand]}


class TestValidateManifest:
    def test_valid_create_feature_manifest_passes(self):
        m = _base_valid_manifest("a" * 64)
        fii.validate_manifest(m)  # no raise

    def test_missing_candidates_refuses(self):
        with pytest.raises(TrackerError) as exc:
            fii.validate_manifest({"source_sha256": "a" * 64})
        assert exc.value.code == "invalid"

    def test_empty_candidates_refuses(self):
        with pytest.raises(TrackerError) as exc:
            fii.validate_manifest({"source_sha256": "a" * 64, "candidates": []})
        assert exc.value.code == "invalid"

    def test_candidate_missing_action_refuses(self):
        m = _base_valid_manifest("a" * 64)
        del m["candidates"][0]["action"]
        with pytest.raises(TrackerError) as exc:
            fii.validate_manifest(m)
        assert exc.value.code == "invalid"

    def test_candidate_only_proposed_action_refuses(self):
        m = _base_valid_manifest("a" * 64)
        del m["candidates"][0]["action"]
        m["candidates"][0]["proposed_action"] = "create-feature"
        with pytest.raises(TrackerError) as exc:
            fii.validate_manifest(m)
        assert exc.value.code == "invalid"

    def test_invalid_action_refuses(self):
        m = _base_valid_manifest("a" * 64, action="nope")
        with pytest.raises(TrackerError) as exc:
            fii.validate_manifest(m)
        assert exc.value.code == "invalid"

    def test_adjudication_sentinel_action_refuses(self):
        m = _base_valid_manifest("a" * 64, action=fii.ADJUDICATION_SENTINEL)
        with pytest.raises(TrackerError) as exc:
            fii.validate_manifest(m)
        assert exc.value.code == "invalid"

    def test_adjudication_sentinel_in_labels_refuses(self):
        m = _base_valid_manifest("a" * 64, labels=["roadmap", fii.ADJUDICATION_SENTINEL])
        with pytest.raises(TrackerError) as exc:
            fii.validate_manifest(m)
        assert exc.value.code == "invalid"

    def test_terminal_feature_requires_terminal_status(self):
        m = _base_valid_manifest("a" * 64, action="create-terminal-feature", status="open")
        with pytest.raises(TrackerError) as exc:
            fii.validate_manifest(m)
        assert exc.value.code == "invalid"

    def test_terminal_feature_requires_resolution(self):
        m = _base_valid_manifest(
            "a" * 64, action="create-terminal-feature", status="closed", resolution=""
        )
        with pytest.raises(TrackerError) as exc:
            fii.validate_manifest(m)
        assert exc.value.code == "invalid"

    def test_skip_invalid_requires_rationale(self):
        m = _base_valid_manifest("a" * 64, action="skip-invalid")
        with pytest.raises(TrackerError) as exc:
            fii.validate_manifest(m)
        assert exc.value.code == "invalid"

    def test_map_existing_requires_canonical_key(self):
        m = _base_valid_manifest("a" * 64, action="map-existing")
        with pytest.raises(TrackerError) as exc:
            fii.validate_manifest(m)
        assert exc.value.code == "invalid"

    def test_map_existing_invalid_canonical_format_refuses(self):
        m = _base_valid_manifest("a" * 64, action="map-existing", canonical_key="UPPER CASE BAD")
        with pytest.raises(TrackerError) as exc:
            fii.validate_manifest(m)
        assert exc.value.code == "invalid"

    def test_relate_existing_invalid_related_key_refuses(self):
        m = _base_valid_manifest(
            "a" * 64,
            action="relate-existing",
            related_keys=["Not A Valid Key!"],
        )
        with pytest.raises(TrackerError) as exc:
            fii.validate_manifest(m)
        assert exc.value.code == "invalid"

    def test_source_sha_mismatch_refuses(self):
        m = _base_valid_manifest("a" * 64)
        with pytest.raises(TrackerError) as exc:
            fii.validate_manifest(m, expected_source_sha256="b" * 64)
        assert exc.value.code == "conflict"

    def test_supplement_sha_mismatch_refuses(self):
        m = _base_valid_manifest("a" * 64)
        m["supplement_sha256"] = "c" * 64
        with pytest.raises(TrackerError) as exc:
            fii.validate_manifest(m, expected_supplement_sha256="d" * 64)
        assert exc.value.code == "conflict"

    def test_missing_source_sha_refuses(self):
        m = _base_valid_manifest("a" * 64)
        del m["source_sha256"]
        with pytest.raises(TrackerError) as exc:
            fii.validate_manifest(m)
        assert exc.value.code == "invalid"


# ---------------------------------------------------------------------------
# dry_run planning
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_supplement_titles_deduped_against_source(self, tmp_path, cao_system):
        source = _write_source(tmp_path)
        supplement = tmp_path / "SUP.md"
        # One duplicate title + one genuinely new title.
        supplement.write_text(
            "# P3\n\n- **a feature**\n  dup\n\n- **brand new idea**\n  fresh\n",
            encoding="utf-8",
        )
        plan = _plan_for(source, supplement_path=str(supplement))
        titles = [c["title"] for c in plan["candidates"]]
        assert "a feature" in titles  # source copy retained
        assert "brand new idea" in titles  # supplement kept
        assert titles.count("a feature") == 1  # supplement dup dropped
        assert plan["supplement_sha256"] is not None

    def test_inventory_write_is_atomic(self, tmp_path, cao_system):
        source = _write_source(tmp_path)
        out = tmp_path / "inv.json"
        plan = _plan_for(source, inventory_out=str(out))
        assert out.is_file()
        written = json.loads(out.read_text(encoding="utf-8"))
        assert written["source_sha256"] == plan["source_sha256"]
        assert not out.with_suffix(out.suffix + ".tmp").exists()


# ---------------------------------------------------------------------------
# apply_manifest — the atomic importer
# ---------------------------------------------------------------------------


class TestApplyManifest:
    def test_create_feature_allocates_key_and_writes_receipt(self, tmp_path, cao_system):
        source = _write_source(tmp_path)
        plan = _plan_for(source)
        manifest = tmp_path / "m.json"
        manifest.write_text(
            json.dumps(_manifest_from_plan(plan, [_candidate(plan)])),
            encoding="utf-8",
        )
        receipt_out = tmp_path / "receipt.json"
        receipt = fii.apply_manifest(
            str(manifest),
            project_id="cao-system",
            receipt_out=str(receipt_out),
        )
        assert receipt["candidate_count"] == 1
        assert receipt["mappings"][0]["status"] == "created"
        key = receipt["mappings"][0]["key"]
        assert key.startswith("cond-")
        assert receipt_out.is_file()
        # The feature is actually in the tracker.
        assert tracker.get_issue(key)["title"] == "a feature"

    def test_idempotent_replay_returns_existing_key(self, tmp_path, cao_system):
        source = _write_source(tmp_path)
        plan = _plan_for(source)
        manifest = _manifest_from_plan(plan, [_candidate(plan)])
        mpath = tmp_path / "m.json"
        mpath.write_text(json.dumps(manifest), encoding="utf-8")
        first = fii.apply_manifest(str(mpath), project_id="cao-system")
        second = fii.apply_manifest(str(mpath), project_id="cao-system")
        assert first["mappings"][0]["key"] == second["mappings"][0]["key"]
        assert first["mappings"][0]["status"] == "created"
        assert second["mappings"][0]["status"] == "existing"
        # No second key allocated.
        assert first["after_next_issue_number"] == second["after_next_issue_number"]

    def test_conflicting_bytes_on_replay_refuses(self, tmp_path, cao_system):
        source = _write_source(tmp_path)
        plan = _plan_for(source)
        cands = [_candidate(plan)]
        manifest = _manifest_from_plan(plan, cands)
        mpath = tmp_path / "m.json"
        mpath.write_text(json.dumps(manifest), encoding="utf-8")
        fii.apply_manifest(str(mpath), project_id="cao-system")
        # Replay with a different title -> different row digest -> conflict.
        cands[0]["title"] = "a totally different feature"
        mpath.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(TrackerError) as exc:
            fii.apply_manifest(str(mpath), project_id="cao-system")
        assert exc.value.code == "conflict"

    def test_map_existing_attaches_provenance_label(self, tmp_path, cao_system):
        # First create a feature to map onto.
        existing = tracker.create_feature(project_id="cao-system", title="preexisting")
        source = _write_source(tmp_path)
        plan = _plan_for(source)
        cand = _candidate(plan, action="map-existing", canonical_key=existing["key"])
        manifest = _manifest_from_plan(plan, [cand])
        mpath = tmp_path / "m.json"
        mpath.write_text(json.dumps(manifest), encoding="utf-8")
        receipt = fii.apply_manifest(str(mpath), project_id="cao-system")
        assert receipt["mappings"][0]["status"] == "mapped"
        assert receipt["mappings"][0]["key"] == existing["key"]
        # A second apply is idempotent (label already present).
        receipt2 = fii.apply_manifest(str(mpath), project_id="cao-system")
        assert receipt2["mappings"][0]["status"] == "existing"

    def test_map_existing_missing_canonical_refuses(self, tmp_path, cao_system):
        source = _write_source(tmp_path)
        plan = _plan_for(source)
        cand = _candidate(plan, action="map-existing")  # no canonical_key
        manifest = _manifest_from_plan(plan, [cand])
        mpath = tmp_path / "m.json"
        mpath.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(TrackerError) as exc:
            fii.apply_manifest(str(mpath), project_id="cao-system")
        assert exc.value.code == "invalid"

    def test_map_existing_nonexistent_canonical_refuses(self, tmp_path, cao_system):
        source = _write_source(tmp_path)
        plan = _plan_for(source)
        cand = _candidate(plan, action="map-existing", canonical_key="cond-9999")
        manifest = _manifest_from_plan(plan, [cand])
        mpath = tmp_path / "m.json"
        mpath.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(TrackerError) as exc:
            fii.apply_manifest(str(mpath), project_id="cao-system")
        assert exc.value.code == "not-found"

    def test_relate_existing_creates_links(self, tmp_path, cao_system):
        target = tracker.create_feature(project_id="cao-system", title="target")
        source = _write_source(tmp_path)
        plan = _plan_for(source)
        cand = _candidate(plan, action="relate-existing", related_keys=[target["key"]])
        manifest = _manifest_from_plan(plan, [cand])
        mpath = tmp_path / "m.json"
        mpath.write_text(json.dumps(manifest), encoding="utf-8")
        receipt = fii.apply_manifest(str(mpath), project_id="cao-system")
        assert receipt["mappings"][0]["status"] == "created"
        new_key = receipt["mappings"][0]["key"]
        links = tracker.get_issue(new_key).get("links", [])
        assert any(l.get("to_key") == target["key"] for l in links)

    def test_relate_existing_replay_link_mismatch_refuses(self, tmp_path, cao_system):
        t1 = tracker.create_feature(project_id="cao-system", title="t1")
        t2 = tracker.create_feature(project_id="cao-system", title="t2")
        source = _write_source(tmp_path)
        plan = _plan_for(source)
        manifest = _manifest_from_plan(
            plan, [_candidate(plan, action="relate-existing", related_keys=[t1["key"]])]
        )
        mpath = tmp_path / "m.json"
        mpath.write_text(json.dumps(manifest), encoding="utf-8")
        fii.apply_manifest(str(mpath), project_id="cao-system")
        # Replay relating a different key set -> conflict.
        manifest["candidates"][0]["related_keys"] = [t2["key"]]
        mpath.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(TrackerError) as exc:
            fii.apply_manifest(str(mpath), project_id="cao-system")
        assert exc.value.code == "conflict"

    def test_relate_existing_missing_related_key_refuses(self, tmp_path, cao_system):
        source = _write_source(tmp_path)
        plan = _plan_for(source)
        cand = _candidate(plan, action="relate-existing", related_keys=["cond-7777"])
        manifest = _manifest_from_plan(plan, [cand])
        mpath = tmp_path / "m.json"
        mpath.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(TrackerError) as exc:
            fii.apply_manifest(str(mpath), project_id="cao-system")
        assert exc.value.code == "not-found"

    def test_skip_invalid_records_skipped_and_no_key(self, tmp_path, cao_system):
        source = _write_source(tmp_path)
        plan = _plan_for(source)
        cand = _candidate(plan, action="skip-invalid", skip_reason="not applicable")
        manifest = _manifest_from_plan(plan, [cand])
        mpath = tmp_path / "m.json"
        mpath.write_text(json.dumps(manifest), encoding="utf-8")
        receipt = fii.apply_manifest(str(mpath), project_id="cao-system")
        assert receipt["mappings"][0]["status"] == "skipped"
        assert receipt["mappings"][0]["key"] is None

    def test_create_terminal_feature(self, tmp_path, cao_system):
        source = _write_source(tmp_path)
        plan = _plan_for(source)
        cand = _candidate(
            plan,
            action="create-terminal-feature",
            status="closed",
            resolution="done in place",
        )
        manifest = _manifest_from_plan(plan, [cand])
        mpath = tmp_path / "m.json"
        mpath.write_text(json.dumps(manifest), encoding="utf-8")
        receipt = fii.apply_manifest(str(mpath), project_id="cao-system")
        assert receipt["mappings"][0]["status"] == "created"
        assert tracker.get_issue(receipt["mappings"][0]["key"])["status"] == "closed"

    def test_unknown_action_refuses(self, tmp_path, cao_system):
        source = _write_source(tmp_path)
        plan = _plan_for(source)
        cand = _candidate(plan)
        cand["action"] = "teleport"  # passes validate? no — validate refuses first
        manifest = _manifest_from_plan(plan, [cand])
        mpath = tmp_path / "m.json"
        mpath.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(TrackerError):
            fii.apply_manifest(str(mpath), project_id="cao-system")

    def test_manifest_project_mismatch_refuses(self, tmp_path, cao_system):
        source = _write_source(tmp_path)
        plan = _plan_for(source)
        manifest = _manifest_from_plan(plan, [_candidate(plan)])
        # manifest.get("project") takes precedence over target_project.
        manifest["project"] = "other-project"
        manifest["target_project"] = "other-project"
        mpath = tmp_path / "m.json"
        mpath.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(TrackerError) as exc:
            fii.apply_manifest(str(mpath), project_id="cao-system")
        assert exc.value.code == "invalid"

    def test_manifest_missing_project_refuses(self, tmp_path, cao_system):
        source = _write_source(tmp_path)
        plan = _plan_for(source)
        manifest = _manifest_from_plan(plan, [_candidate(plan)])
        del manifest["target_project"]
        del manifest["project"]
        mpath = tmp_path / "m.json"
        mpath.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(TrackerError) as exc:
            fii.apply_manifest(str(mpath), project_id="cao-system")
        assert exc.value.code == "invalid"

    def test_nonexistent_project_refuses(self, tmp_path, cao_system):
        source = _write_source(tmp_path)
        plan = _plan_for(source)
        # Manifest must claim the nonexistent project to pass the project-match
        # check and reach the DB lookup that raises not-found.
        cand = _candidate(plan)
        cand["target_project"] = "no-such-project"
        manifest = _manifest_from_plan(plan, [cand])
        manifest["project"] = "no-such-project"
        manifest["target_project"] = "no-such-project"
        mpath = tmp_path / "m.json"
        mpath.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(TrackerError) as exc:
            fii.apply_manifest(str(mpath), project_id="no-such-project")
        assert exc.value.code == "not-found"

    def test_invalid_priority_refuses(self, tmp_path, cao_system):
        source = _write_source(tmp_path)
        plan = _plan_for(source)
        manifest = _manifest_from_plan(plan, [_candidate(plan, priority="P9")])
        mpath = tmp_path / "m.json"
        mpath.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(TrackerError) as exc:
            fii.apply_manifest(str(mpath), project_id="cao-system")
        assert exc.value.code == "invalid"

    def test_invalid_status_refuses(self, tmp_path, cao_system):
        source = _write_source(tmp_path)
        plan = _plan_for(source)
        manifest = _manifest_from_plan(plan, [_candidate(plan, status="frozen")])
        mpath = tmp_path / "m.json"
        mpath.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(TrackerError) as exc:
            fii.apply_manifest(str(mpath), project_id="cao-system")
        assert exc.value.code == "invalid"

    def test_candidate_missing_migration_id_refuses(self, tmp_path, cao_system):
        source = _write_source(tmp_path)
        plan = _plan_for(source)
        cand = _candidate(plan)
        # migration_id falls back to migrationId then ordinal; drop all three
        # so the guard's empty-migration_id refusal is reached.
        for k in ("migration_id", "migrationId", "ordinal"):
            cand.pop(k, None)
        manifest = _manifest_from_plan(plan, [cand])
        mpath = tmp_path / "m.json"
        mpath.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(TrackerError) as exc:
            fii.apply_manifest(str(mpath), project_id="cao-system")
        assert exc.value.code == "invalid"


class TestEnsureProjectExists:
    def test_existing_project_passes(self, cao_system):
        fii._ensure_project_exists("cao-system")  # no raise

    def test_missing_project_refuses(self):
        with pytest.raises(TrackerError) as exc:
            fii._ensure_project_exists("no-such-project")
        assert exc.value.code == "not-found"

    def test_unknown_tracker_error_is_reraised(self, monkeypatch):
        def boom(project_id):
            raise TrackerError("conflict", "unavailable")

        monkeypatch.setattr(fii.tracker, "get_project", boom)
        with pytest.raises(TrackerError) as exc:
            fii._ensure_project_exists("cao-system")
        assert exc.value.code == "conflict"


class TestHelpersAndEdgeCases:
    def test_provenance_label_short_passthrough(self):
        assert fii._provenance_label("abc") == "migration:abc"

    def test_provenance_label_truncates_long_migration_id(self):
        # A 70-char migration_id exceeds the 64-char label cap and is truncated
        # with a short hash suffix.
        label = fii._provenance_label("x" * 70)
        assert label.startswith("migration:")
        assert len(label) <= fii.MAX_MIGRATION_LABEL_LEN
        assert label != "migration:" + ("x" * 70)

    def test_migration_id_matches_lowercase_inventory_variant(self):
        # The inventory key is capitalised ("Memory-..."); a lowercase title
        # still resolves via the normalised lowercase fallback.
        title = (
            "memory-candidate adjudication pipeline — promoted to the " "pre-chess lifecycle track"
        )
        assert (
            fii._migration_id("0" * 64, 5, title)
            == "memory-candidate-adjudication-pipeline-promoted-to-the"
        )

    def test_parse_source_unreadable_file_refuses(self, tmp_path, monkeypatch):
        source = tmp_path / "FUTURE.md"
        source.write_text("ok", encoding="utf-8")

        def boom(self):
            raise OSError("io error")

        monkeypatch.setattr(fii.Path, "read_bytes", boom)
        with pytest.raises(TrackerError) as exc:
            fii.parse_source_file(str(source))
        assert exc.value.code == "invalid"

    def test_load_manifest_unreadable_refuses(self, tmp_path, monkeypatch):
        manifest = tmp_path / "m.json"
        manifest.write_text("{}", encoding="utf-8")

        def boom(self):
            raise OSError("io error")

        monkeypatch.setattr(fii.Path, "read_bytes", boom)
        with pytest.raises(TrackerError) as exc:
            fii._load_manifest(str(manifest))
        assert exc.value.code == "invalid"
