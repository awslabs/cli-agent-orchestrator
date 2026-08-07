"""Project-scoped issue tracking.

The properties under test are the ones that make a shared issue log
trustworthy rather than merely present: an identifier resolves to exactly one
project, a key is never reused, every mutation leaves a trace, and a filing
that cannot be placed fails loudly instead of landing somewhere plausible.
"""

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services import issue_tracker as tracker
from cli_agent_orchestrator.services.issue_tracker import TrackerError


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path}/tracker.db")
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(tracker, "SessionLocal", sessionmaker(bind=engine))
    yield
    engine.dispose()


@pytest.fixture
def cao_system(tmp_path):
    conductor = tmp_path / "cao-conductor"
    fork = tmp_path / "cli-agent-orchestrator"
    conductor.mkdir()
    fork.mkdir()
    project = tracker.create_project(
        name="CAO System",
        project_id="cao-system",
        issue_prefix="cond",
        scopes=[
            {"kind": "path", "value": str(conductor)},
            {"kind": "path", "value": str(fork)},
            {"kind": "session", "value": "cao-p1-closure"},
        ],
    )
    return {"project": project, "conductor": conductor, "fork": fork}


class TestProjectCreation:
    def test_a_project_spans_many_paths_and_sessions(self, cao_system):
        detail = tracker.get_project("cao-system")
        kinds = sorted((s["kind"], s["value"]) for s in detail["scopes"])
        assert [k for k, _ in kinds] == ["path", "path", "session"]

    def test_the_slug_is_derived_from_the_name_when_not_given(self):
        created = tracker.create_project(name="Quest Scheduler")
        assert created["id"] == "quest-scheduler"

    def test_a_name_with_no_usable_characters_is_refused_not_invented(self):
        # Generating `project-1` here would be a naming decision the caller
        # never made, and it is the kind of id nobody can find later.
        with pytest.raises(TrackerError) as exc:
            tracker.create_project(name="???")
        assert exc.value.code == "invalid"

    def test_a_duplicate_project_id_is_refused(self, cao_system):
        with pytest.raises(TrackerError) as exc:
            tracker.create_project(name="Something else", project_id="cao-system")
        assert exc.value.code == "conflict"

    def test_a_rejected_scope_leaves_no_half_built_project(self, tmp_path):
        tracker.create_project(name="First", scopes=[{"kind": "path", "value": str(tmp_path)}])
        with pytest.raises(TrackerError):
            tracker.create_project(name="Second", scopes=[{"kind": "path", "value": str(tmp_path)}])
        assert [p["id"] for p in tracker.list_projects()] == ["first"]


class TestScopeUniqueness:
    def test_one_path_cannot_belong_to_two_projects(self, cao_system):
        tracker.create_project(name="Other", project_id="other")
        with pytest.raises(TrackerError) as exc:
            tracker.add_scope("other", kind="path", value=str(cao_system["conductor"]))
        assert exc.value.code == "conflict"
        assert "cao-system" in exc.value.message

    def test_re_registering_the_same_scope_on_the_same_project_is_idempotent(self, cao_system):
        again = tracker.add_scope("cao-system", kind="path", value=str(cao_system["fork"]))
        assert again["created"] is False

    def test_scope_values_are_unique_across_kinds(self, cao_system):
        # A session literally named like a path is pathological, but the
        # uniqueness rule is what stops resolution order from deciding.
        tracker.create_project(name="Other", project_id="other")
        with pytest.raises(TrackerError):
            tracker.add_scope("other", kind="session", value=str(cao_system["conductor"]))


