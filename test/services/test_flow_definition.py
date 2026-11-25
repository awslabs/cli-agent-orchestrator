"""Flow definition rules, fire times and in-place edits (flow-authoring-api).

Covers ``flow_service.validate_flow_definition``, ``flow_service.next_fire_times`` and
``flow_service.update_flow``. Attributes are read off the module at call time so each test
reports its own missing symbol instead of one collection error for the whole file.
"""

import os
import stat
import time
from datetime import datetime
from pathlib import Path

import frontmatter  # type: ignore
import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.services import flow_service

pytestmark = pytest.mark.usefixtures("isolated_memory_db")

GOOD = {
    "name": "daily-report",
    "schedule": "0 9 * * mon-fri",
    "agent_profile": "developer",
    "provider": "claude_code",
}


def naive(moment: datetime) -> datetime:
    """Wall-clock value of a fire time, comparable with the naive ``after`` arguments."""
    return moment.replace(tzinfo=None)


def errors_for(**override):
    """Field -> message for the definition ``GOOD`` with ``override`` applied (edit rules)."""
    errors = flow_service.validate_flow_definition({**GOOD, **override}, must_be_new=False)
    return {e["field"]: e["message"] for e in errors}


def register_row(name="daily-report", file_path="/flows/x.flow.md", schedule="0 9 * * *"):
    database.create_flow(
        name=name,
        file_path=file_path,
        schedule=schedule,
        agent_profile="developer",
        provider="kiro_cli",
        script="",
        next_run=datetime(2030, 1, 1, 9, 0),
    )


# --------------------------------------------------------------------------- #
# validate_flow_definition
# --------------------------------------------------------------------------- #


def test_a_prompt_template_is_checked_only_when_the_caller_asks_for_it():
    # add_flow and update_flow hold no prompt in their definition; only the authoring routes do.
    assert flow_service.validate_flow_definition(GOOD, must_be_new=False) == []

    errors = flow_service.validate_flow_definition(
        GOOD, must_be_new=False, require_prompt_template=True
    )

    assert errors == [{"field": "prompt_template", "message": "is required"}]


@pytest.mark.parametrize(
    ("value", "message"),
    [(5, "must be a string"), (["a"], "must be a string"), ({"a": 1}, "must be a string")],
)
def test_a_prompt_template_that_is_not_a_string_is_refused(value, message):
    errors = flow_service.validate_flow_definition(
        {**GOOD, "prompt_template": value}, must_be_new=False, require_prompt_template=True
    )

    assert errors == [{"field": "prompt_template", "message": message}]


@pytest.mark.parametrize("value", ["", "Report.", "line one\nline two", "---\nscript: x\n---"])
def test_any_string_is_an_acceptable_prompt_template(value):
    errors = flow_service.validate_flow_definition(
        {**GOOD, "prompt_template": value}, must_be_new=False, require_prompt_template=True
    )

    assert errors == []


def test_a_valid_definition_has_no_errors():
    assert flow_service.validate_flow_definition(GOOD, must_be_new=False) == []


def test_errors_are_field_and_message_pairs_one_per_bad_field():
    bad = {"name": "bad/name", "schedule": "61 * * * *", "agent_profile": "ok", "provider": "ok"}

    errors = flow_service.validate_flow_definition(bad, must_be_new=False)

    assert sorted(e["field"] for e in errors) == ["name", "schedule"]
    assert all(set(e) == {"field", "message"} for e in errors)
    assert all(isinstance(e["message"], str) and e["message"] for e in errors)


def test_validation_does_not_modify_its_input():
    fields = dict(GOOD, name="bad/name")
    snapshot = dict(fields)

    flow_service.validate_flow_definition(fields, must_be_new=True)

    assert fields == snapshot


@pytest.mark.parametrize("name", ["a", "A_b-9", "0", "true", "123", "a" * 64])
def test_names_at_the_edges_of_the_rule_are_accepted(name):
    assert errors_for(name=name) == {}


@pytest.mark.parametrize(
    "name",
    [
        "",
        "a" * 65,
        "bad/name",
        "..",
        ".",
        "a.b",
        "../evil",
        "/abs",
        "back\\slash",
        "has space",
        " lead",
        "trail ",
        "tab\t",
        "line\nbreak",
        "good\n",
        "名前",
        "ｆｌｏｗ",
        "café",
    ],
)
def test_names_outside_the_rule_are_name_errors_and_nothing_else(name):
    assert set(errors_for(name=name)) == {"name"}


