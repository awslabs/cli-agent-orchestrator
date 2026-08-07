"""Importing a markdown issue ledger.

The parser is tested against the exact shapes the cao-conductor self-heal
ledger actually contains — free-text status prose, one-off field names, bodies
that themselves contain `- **bold:**` lines — because those are what a
migration silently mangles, and a mangled 208-entry history is not something
anybody reviews line by line afterwards.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services import issue_ledger_import as importer
from cli_agent_orchestrator.services import issue_tracker as tracker


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/ledger.db")
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(tracker, "SessionLocal", sessionmaker(bind=engine))
    tracker.create_project(name="CAO System", project_id="cao-system", issue_prefix="cond")
    yield
    engine.dispose()


LEDGER = """# Open issues — cao-conductor self-heal ledger

Preamble prose that is not an entry.

---

## cond-0025 — Recurring non-fatal worker-conduct SKILL.md unreadable warning

- **filed:** 2026-07-21T11:44:29Z
- **reporter:** human
- **status:** open
- **failing command:** `conduct spawn (any worker)`
- **evidence:** (none given)

Terra flagged in the campaign close-out journal: every worker spawn logs a
non-fatal warning.

---

## cond-0039 — [P2] event-mirror lock contention logs full traceback every tick

- **filed:** 2026-07-21T17:01:14Z
- **reporter:** 13e6fe47
- **status:** deferred procedural hardening; not a pre-chess blocker
- **failing command:** `python3 -B independent_core_probes.py`
- **evidence:** /Users/colin/runs/self-heal/report.md
- **affected heads:** 70b4a9a
- **preserved run:** runs/self-heal-pr-a-validator-sol

Independent Sol/max validation confirmed the event mirror remains bounded.

- **this line is prose, not metadata:** it lives inside the body.

---
"""

CLOSED_LEDGER = """# Closed issues — cao-conductor self-heal ledger

---

## cond-0001 — doctor plugin-loaded check satisfiable by stale logs

- **filed:** 2026-07-20T23:50:48Z
- **reporter:** human
- **status:** fixed (v0.1.2 — critical gate = venv entry point)
- **failing command:** `conduct doctor`
- **evidence:** (none given)

conduct doctor's plugin-loaded critical check greps too broadly.