class TestScopeNormalisation:
    def test_a_trailing_separator_is_the_same_scope(self, tmp_path):
        tracker.create_project(name="P", project_id="p")
        tracker.add_scope("p", kind="path", value=str(tmp_path))
        again = tracker.add_scope("p", kind="path", value=str(tmp_path) + "/")
        assert again["created"] is False

    def test_ssh_and_https_remotes_are_one_scope(self):
        tracker.create_project(name="P", project_id="p")
        tracker.add_scope("p", kind="git_remote", value="git@github.com:colindmurray/cao.git")
        again = tracker.add_scope(
            "p", kind="git_remote", value="https://github.com/colindmurray/cao"
        )
        assert again["created"] is False

    def test_remote_credentials_are_never_stored(self):
        tracker.create_project(name="P", project_id="p")
        row = tracker.add_scope(
            "p", kind="git_remote", value="https://user:ghp_secret@github.com/o/r.git"
        )
        assert "ghp_secret" not in row["value"]
        assert row["value"] == "github.com/o/r"

    def test_a_relative_path_scope_is_refused(self):
        tracker.create_project(name="P", project_id="p")
        with pytest.raises(TrackerError):
            tracker.add_scope("p", kind="path", value="relative/dir")

    def test_an_unknown_scope_kind_is_refused(self):
        tracker.create_project(name="P", project_id="p")
        with pytest.raises(TrackerError):
            tracker.add_scope("p", kind="hostname", value="foundry")


class TestResolution:
    def test_an_explicit_project_wins(self, cao_system):
        got = tracker.resolve_project(project="cao-system", session="unknown-session")
        assert (got.project_id, got.matched_by) == ("cao-system", "explicit")

    def test_a_session_resolves_across_directories(self, cao_system, tmp_path):
        got = tracker.resolve_project(session="cao-p1-closure", cwd=str(tmp_path / "elsewhere"))
        assert (got.project_id, got.matched_by) == ("cao-system", "session")

    def test_a_subdirectory_resolves_to_its_project(self, cao_system):
        nested = cao_system["conductor"] / "conduct" / "lib"
        nested.mkdir(parents=True)
        got = tracker.resolve_project(cwd=str(nested))
        assert (got.project_id, got.matched_by) == ("cao-system", "path")

    def test_a_sibling_directory_sharing_a_name_prefix_does_not_match(self, cao_system, tmp_path):
        # `cao-conductor-worktrees` is a REAL sibling of `cao-conductor` on
        # this machine. String-prefix matching would file every worktree
        # issue into the wrong project and nobody would notice for weeks.
        sibling = tmp_path / "cao-conductor-worktrees"
        sibling.mkdir()
        got = tracker.resolve_project(cwd=str(sibling))
        assert got.project_id is None

    def test_the_most_specific_path_scope_wins(self, cao_system):
        gateway = cao_system["conductor"] / "gateway"
        gateway.mkdir()
        tracker.create_project(name="Gateway", project_id="gateway")
        # Same value is refused, so the specific project registers the deeper dir.
        tracker.add_scope("gateway", kind="path", value=str(gateway))
        got = tracker.resolve_project(cwd=str(gateway / "src"))
        assert got.project_id == "gateway"

    def test_session_beats_path(self, cao_system, tmp_path):
        other = tmp_path / "other-repo"
        other.mkdir()
        tracker.create_project(
            name="Other", project_id="other", scopes=[{"kind": "path", "value": str(other)}]
        )
        got = tracker.resolve_project(session="cao-p1-closure", cwd=str(other))
        assert got.project_id == "cao-system"

    def test_a_git_remote_resolves_when_no_path_matches(self, tmp_path):
        tracker.create_project(
            name="Aegix",
            project_id="aegix",
            scopes=[{"kind": "git_remote", "value": "git@github.com:g/aegix.git"}],
        )
        got = tracker.resolve_project(git_remote="https://github.com/g/aegix")
        assert (got.project_id, got.matched_by) == ("aegix", "git_remote")

    def test_an_unregistered_site_resolves_to_nothing_rather_than_raising(self, tmp_path):
        got = tracker.resolve_project(cwd=str(tmp_path / "nowhere"))
        assert got.as_dict() == {"project_id": None, "matched_by": None, "matched_value": None}

    def test_an_explicit_unknown_project_raises(self):
        with pytest.raises(TrackerError) as exc:
            tracker.resolve_project(project="no-such-project")
        assert exc.value.code == "not-found"