@pytest.mark.parametrize(
    "cron",
    [
        "59 23 31 12 *",
        "0 0 1 1 *",
        "0 0 29 2 *",
        "* * * * 0",
        "*/5 * * * *",
        "0 9 * * mon-fri",
        "0 9 * * 1-5",
        " 0 9 * * * ",
    ],
)
def test_crons_the_scheduler_accepts_are_accepted(cron):
    assert errors_for(schedule=cron) == {}


@pytest.mark.parametrize(
    "cron",
    [
        "61 * * * *",
        "60 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "* * 32 * *",
        "* * * 0 *",
        "* * * 13 *",
        "* * * * 7",
        "not a cron",
        "",
        "   ",
        "* * * *",
        "* * * * * *",
        "@daily",
        "0 0 30 2 *",
    ],
)
def test_crons_the_scheduler_refuses_or_that_never_fire_are_schedule_errors(cron):
    assert set(errors_for(schedule=cron)) == {"schedule"}


SCHEDULE_CAP = 256


def schedule_of_length(length):
    """A schedule that fires (every minute of every hour, listed out) padded to ``length``."""
    minutes = ",".join(str(m) for m in range(60))
    hours = ",".join(str(h) for h in range(24))
    return f"{minutes} {hours} * * *".ljust(length)


def never_firing(repeats):
    """31 February listed ``repeats`` times: it parses but never fires."""
    return "0 0 " + ",".join(["31"] * repeats) + " 2 *"


def test_a_schedule_at_the_length_cap_is_accepted():
    assert errors_for(schedule=schedule_of_length(SCHEDULE_CAP)) == {}


def test_a_schedule_one_past_the_length_cap_is_refused_as_too_long():
    errors = errors_for(schedule=schedule_of_length(SCHEDULE_CAP + 1))

    assert set(errors) == {"schedule"}
    assert "too long" in errors["schedule"]


def test_a_long_schedule_that_never_fires_is_refused_quickly():
    started = time.monotonic()

    errors = errors_for(schedule=never_firing(250))

    assert time.monotonic() - started < 1
    assert set(errors) == {"schedule"}


@pytest.mark.parametrize("field", ["schedule", "agent_profile", "provider"])
@pytest.mark.parametrize("char", ["\n", "\r", "\t", "\x00", "\x1b", "\x7f"])
def test_a_control_character_inside_the_value_is_refused(field, char):
    base = {"schedule": "0 9 * * *", "agent_profile": "dev", "provider": "claude_code"}[field]
    value = base[:2] + char + base[2:]

    assert set(errors_for(**{field: value})) == {field}


@pytest.mark.parametrize("field", ["schedule", "agent_profile", "provider"])
def test_a_smuggled_front_matter_key_is_refused(field):
    value = "0 9 * * *\nscript: /tmp/x" if field == "schedule" else "dev\nscript: /tmp/x"

    assert field in errors_for(**{field: value})


@pytest.mark.parametrize("field", ["agent_profile", "provider"])
@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_profile_or_provider_is_refused(field, value):
    assert set(errors_for(**{field: value})) == {field}


@pytest.mark.parametrize("value", ["développeur", "日本語", "dev: team #1"])
def test_unicode_and_punctuation_in_a_profile_are_accepted(value):
    assert errors_for(agent_profile=value) == {}


def test_missing_fields_are_errors_not_exceptions():
    errors = flow_service.validate_flow_definition({}, must_be_new=False)

    assert {"name", "schedule", "agent_profile"} <= {e["field"] for e in errors}


@pytest.mark.parametrize("field", ["name", "schedule", "agent_profile", "provider"])
@pytest.mark.parametrize("value", [None, 123, 12.5, True, ["a"], {"a": 1}])
def test_a_value_that_is_not_a_string_is_a_field_error_not_an_exception(field, value):
    assert set(errors_for(**{field: value})) == {field}


def test_an_existing_name_is_refused_only_when_the_name_must_be_new():
    register_row()

    taken = flow_service.validate_flow_definition(GOOD, must_be_new=True)
    edit = flow_service.validate_flow_definition(GOOD, must_be_new=False)

    assert [e["field"] for e in taken] == ["name"]
    assert "exist" in taken[0]["message"].lower()
    assert edit == []