---
"""


class TestParsing:
    def test_preamble_prose_is_not_an_entry(self):
        assert [e["key"] for e in importer.parse_ledger(LEDGER)] == ["cond-0025", "cond-0039"]

    def test_a_severity_prefix_is_lifted_out_of_the_title(self):
        entry = importer.parse_ledger(LEDGER)[1]
        assert entry["severity"] == "P2"
        assert entry["title"].startswith("event-mirror lock contention")

    def test_a_severity_before_the_dash_is_read_too(self):
        # Exactly one of the 208 real entries writes `## cond-0200 [P2] — ...`.
        # A parser validated only against its own fixtures drops that entry
        # silently, and 207 of 208 looks like success.
        text = "## cond-0200 [P2] — Closed duplicate visual-QA run rejects collect\n\n- **status:** open\n\nbody\n"
        entry = importer.parse_ledger(text)[0]
        assert (entry["key"], entry["severity"]) == ("cond-0200", "P2")
        assert entry["title"].startswith("Closed duplicate visual-QA")

    def test_an_entry_without_a_severity_prefix_keeps_its_whole_title(self):
        entry = importer.parse_ledger(LEDGER)[0]
        assert entry["severity"] is None
        assert entry["title"].startswith("Recurring non-fatal worker-conduct")

    def test_unrecognised_fields_are_captured_rather_than_dropped(self):
        entry = importer.parse_ledger(LEDGER)[1]
        assert dict(entry["extra"]) == {
            "affected heads": "70b4a9a",
            "preserved run": "runs/self-heal-pr-a-validator-sol",
        }

    def test_a_bold_line_inside_the_body_stays_in_the_body(self):
        # The metadata block ends at the first prose line. Hoisting a
        # mid-paragraph `- **x:**` into metadata would delete the sentence it
        # belonged to.
        entry = importer.parse_ledger(LEDGER)[1]
        body = "\n".join(entry["body_lines"])
        assert "this line is prose, not metadata" in body
        assert "this line is prose, not metadata" not in dict(entry["extra"])


class TestStatusMapping:
    def test_a_bare_open_stays_open_with_no_resolution(self):
        entry = importer.parse_ledger(LEDGER)[0]
        built = importer.build_issue(entry, project_id="cao-system", default_status="open")
        assert built["status"] == "open"
        assert built["_resolution"] is None

    def test_deferred_stays_open_and_is_labelled(self):
        # Deferring is a scheduling decision. Mapping it to `wontfix` would
        # retire live defects by transcription error.
        entry = importer.parse_ledger(LEDGER)[1]
        built = importer.build_issue(entry, project_id="cao-system", default_status="open")
        assert built["status"] == "open"
        assert built["labels"] == ["deferred"]
        assert "not a pre-chess blocker" in built["_resolution"]

    def test_free_text_resolution_prose_is_preserved_verbatim(self):
        entry = importer.parse_ledger(CLOSED_LEDGER)[0]
        built = importer.build_issue(entry, project_id="cao-system", default_status="closed")
        assert built["status"] == "closed"
        assert built["_resolution"] == "fixed (v0.1.2 — critical gate = venv entry point)"

    def test_qualified_open_prose_is_still_open(self):
        text = LEDGER.replace(
            "- **status:** open\n",
            "- **status:** open; intentionally deferred from the pre-chess gate\n",
        )
        built = importer.build_issue(
            importer.parse_ledger(text)[0], project_id="cao-system", default_status="open"
        )
        assert built["status"] == "open"
        assert "pre-chess gate" in built["_resolution"]


class TestFieldMapping:
    def test_none_given_evidence_becomes_null_not_the_literal_string(self):
        built = importer.build_issue(
            importer.parse_ledger(LEDGER)[0], project_id="cao-system", default_status="open"
        )
        assert built["evidence"] is None

    def test_a_backticked_failing_command_loses_its_backticks(self):
        built = importer.build_issue(
            importer.parse_ledger(LEDGER)[0], project_id="cao-system", default_status="open"
        )
        assert built["failing_command"] == "conduct spawn (any worker)"

    def test_the_filed_stamp_is_read_as_utc(self):
        built = importer.build_issue(
            importer.parse_ledger(LEDGER)[0], project_id="cao-system", default_status="open"
        )
        assert built["created_at"].isoformat() == "2026-07-21T11:44:29+00:00"

    def test_a_date_with_a_trailing_author_note_keeps_both(self):
        # Seven real entries write `2026-07-21 (external adjudicator)`, and
        # those are exactly the entries with no `reporter:` field — so the
        # note is the only record of who filed them. Parsing the whole string
        # as a timestamp fails and would restamp the entry as filed today.
        text = "## cond-0010 — Runbook sandbox seeding omits remote creation\n\n- **filed:** 2026-07-21 (external adjudicator)\n- **status:** fixed\n\nbody\n"
        built = importer.build_issue(
            importer.parse_ledger(text)[0], project_id="cao-system", default_status="closed"
        )
        assert built["created_at"].isoformat() == "2026-07-21T00:00:00+00:00"
        assert "**filed note:** (external adjudicator)" in built["body"]

    def test_a_minute_precision_stamp_parses(self):
        text = "## cond-0111 — x\n\n- **filed:** 2026-07-25T03:55Z\n\nbody\n"
        built = importer.build_issue(
            importer.parse_ledger(text)[0], project_id="cao-system", default_status="open"
        )
        assert built["created_at"].isoformat() == "2026-07-25T03:55:00+00:00"

    def test_a_non_utc_offset_is_converted_not_dropped(self):
        text = "## cond-0112 — x\n\n- **filed:** 2026-07-25T20:09:22-04:00\n\nbody\n"
        built = importer.build_issue(
            importer.parse_ledger(text)[0], project_id="cao-system", default_status="open"
        )
        assert built["created_at"].isoformat() == "2026-07-25T20:09:22-04:00"

    def test_an_unreadable_stamp_says_so_rather_than_filing_it_today(self):
        text = "## cond-0113 — x\n\n- **filed:** sometime last week\n\nbody\n"
        built = importer.build_issue(
            importer.parse_ledger(text)[0], project_id="cao-system", default_status="open"
        )
        assert built["created_at"] is None
        assert "**filed (unparsed):** sometime last week" in built["body"]

    def test_p0_survives_as_p0(self):
        # Two real entries are P0 ("can kill the production tmux server").
        # Folding them into P1 erases the one distinction their author made.
        text = "## cond-0100 — [P0] Lane D live acceptance can kill the tmux server\n\n- **status:** closed\n\nbody\n"
        built = importer.build_issue(
            importer.parse_ledger(text)[0], project_id="cao-system", default_status="closed"
        )
        assert built["severity"] == "P0"

    def test_a_space_separated_severity_prefix_is_read(self):
        text = (
            "## cond-0104 — P1 native v2 admission boot-gate 409\n\n- **status:** closed\n\nbody\n"
        )
        built = importer.build_issue(
            importer.parse_ledger(text)[0], project_id="cao-system", default_status="closed"
        )
        assert (built["severity"], built["title"]) == ("P1", "native v2 admission boot-gate 409")

    def test_an_entry_with_no_filed_line_says_the_date_is_the_migrations(self):
        text = "## cond-0114 — x\n\n- **status:** closed\n\nbody\n"
        built = importer.build_issue(
            importer.parse_ledger(text)[0], project_id="cao-system", default_status="closed"
        )
        assert built["created_at"] is None
        assert "**filed:** absent in the markdown ledger" in built["body"]

    def test_an_unbracketed_severity_prefix_is_read(self):
        # 41 of 208 real entries write `P1: title` rather than `[P1] title`.
        text = "## cond-0114 — P1: provider-authored Claude readiness drift\n\n- **status:** closed\n\nbody\n"
        built = importer.build_issue(
            importer.parse_ledger(text)[0], project_id="cao-system", default_status="closed"
        )
        assert built["severity"] == "P1"
        assert built["title"] == "provider-authored Claude readiness drift"


class TestImport:
    def _write(self, tmp_path, text, name="OPEN_ISSUES.md"):
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_ids_and_filing_dates_survive_the_import(self, tmp_path):
        importer.import_ledger(self._write(tmp_path, LEDGER), project_id="cao-system")
        issue = tracker.get_issue("cond-0025")
        assert issue["created_at"] == "2026-07-21T11:44:29Z"
        assert issue["reporter"] == "human"

    def test_the_counter_advances_past_the_highest_imported_id(self, tmp_path):
        importer.import_ledger(self._write(tmp_path, LEDGER), project_id="cao-system")
        fresh = tracker.create_issue(project_id="cao-system", title="filed after the import")
        assert fresh["key"] == "cond-0040"

    def test_re_running_the_import_skips_rather_than_duplicates(self, tmp_path):
        path = self._write(tmp_path, LEDGER)
        importer.import_ledger(path, project_id="cao-system")
        second = importer.import_ledger(path, project_id="cao-system")
        assert (second["imported"], second["skipped"]) == (0, 2)
        assert tracker.list_issues(project_id="cao-system")["total"] == 2

    def test_a_dry_run_writes_nothing(self, tmp_path):
        report = importer.import_ledger(
            self._write(tmp_path, LEDGER), project_id="cao-system", dry_run=True
        )
        assert report["parsed"] == 2
        assert tracker.list_issues(project_id="cao-system")["total"] == 0

    def test_both_ledgers_import_into_one_project(self, tmp_path):
        importer.import_ledger(self._write(tmp_path, LEDGER), project_id="cao-system")
        importer.import_ledger(
            self._write(tmp_path, CLOSED_LEDGER, "CLOSED_ISSUES.md"),
            project_id="cao-system",
            default_status="closed",
        )
        assert tracker.list_issues(project_id="cao-system")["total"] == 3
        assert tracker.list_issues(project_id="cao-system", open_only=True)["total"] == 2

    def test_a_preserved_field_is_readable_in_the_body(self, tmp_path):
        importer.import_ledger(self._write(tmp_path, LEDGER), project_id="cao-system")
        body = tracker.get_issue("cond-0039")["body"]
        assert "**affected heads:** 70b4a9a" in body
        assert "preserved from the markdown ledger" in body

    def test_a_closed_import_stamps_closed_at(self, tmp_path):
        importer.import_ledger(
            self._write(tmp_path, CLOSED_LEDGER, "CLOSED_ISSUES.md"),
            project_id="cao-system",
            default_status="closed",
        )
        assert tracker.get_issue("cond-0001")["closed_at"] is not None