class TestIssueKeys:
    def test_keys_use_the_project_prefix_and_increment(self, cao_system):
        a = tracker.create_issue(project_id="cao-system", title="first")
        b = tracker.create_issue(project_id="cao-system", title="second")
        assert (a["key"], b["key"]) == ("cond-0001", "cond-0002")

    def test_a_deleted_issue_never_returns_its_key(self, cao_system):
        first = tracker.create_issue(project_id="cao-system", title="first")
        tracker.delete_issue(first["key"])
        second = tracker.create_issue(project_id="cao-system", title="second")
        # Recycling cond-0001 would silently repoint every commit message,
        # report and evidence path that already quotes it.
        assert second["key"] == "cond-0002"

    def test_an_explicit_key_is_preserved_and_advances_the_counter(self, cao_system):
        tracker.create_issue(project_id="cao-system", title="migrated", key="cond-0242")
        nxt = tracker.create_issue(project_id="cao-system", title="new")
        assert nxt["key"] == "cond-0243"

    def test_an_explicit_key_below_the_counter_does_not_rewind_it(self, cao_system):
        tracker.create_issue(project_id="cao-system", title="migrated", key="cond-0100")
        tracker.create_issue(project_id="cao-system", title="older", key="cond-0005")
        nxt = tracker.create_issue(project_id="cao-system", title="new")
        assert nxt["key"] == "cond-0101"

    def test_a_duplicate_explicit_key_is_refused(self, cao_system):
        tracker.create_issue(project_id="cao-system", title="a", key="cond-0007")
        with pytest.raises(TrackerError) as exc:
            tracker.create_issue(project_id="cao-system", title="b", key="cond-0007")
        assert exc.value.code == "conflict"

    def test_two_projects_keep_separate_numbering(self, cao_system):
        tracker.create_project(name="Quest Scheduler", project_id="qs", issue_prefix="qs")
        a = tracker.create_issue(project_id="cao-system", title="a")
        b = tracker.create_issue(project_id="qs", title="b")
        assert (a["key"], b["key"]) == ("cond-0001", "qs-0001")

    def test_renaming_the_prefix_leaves_existing_keys_alone(self, cao_system):
        old = tracker.create_issue(project_id="cao-system", title="a")
        tracker.update_project("cao-system", issue_prefix="cao")
        new = tracker.create_issue(project_id="cao-system", title="b")
        assert tracker.get_issue(old["key"])["key"] == "cond-0001"
        assert new["key"] == "cao-0002"


class TestFiling:
    def test_an_issue_filed_from_a_scoped_directory_lands_in_that_project(self, cao_system):
        issue = tracker.create_issue(
            title="conduct spawn warns about SKILL.md",
            cwd=str(cao_system["fork"] / "src"),
        )
        assert issue["project_id"] == "cao-system"
        assert issue["resolved_by"] == "path"

    def test_an_unresolvable_filing_site_is_refused(self, tmp_path):
        with pytest.raises(TrackerError) as exc:
            tracker.create_issue(title="orphan", cwd=str(tmp_path / "unknown"))
        assert exc.value.code == "unresolved"

    def test_an_empty_title_is_refused(self, cao_system):
        with pytest.raises(TrackerError):
            tracker.create_issue(project_id="cao-system", title="   ")

    def test_an_unknown_status_is_refused(self, cao_system):
        with pytest.raises(TrackerError):
            tracker.create_issue(project_id="cao-system", title="a", status="pending")

    def test_filing_records_the_creation_event(self, cao_system):
        issue = tracker.create_issue(project_id="cao-system", title="a", reporter="13e6fe47")
        events = tracker.get_issue(issue["key"])["events"]
        assert [(e["kind"], e["actor"]) for e in events] == [("created", "13e6fe47")]

    def test_a_terminal_status_at_filing_time_sets_closed_at(self, cao_system):
        issue = tracker.create_issue(project_id="cao-system", title="a", status="closed")
        assert issue["closed_at"] is not None