def test_an_unregistered_name_passes_the_must_be_new_check():
    register_row(name="other-flow")

    assert flow_service.validate_flow_definition(GOOD, must_be_new=True) == []


def test_an_existing_name_and_a_bad_cron_are_both_reported():
    register_row()

    errors = flow_service.validate_flow_definition({**GOOD, "schedule": "nope"}, must_be_new=True)

    assert sorted(e["field"] for e in errors) == ["name", "schedule"]


def test_validation_touches_no_file(monkeypatch):
    register_row()
    import builtins
    import pathlib

    def boom(*_args, **_kwargs):
        raise AssertionError("validate_flow_definition touched the file system")

    with monkeypatch.context() as patched:
        for attr in ("exists", "is_file", "is_dir", "read_text", "write_text", "open", "stat"):
            patched.setattr(pathlib.Path, attr, boom)
        patched.setattr(builtins, "open", boom)
        for attr in ("exists", "isfile", "isdir", "islink", "realpath"):
            patched.setattr(os.path, attr, boom)
        for attr in ("stat", "listdir", "scandir"):
            patched.setattr(os, attr, boom)
        # control: the database lookup the validator needs must not trip the same guard
        assert database.get_flow("daily-report") is not None

        errors = flow_service.validate_flow_definition(GOOD, must_be_new=True)

    assert [e["field"] for e in errors] == ["name"]


# --------------------------------------------------------------------------- #
# next_fire_times and _get_next_run_time
# --------------------------------------------------------------------------- #

WEDNESDAY_AFTER_NINE = datetime(2026, 11, 18, 10, 0)  # 2026-11-18 is a Wednesday


def test_weekday_names_skip_the_weekend():
    times = flow_service.next_fire_times("0 9 * * mon-fri", 3, WEDNESDAY_AFTER_NINE)

    assert [naive(t) for t in times] == [
        datetime(2026, 11, 19, 9, 0),  # Thursday
        datetime(2026, 11, 20, 9, 0),  # Friday
        datetime(2026, 11, 23, 9, 0),  # Monday
    ]


def test_numeric_day_of_week_follows_the_scheduler():
    # The scheduler counts days from Monday as 0, so 1-5 is Tuesday to Saturday; its reading
    # is authoritative (spec "Numeric day of week follows the scheduler").
    times = flow_service.next_fire_times("0 9 * * 1-5", 3, WEDNESDAY_AFTER_NINE)

    assert [t.strftime("%A") for t in times] == ["Thursday", "Friday", "Saturday"]
    assert all((t.hour, t.minute) == (9, 0) for t in times)


def test_numeric_day_of_week_never_fires_on_sunday_or_monday():
    times = flow_service.next_fire_times("0 9 * * 1-5", 12, WEDNESDAY_AFTER_NINE)

    assert len(times) == 12
    assert {t.strftime("%A") for t in times} == {
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
    }


def test_the_weekend_wraps_into_the_next_week():
    friday_after_nine = datetime(2026, 11, 20, 10, 0)

    times = flow_service.next_fire_times("0 9 * * mon-fri", 3, friday_after_nine)

    assert [t.strftime("%A") for t in times] == ["Monday", "Tuesday", "Wednesday"]


def test_fire_times_are_consecutive_and_strictly_ascending():
    times = flow_service.next_fire_times("* * * * *", 3, datetime(2026, 11, 18, 10, 0, 30))

    assert [naive(t) for t in times] == [
        datetime(2026, 11, 18, 10, 1),
        datetime(2026, 11, 18, 10, 2),
        datetime(2026, 11, 18, 10, 3),
    ]


def test_fire_times_cross_a_year_boundary():
    times = flow_service.next_fire_times("0 0 1 * *", 3, datetime(2026, 11, 15))

    assert [naive(t) for t in times] == [
        datetime(2026, 12, 1),
        datetime(2027, 1, 1),
        datetime(2027, 2, 1),
    ]


def test_a_leap_day_schedule_finds_the_next_leap_year():
    times = flow_service.next_fire_times("0 0 29 2 *", 1, datetime(2026, 3, 1))

    assert [naive(t) for t in times] == [datetime(2028, 2, 29)]