class TestEditing:
    def test_every_changed_field_leaves_an_audit_event(self, cao_system):
        issue = tracker.create_issue(project_id="cao-system", title="a")
        tracker.update_issue(
            issue["key"], actor="colin", status="in-progress", severity="P2", assignee="terra"
        )
        events = [e for e in tracker.get_issue(issue["key"])["events"] if e["kind"] == "field"]
        assert sorted(e["field"] for e in events) == ["assignee", "severity", "status"]
        assert all(e["actor"] == "colin" for e in events)

    def test_a_no_op_write_records_nothing(self, cao_system):
        issue = tracker.create_issue(project_id="cao-system", title="a", status="open")
        tracker.update_issue(issue["key"], status="open")
        assert [e["kind"] for e in tracker.get_issue(issue["key"])["events"]] == ["created"]

    def test_closing_stamps_closed_at_and_reopening_clears_it(self, cao_system):
        issue = tracker.create_issue(project_id="cao-system", title="a")
        closed = tracker.update_issue(issue["key"], status="closed")
        assert closed["closed_at"] is not None
        reopened = tracker.update_issue(issue["key"], status="open")
        assert reopened["closed_at"] is None

    def test_resolved_is_not_treated_as_closed(self, cao_system):
        # "a fix landed" and "somebody verified it" are different claims, and
        # collapsing them is how fixes get shipped unverified.
        issue = tracker.create_issue(project_id="cao-system", title="a")
        tracker.update_issue(issue["key"], status="resolved")
        assert tracker.list_issues(project_id="cao-system", open_only=True)["total"] == 1

    def test_an_empty_string_clears_a_free_text_field(self, cao_system):
        issue = tracker.create_issue(project_id="cao-system", title="a", assignee="terra")
        assert tracker.update_issue(issue["key"], assignee="")["assignee"] is None

    def test_a_non_editable_field_is_refused(self, cao_system):
        issue = tracker.create_issue(project_id="cao-system", title="a")
        with pytest.raises(TrackerError) as exc:
            tracker.update_issue(issue["key"], key="cond-9999")
        assert exc.value.code == "invalid"

    def test_project_id_cannot_be_edited_through_the_field_path(self, cao_system):
        issue = tracker.create_issue(project_id="cao-system", title="a")
        with pytest.raises(TrackerError):
            tracker.update_issue(issue["key"], project_id="other")

    def test_an_issue_cannot_duplicate_itself(self, cao_system):
        issue = tracker.create_issue(project_id="cao-system", title="a")
        with pytest.raises(TrackerError):
            tracker.update_issue(issue["key"], duplicate_of=issue["key"])

    def test_duplicate_of_must_name_a_real_issue(self, cao_system):
        issue = tracker.create_issue(project_id="cao-system", title="a")
        with pytest.raises(TrackerError) as exc:
            tracker.update_issue(issue["key"], duplicate_of="cond-9999")
        assert exc.value.code == "not-found"


class TestLabels:
    def test_labels_are_deduplicated_and_order_preserved(self, cao_system):
        issue = tracker.create_issue(
            project_id="cao-system", title="a", labels=["quota", "flaky", "quota"]
        )
        assert issue["labels"] == ["quota", "flaky"]

    def test_a_comma_string_is_accepted(self, cao_system):
        issue = tracker.create_issue(project_id="cao-system", title="a", labels="quota, flaky")
        assert issue["labels"] == ["quota", "flaky"]

    def test_label_filtering_does_not_match_a_longer_label(self, cao_system):
        tracker.create_issue(project_id="cao-system", title="a", labels=["ui"])
        tracker.create_issue(project_id="cao-system", title="b", labels=["ui-polish"])
        got = tracker.list_issues(project_id="cao-system", label="ui")
        assert [i["title"] for i in got["issues"]] == ["a"]

    def test_too_many_labels_is_refused(self, cao_system):
        with pytest.raises(TrackerError):
            tracker.create_issue(
                project_id="cao-system", title="a", labels=[f"l{i}" for i in range(40)]
            )


class TestListingAndSearch:
    def test_total_is_the_unpaged_count(self, cao_system):
        for i in range(5):
            tracker.create_issue(project_id="cao-system", title=f"issue {i}")
        page = tracker.list_issues(project_id="cao-system", limit=2)
        assert page["total"] == 5
        assert len(page["issues"]) == 2

    def test_search_covers_title_body_key_and_failing_command(self, cao_system):
        tracker.create_issue(project_id="cao-system", title="alpha", body="nothing")
        tracker.create_issue(
            project_id="cao-system", title="beta", failing_command="conduct spawn --lane x"
        )
        got = tracker.list_issues(project_id="cao-system", query="conduct spawn")
        assert [i["title"] for i in got["issues"]] == ["beta"]

    def test_open_only_excludes_terminal_statuses(self, cao_system):
        a = tracker.create_issue(project_id="cao-system", title="a")
        tracker.create_issue(project_id="cao-system", title="b")
        tracker.update_issue(a["key"], status="wontfix")
        assert tracker.list_issues(project_id="cao-system", open_only=True)["total"] == 1

    def test_listing_is_scoped_to_one_project(self, cao_system):
        tracker.create_project(name="Other", project_id="other")
        tracker.create_issue(project_id="cao-system", title="mine")
        tracker.create_issue(project_id="other", title="theirs")
        got = tracker.list_issues(project_id="cao-system")
        assert [i["title"] for i in got["issues"]] == ["mine"]

    def test_severity_order_puts_p1_first(self, cao_system):
        tracker.create_issue(project_id="cao-system", title="low", severity="P4")
        tracker.create_issue(project_id="cao-system", title="high", severity="P1")
        got = tracker.list_issues(project_id="cao-system", order="severity")
        assert [i["title"] for i in got["issues"]] == ["high", "low"]

    def test_limit_is_bounded(self, cao_system):
        assert tracker.list_issues(project_id="cao-system", limit=100000)["limit"] == 500


class TestCommentsAndLinks:
    def test_a_comment_is_stored_and_audited(self, cao_system):
        issue = tracker.create_issue(project_id="cao-system", title="a")
        tracker.add_comment(issue["key"], body="reproduced on main", author="colin")
        detail = tracker.get_issue(issue["key"])
        assert [c["body"] for c in detail["comments"]] == ["reproduced on main"]
        assert any(e["kind"] == "comment" for e in detail["events"])

    def test_an_empty_comment_is_refused(self, cao_system):
        issue = tracker.create_issue(project_id="cao-system", title="a")
        with pytest.raises(TrackerError):
            tracker.add_comment(issue["key"], body="  ")

    def test_links_appear_on_both_issues(self, cao_system):
        a = tracker.create_issue(project_id="cao-system", title="a")
        b = tracker.create_issue(project_id="cao-system", title="b")
        tracker.add_link(a["key"], to_key=b["key"], kind="blocks")
        assert len(tracker.get_issue(a["key"])["links"]) == 1
        assert len(tracker.get_issue(b["key"])["links"]) == 1

    def test_a_duplicate_link_is_idempotent(self, cao_system):
        a = tracker.create_issue(project_id="cao-system", title="a")
        b = tracker.create_issue(project_id="cao-system", title="b")
        tracker.add_link(a["key"], to_key=b["key"], kind="blocks")
        again = tracker.add_link(a["key"], to_key=b["key"], kind="blocks")
        assert again["created"] is False

    def test_a_link_to_a_missing_issue_is_refused(self, cao_system):
        a = tracker.create_issue(project_id="cao-system", title="a")
        with pytest.raises(TrackerError):
            tracker.add_link(a["key"], to_key="cond-9999", kind="relates")

    def test_deleting_an_issue_removes_its_comments_events_and_links(self, cao_system):
        a = tracker.create_issue(project_id="cao-system", title="a")
        b = tracker.create_issue(project_id="cao-system", title="b")
        tracker.add_comment(a["key"], body="note")
        tracker.add_link(a["key"], to_key=b["key"], kind="relates")
        tracker.delete_issue(a["key"])
        assert tracker.get_issue(b["key"])["links"] == []