def test_a_leap_day_schedule_keeps_its_next_three_runs():
    times = flow_service.next_fire_times("0 0 29 2 *", 3, datetime(2026, 3, 1))

    assert [naive(t) for t in times] == [
        datetime(2028, 2, 29),
        datetime(2032, 2, 29),
        datetime(2036, 2, 29),
    ]


def test_a_leap_day_on_a_weekday_fires_across_the_forty_year_gap():
    # Day and weekday must both match, so 29 February on a Monday recurs 28 or 40 years apart
    # (2072 to 2112 skips the non-leap 2100): the longest gap a schedule that fires can have.
    times = flow_service.next_fire_times("0 0 29 2 mon", 3, datetime(2026, 3, 1))

    assert [naive(t) for t in times] == [
        datetime(2044, 2, 29),
        datetime(2072, 2, 29),
        datetime(2112, 2, 29),
    ]


def test_a_long_list_that_never_fires_stops_scanning_quickly():
    started = time.monotonic()

    times = flow_service.next_fire_times(never_firing(250), 3, WEDNESDAY_AFTER_NINE)

    assert time.monotonic() - started < 1
    assert times == []


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        (datetime(2026, 11, 18, 8, 59, 59), datetime(2026, 11, 18, 9, 0)),
        (datetime(2026, 11, 18, 9, 0, 0, 1), datetime(2026, 11, 19, 9, 0)),
    ],
)
def test_the_first_fire_time_is_on_the_right_side_of_the_scheduled_minute(after, expected):
    times = flow_service.next_fire_times("0 9 * * *", 1, after)

    assert [naive(t) for t in times] == [expected]


@pytest.mark.parametrize("count", [0, 1, 5])
def test_it_returns_exactly_the_number_asked_for(count):
    times = flow_service.next_fire_times("* * * * *", count, WEDNESDAY_AFTER_NINE)

    assert len(times) == count
    assert times == sorted(times)
    assert len(set(times)) == count


def test_without_after_the_first_time_is_not_in_the_past():
    before = datetime.now()

    times = flow_service.next_fire_times("* * * * *", 3)

    assert len(times) == 3
    assert naive(times[0]) >= before.replace(microsecond=0)
    assert times == sorted(times)


@pytest.mark.parametrize("cron", ["not a cron", "61 * * * *", ""])
def test_a_bad_cron_raises_value_error(cron):
    with pytest.raises(ValueError):
        flow_service.next_fire_times(cron, 3, WEDNESDAY_AFTER_NINE)


def test_get_next_run_time_still_returns_one_time_not_before_now():
    before = datetime.now()

    result = flow_service._get_next_run_time("* * * * *")

    assert isinstance(result, datetime)
    assert naive(result) >= before.replace(microsecond=0)


def test_get_next_run_time_still_follows_day_names():
    result = flow_service._get_next_run_time("0 9 * * mon-fri")

    assert (result.hour, result.minute) == (9, 0)
    assert result.weekday() < 5


@pytest.mark.parametrize("cron", ["invalid cron", "0 0 30 2 *"])
def test_get_next_run_time_still_raises_for_a_cron_that_cannot_fire(cron):
    with pytest.raises(ValueError):
        flow_service._get_next_run_time(cron)


# --------------------------------------------------------------------------- #
# add_flow goes through the same rules
# --------------------------------------------------------------------------- #


def test_add_flow_refuses_a_control_character_in_the_profile(tmp_path):
    path = tmp_path / "evil.flow.md"
    path.write_text(
        '---\nname: evil\nschedule: "0 9 * * *"\nagent_profile: "dev\\nscript: /tmp/x"\n---\nHi\n'
    )

    with pytest.raises(ValueError):
        flow_service.add_flow(str(path))

    assert database.get_flow("evil") is None


# --------------------------------------------------------------------------- #
# update_flow
# --------------------------------------------------------------------------- #


def write_flow_file(home, name="daily-report", schedule="0 9 * * *", extra="", body="Report.\n"):
    path = home / "flows" / f"{name}.flow.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'---\nname: {name}\nschedule: "{schedule}"\nagent_profile: developer\n'
        f"provider: claude_code\n{extra}---\n{body}",
        encoding="utf-8",
    )
    return path


def register_flow(home, name="daily-report", enabled=True, **file_args):
    """A flow file under ``home/flows`` registered the way ``cao schedule add`` does."""
    path = write_flow_file(home, name, **file_args)
    flow_service.add_flow(str(path))
    if not enabled:
        database.update_flow_enabled(name, False)
    return path, database.get_flow(name)