class TestProjectDeletion:
    def test_a_project_holding_issues_is_not_deleted_by_accident(self, cao_system):
        tracker.create_issue(project_id="cao-system", title="a")
        with pytest.raises(TrackerError) as exc:
            tracker.delete_project("cao-system")
        assert exc.value.code == "conflict"

    def test_force_deletes_the_issues_too(self, cao_system):
        tracker.create_issue(project_id="cao-system", title="a")
        got = tracker.delete_project("cao-system", force=True)
        assert got["issues_deleted"] == 1
        assert tracker.list_projects() == []

    def test_archiving_hides_a_project_without_touching_its_issues(self, cao_system):
        tracker.create_issue(project_id="cao-system", title="a")
        tracker.update_project("cao-system", status="archived")
        assert tracker.list_projects() == []
        assert len(tracker.list_projects(include_archived=True)) == 1
        assert tracker.list_issues(project_id="cao-system")["total"] == 1


class TestTimestamps:
    def test_stored_timestamps_serialize_as_explicit_utc(self, cao_system):
        issue = tracker.create_issue(project_id="cao-system", title="a")
        assert issue["created_at"].endswith("Z")

    def test_a_supplied_creation_stamp_is_preserved(self, cao_system):
        when = datetime(2026, 7, 21, 11, 44, 29, tzinfo=timezone.utc)
        issue = tracker.create_issue(
            project_id="cao-system", title="a", key="cond-0025", created_at=when
        )
        assert issue["created_at"] == "2026-07-21T11:44:29Z"


class TestMarkdownExport:
    def test_the_export_carries_key_severity_and_metadata(self, cao_system):
        tracker.create_issue(
            project_id="cao-system",
            title="event-mirror lock contention logs full traceback",
            severity="P2",
            reporter="13e6fe47",
            failing_command="python3 -B probes.py",
            body="Independent validation confirmed the mirror stays bounded.",
        )
        rendered = tracker.render_markdown("cao-system")
        assert "## cond-0001 — [P2] event-mirror lock contention" in rendered
        assert "- **reporter:** 13e6fe47" in rendered
        assert "python3 -B probes.py" in rendered

    def test_the_export_defaults_to_open_issues(self, cao_system):
        a = tracker.create_issue(project_id="cao-system", title="done")
        tracker.create_issue(project_id="cao-system", title="pending")
        tracker.update_issue(a["key"], status="closed")
        rendered = tracker.render_markdown("cao-system")
        assert "pending" in rendered
        assert "done" not in rendered


class TestStats:
    def test_stats_break_down_by_status_severity_and_component(self, cao_system):
        tracker.create_issue(project_id="cao-system", title="a", severity="P1", component="conduct")
        tracker.create_issue(project_id="cao-system", title="b", severity="P1", component="fork")
        got = tracker.stats("cao-system")
        assert got["total"] == 2
        assert got["by_severity"]["P1"] == 2
        assert sorted(got["by_component"]) == ["conduct", "fork"]


class TestPrefixUniqueness:
    """Issue keys are unique across the installation, not per project.

    `cond-0242` appears in commit messages, reports and evidence paths, so it
    has to mean one thing. Two projects sharing a prefix would collide at
    key-allocation time with a conflict naming a project the caller never
    mentioned — found by importing a 208-entry ledger into a second project
    that also used `cond`.
    """

    def test_a_second_project_cannot_claim_a_used_prefix(self, cao_system):
        with pytest.raises(TrackerError) as exc:
            tracker.create_project(name="Other", project_id="other", issue_prefix="cond")
        assert exc.value.code == "conflict"
        assert "cao-system" in exc.value.message

    def test_a_project_cannot_be_renamed_onto_a_used_prefix(self, cao_system):
        tracker.create_project(name="Other", project_id="other", issue_prefix="oth")
        with pytest.raises(TrackerError):
            tracker.update_project("other", issue_prefix="cond")

    def test_a_project_may_keep_its_own_prefix_through_an_unrelated_edit(self, cao_system):
        # The uniqueness check must exclude the project being edited, or every
        # rename would refuse itself.
        updated = tracker.update_project("cao-system", issue_prefix="cond", name="Renamed")
        assert updated["issue_prefix"] == "cond"

    def test_a_freed_prefix_can_be_reclaimed(self, cao_system):
        tracker.delete_project("cao-system", force=True)
        reclaimed = tracker.create_project(name="Successor", project_id="succ", issue_prefix="cond")
        assert reclaimed["issue_prefix"] == "cond"