def edit(home, changes, name="daily-report"):
    return flow_service.update_flow(name, changes, home / "flows")


def listing(directory):
    return sorted(p.name for p in directory.iterdir())


def test_rescheduling_an_enabled_flow_recomputes_next_run_and_is_not_refused_for_its_name(
    tmp_path,
):
    path, before = register_flow(tmp_path)
    now = datetime.now()

    flow = edit(tmp_path, {"schedule": "30 8 * * *"})

    assert flow.schedule == "30 8 * * *"
    assert (flow.next_run.hour, flow.next_run.minute) == (8, 30)
    assert naive(flow.next_run) >= now.replace(microsecond=0)
    row = database.get_flow("daily-report")
    assert (row.schedule, row.next_run) == (flow.schedule, flow.next_run)
    assert frontmatter.load(str(path)).metadata["schedule"] == "30 8 * * *"
    assert before.schedule == "0 9 * * *"


@pytest.mark.parametrize(("field", "value"), [("agent_profile", "reviewer"), ("provider", "q_cli")])
def test_each_editable_field_reaches_the_file_and_the_row(tmp_path, field, value):
    path, _ = register_flow(tmp_path)

    edit(tmp_path, {field: value})

    assert frontmatter.load(str(path)).metadata[field] == value
    assert getattr(database.get_flow("daily-report"), field) == value


def test_fields_left_out_keep_their_stored_values(tmp_path):
    path, before = register_flow(tmp_path)

    edit(tmp_path, {"schedule": "30 8 * * *"})

    meta = frontmatter.load(str(path)).metadata
    assert (meta["name"], meta["agent_profile"], meta["provider"]) == (
        "daily-report",
        "developer",
        "claude_code",
    )
    row = database.get_flow("daily-report")
    assert (row.agent_profile, row.provider, row.script) == ("developer", "claude_code", "")


def test_an_edit_that_changes_nothing_succeeds(tmp_path):
    path, _ = register_flow(tmp_path)

    flow = edit(tmp_path, {"schedule": "0 9 * * *", "agent_profile": "developer"})

    assert flow.schedule == "0 9 * * *"
    assert frontmatter.load(str(path)).metadata["agent_profile"] == "developer"


@pytest.mark.parametrize("engine", ["v2", "v3", "kas"])
def test_an_edit_keeps_script_engine_and_every_other_key_unchanged(tmp_path, engine):
    extra = (
        f"script: gate.py\nengine: {engine}\nowner: team-a\nretries: 3\ntags: [a, b]\n"
        'limits:\n  cpu: 2\n  note: "café"\n'
    )
    path, _ = register_flow(tmp_path, extra=extra)
    original = dict(frontmatter.load(str(path)).metadata)

    edit(tmp_path, {"schedule": "30 8 * * *"})

    after = frontmatter.load(str(path)).metadata
    untouched = {k: v for k, v in original.items() if k != "schedule"}
    assert {k: after[k] for k in untouched} == untouched
    assert after["script"] == "gate.py"
    assert after["engine"] == engine
    assert set(after) == set(original)
    assert database.get_flow("daily-report").script == "gate.py"
    assert flow_service.get_flow("daily-report").engine == KiroEngine(engine)


def test_the_prompt_body_is_replaced_and_the_front_matter_is_not(tmp_path):
    path, _ = register_flow(tmp_path, extra="script: gate.py\n")

    edit(tmp_path, {"prompt_template": "New body."})

    post = frontmatter.load(str(path))
    assert post.content.strip() == "New body."
    assert post.metadata["agent_profile"] == "developer"
    assert post.metadata["schedule"] == "0 9 * * *"
    assert post.metadata["script"] == "gate.py"
    assert flow_service.get_flow("daily-report").prompt_template == "New body."


def test_the_prompt_body_survives_an_edit_that_does_not_name_it(tmp_path):
    body = "Intro café\n\n---\n\n```py\nprint(1)\n```\n\n日本語\n"
    path, _ = register_flow(tmp_path, body=body)
    before = frontmatter.load(str(path)).content

    edit(tmp_path, {"schedule": "30 8 * * *"})

    assert frontmatter.load(str(path)).content == before