class TestExportCompleteness:
    def test_the_export_pages_past_the_list_page_size(self, cao_system):
        # `list_issues` caps a page at 500. This export replaces a file that
        # held every entry, so a cap here would silently drop issue 501 onward
        # and the rendered log would look complete.
        for i in range(505):
            tracker.create_issue(project_id="cao-system", title=f"issue {i}")
        rendered = tracker.render_markdown("cao-system")
        assert rendered.count("\n## cond-") == 505
        assert "505 issue(s)" in rendered


class TestPrefixNamespaceIsTheKeyTable:
    """A vacated prefix is not a free prefix.

    Found by independent review. Ownership was checked against the PROJECT
    table, so a project that renamed its prefix left its issues holding those
    keys while the prefix read as unowned. Giving it to a new project made every
    filing collide — and because the counter bump shares a transaction with the
    insert, the rollback took the bump too, so the counter never advanced and
    the wedge was permanent: HTTP 500 on every filing, forever.
    """

    def test_a_vacated_prefix_is_still_refused_while_keys_hold_it(self, cao_system):
        tracker.create_issue(project_id="cao-system", title="a")
        tracker.update_project("cao-system", issue_prefix="cs")
        with pytest.raises(TrackerError) as exc:
            tracker.create_project(name="Beta", project_id="beta", issue_prefix="cond")
        assert exc.value.code == "conflict"
        assert "cond-0001" in exc.value.message

    def test_the_refusal_names_a_key_and_its_project(self, cao_system):
        tracker.create_issue(project_id="cao-system", title="a")
        tracker.update_project("cao-system", issue_prefix="cs")
        with pytest.raises(TrackerError) as exc:
            tracker.create_project(name="Beta", project_id="beta", issue_prefix="cond")
        assert "cao-system" in exc.value.message

    def test_a_prefix_with_no_keys_left_is_reclaimable(self, cao_system):
        # Vacated AND unused must stay available, or a typo'd prefix would be
        # burned permanently.
        tracker.update_project("cao-system", issue_prefix="cs")
        got = tracker.create_project(name="Beta", project_id="beta", issue_prefix="cond")
        assert got["issue_prefix"] == "cond"

    def test_force_deleting_a_project_frees_its_prefix_with_its_keys(self, cao_system):
        tracker.create_issue(project_id="cao-system", title="a")
        tracker.delete_project("cao-system", force=True)
        got = tracker.create_project(name="Successor", project_id="succ", issue_prefix="cond")
        assert got["issue_prefix"] == "cond"

    def test_a_project_keeps_its_own_prefix_through_an_unrelated_edit(self, cao_system):
        tracker.create_issue(project_id="cao-system", title="a")
        updated = tracker.update_project("cao-system", name="Renamed", issue_prefix="cond")
        assert updated["issue_prefix"] == "cond"


class TestDuplicateScopesInOneRequest:
    def test_the_same_value_twice_is_a_conflict_not_a_500(self, tmp_path):
        with pytest.raises(TrackerError) as exc:
            tracker.create_project(
                name="P",
                project_id="p",
                scopes=[
                    {"kind": "path", "value": str(tmp_path)},
                    {"kind": "path", "value": str(tmp_path) + "/"},
                ],
            )
        assert exc.value.code == "conflict"
        assert "twice" in exc.value.message


class TestLabelFilterWildcards:
    """`%` and `_` are LIKE wildcards and legal label characters."""

    def test_an_underscore_label_does_not_match_every_single_character_label(self, cao_system):
        tracker.create_issue(project_id="cao-system", title="a", labels=["_"])
        tracker.create_issue(project_id="cao-system", title="b", labels=["x"])
        got = tracker.list_issues(project_id="cao-system", label="_")
        assert [i["title"] for i in got["issues"]] == ["a"]

    def test_a_percent_label_does_not_match_everything(self, cao_system):
        tracker.create_issue(project_id="cao-system", title="a", labels=["100%"])
        tracker.create_issue(project_id="cao-system", title="b", labels=["other"])
        got = tracker.list_issues(project_id="cao-system", label="%")
        assert got["total"] == 0