def test_a_prompt_that_looks_like_front_matter_cannot_add_a_key(tmp_path):
    path, _ = register_flow(tmp_path)

    edit(tmp_path, {"prompt_template": "---\nscript: /tmp/evil.sh\n---\nHi"})

    assert "script" not in frontmatter.load(str(path)).metadata
    assert database.get_flow("daily-report").script == ""
    assert flow_service.get_flow("daily-report").script == ""


def test_a_one_megabyte_prompt_is_kept_whole(tmp_path):
    path, _ = register_flow(tmp_path)
    prompt = "x" * (1024 * 1024)

    edit(tmp_path, {"prompt_template": prompt})

    assert frontmatter.load(str(path)).content.strip() == prompt


@pytest.mark.parametrize(
    "value",
    [
        "développeur",
        "日本語",
        "dev: team #1",
        'it\'s "quoted"',
        "yes",
        "null",
        "123",
        "- item",
        "*star",
        "{a: b}",
        "a" * 500,
    ],
)
def test_yaml_special_and_unicode_values_round_trip_as_strings(tmp_path, value):
    path, _ = register_flow(tmp_path)

    edit(tmp_path, {"agent_profile": value})

    assert frontmatter.load(str(path)).metadata["agent_profile"] == value
    assert database.get_flow("daily-report").agent_profile == value


@pytest.mark.parametrize("cron", ["* * * * *", "*/5 * * * *", "0 9 * * mon-fri"])
def test_a_schedule_that_yaml_would_misread_round_trips_through_the_file(tmp_path, cron):
    path, _ = register_flow(tmp_path)

    edit(tmp_path, {"schedule": cron})

    assert frontmatter.load(str(path)).metadata["schedule"] == cron
    assert flow_service.get_flow("daily-report").schedule == cron


@pytest.mark.parametrize(
    ("changes", "fields"),
    [
        ({"schedule": "nope"}, ["schedule"]),
        ({"schedule": "0 0 30 2 *"}, ["schedule"]),
        ({"agent_profile": ""}, ["agent_profile"]),
        ({"provider": "a\nb"}, ["provider"]),
        ({"schedule": "nope", "provider": "   "}, ["provider", "schedule"]),
        ({"schedule": "nope", "prompt_template": "Changed."}, ["schedule"]),
    ],
)
def test_an_invalid_merged_definition_changes_nothing(tmp_path, changes, fields):
    path, before = register_flow(tmp_path)
    raw = path.read_bytes()
    names = listing(path.parent)

    with pytest.raises(flow_service.FlowDefinitionError) as raised:
        edit(tmp_path, changes)

    assert sorted(e["field"] for e in raised.value.errors) == fields
    assert path.read_bytes() == raw
    assert listing(path.parent) == names
    assert database.get_flow("daily-report") == before


def test_a_disabled_flow_stays_disabled(tmp_path):
    path, _ = register_flow(tmp_path, enabled=False)

    flow = edit(tmp_path, {"schedule": "30 8 * * *"})

    assert flow.enabled is False
    row = database.get_flow("daily-report")
    assert row.enabled is False
    assert row.schedule == "30 8 * * *"
    assert frontmatter.load(str(path)).metadata["schedule"] == "30 8 * * *"
    assert flow_service.get_flows_to_run() == []


def test_the_same_edit_twice_gives_the_same_file(tmp_path):
    path, _ = register_flow(tmp_path)
    changes = {"schedule": "30 8 * * *", "prompt_template": "Same."}

    edit(tmp_path, changes)
    first = path.read_bytes()
    edit(tmp_path, changes)

    assert path.read_bytes() == first


def test_a_flow_registered_from_another_directory_is_not_rewritten(tmp_path):
    path, before = register_flow(tmp_path / "elsewhere")
    raw = path.read_bytes()

    with pytest.raises(flow_service.FlowFileOutsideHomeError):
        edit(tmp_path, {"schedule": "30 8 * * *"})

    assert path.read_bytes() == raw
    assert database.get_flow("daily-report") == before


def test_a_directory_whose_name_only_starts_like_the_flows_directory_is_outside(tmp_path):
    outside = tmp_path / "flows-evil" / "daily-report.flow.md"
    outside.parent.mkdir()
    outside.write_text(
        '---\nname: daily-report\nschedule: "0 9 * * *"\nagent_profile: d\n---\nHi\n'
    )
    register_row(file_path=str(outside))
    raw = outside.read_bytes()
    (tmp_path / "flows").mkdir()

    with pytest.raises(flow_service.FlowFileOutsideHomeError):
        edit(tmp_path, {"schedule": "30 8 * * *"})

    assert outside.read_bytes() == raw


def test_a_symlink_inside_the_flows_directory_that_points_out_is_not_followed(tmp_path):
    target = tmp_path / "elsewhere" / "daily-report.flow.md"
    target.parent.mkdir()
    target.write_text('---\nname: daily-report\nschedule: "0 9 * * *"\nagent_profile: d\n---\nHi\n')
    link = tmp_path / "flows" / "daily-report.flow.md"
    link.parent.mkdir()
    link.symlink_to(target)
    register_row(file_path=str(link))
    raw = target.read_bytes()

    with pytest.raises(flow_service.FlowFileOutsideHomeError):
        edit(tmp_path, {"schedule": "30 8 * * *"})

    assert target.read_bytes() == raw
    assert link.is_symlink()


def test_a_dot_dot_path_that_resolves_outside_the_flows_directory_is_refused(tmp_path):
    target = tmp_path / "elsewhere" / "daily-report.flow.md"
    target.parent.mkdir()
    target.write_text('---\nname: daily-report\nschedule: "0 9 * * *"\nagent_profile: d\n---\nHi\n')
    (tmp_path / "flows").mkdir()
    register_row(file_path=f"{tmp_path}/flows/../elsewhere/daily-report.flow.md")
    raw = target.read_bytes()

    with pytest.raises(flow_service.FlowFileOutsideHomeError):
        edit(tmp_path, {"schedule": "30 8 * * *"})

    assert target.read_bytes() == raw


def test_an_unknown_flow_raises_not_found_and_writes_nothing(tmp_path):
    with pytest.raises(flow_service.FlowNotFoundError):
        edit(tmp_path, {"schedule": "30 8 * * *"}, name="missing")

    assert not list(tmp_path.rglob("*.flow.md"))


def test_a_registered_flow_whose_file_is_gone_is_not_reported_as_an_unknown_flow(tmp_path):
    path, before = register_flow(tmp_path)
    path.unlink()

    with pytest.raises(ValueError) as raised:
        edit(tmp_path, {"schedule": "30 8 * * *"})

    assert not isinstance(raised.value, flow_service.FlowNotFoundError)
    assert not path.exists()
    assert database.get_flow("daily-report") == before


@pytest.mark.parametrize("mode", [0o600, 0o640, 0o644, 0o664, 0o755])
def test_an_edit_keeps_the_files_permission_bits(tmp_path, mode):
    path, _ = register_flow(tmp_path)
    path.chmod(mode)

    edit(tmp_path, {"schedule": "30 8 * * *", "prompt_template": "New body."})

    assert stat.S_IMODE(path.stat().st_mode) == mode


def _spy_on_renames(monkeypatch):
    calls = []

    def spy(real):
        def wrapper(src, dst, *args, **kwargs):
            calls.append((Path(src), Path(dst)))
            return real(src, dst, *args, **kwargs)

        return wrapper

    monkeypatch.setattr(os, "replace", spy(os.replace))
    monkeypatch.setattr(os, "rename", spy(os.rename))
    return calls


def test_the_rewrite_is_a_temp_file_in_the_same_directory_then_a_rename(tmp_path, monkeypatch):
    path, _ = register_flow(tmp_path)
    calls = _spy_on_renames(monkeypatch)

    edit(tmp_path, {"schedule": "30 8 * * *"})

    assert len(calls) == 1
    source, destination = calls[0]
    assert destination.resolve() == path.resolve()
    assert source != destination
    assert source.resolve().parent == destination.resolve().parent
    assert listing(path.parent) == [path.name]


def test_a_failure_before_the_rename_leaves_the_old_file_and_row_and_no_temp_file(
    tmp_path, monkeypatch
):
    path, before = register_flow(tmp_path)
    raw = path.read_bytes()
    update = flow_service.update_flow  # resolved here so a missing function is not "the failure"

    def refuse(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", refuse)
    monkeypatch.setattr(os, "rename", refuse)

    with pytest.raises(Exception):
        update("daily-report", {"schedule": "30 8 * * *"}, tmp_path / "flows")

    assert path.read_bytes() == raw
    assert listing(path.parent) == [path.name]
    assert database.get_flow("daily-report") == before
