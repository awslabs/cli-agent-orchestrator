"""Authoring behaviour of the flow routes: validate, create and update (flow-authoring-api).

These tests run the real ``flow_service`` and database (``isolated_memory_db``) against a scratch
CAO home, so a route that writes the wrong file, or writes at all, shows up on disk.
"""

import os
import stat
import time
from datetime import datetime
from pathlib import Path

import frontmatter  # type: ignore
import pytest

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.security import auth
from cli_agent_orchestrator.services import flow_service

GOOD = {
    "name": "daily-report",
    "schedule": "0 9 * * mon-fri",
    "agent_profile": "developer",
    "provider": "claude_code",
    "prompt_template": "Report.",
}


@pytest.fixture
def flows_home(tmp_path, monkeypatch, isolated_memory_db):
    """A scratch CAO home and an isolated database; the routes write only here."""
    monkeypatch.setattr("cli_agent_orchestrator.api.main.CAO_HOME_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def scoped(client, monkeypatch):
    """Turn auth on; ``scoped(["cao:read"])`` returns the client holding exactly those scopes."""
    monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example/jwks")

    def use(scopes):
        async def dependency():
            return list(scopes)

        app.dependency_overrides[auth.get_current_scopes] = dependency
        return client

    yield use
    app.dependency_overrides.pop(auth.get_current_scopes, None)


def flow_file(home, name="daily-report"):
    return home / "flows" / f"{name}.flow.md"


def files_under(home):
    """Files the routes left in the CAO home (the per-test database sits there too)."""
    return sorted(
        str(p.relative_to(home))
        for p in home.rglob("*")
        if p.is_file() and not p.name.startswith("memory-metadata")
    )


SCHEDULE_CAP = 256


def schedule_of_length(length):
    """A schedule that fires (every minute of every hour, listed out) padded to ``length``."""
    minutes = ",".join(str(m) for m in range(60))
    hours = ",".join(str(h) for h in range(24))
    return f"{minutes} {hours} * * *".ljust(length)


def runs_of(body):
    return [datetime.fromisoformat(t) for t in body["next_runs"]]


def validated(client, body):
    """POST /flows/validate and return its JSON, failing on any status but 200."""
    response = client.post("/flows/validate", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def edited(client, name, body):
    """PUT /flows/{name} and return its JSON, failing on any status but 200."""
    response = client.put(f"/flows/{name}", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def register_flow(home, name="daily-report", enabled=True, subdir="flows", extra=""):
    """Register a flow the way ``cao schedule add`` does, without using the create route."""
    path = home / subdir / f"{name}.flow.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'---\nname: {name}\nschedule: "0 9 * * *"\nagent_profile: developer\n'
        f"provider: claude_code\n{extra}---\nReport.\n",
        encoding="utf-8",
    )
    flow_service.add_flow(str(path))
    if not enabled:
        database.update_flow_enabled(name, False)
    return path


WEDNESDAY_AFTER_NINE = datetime(2026, 11, 18, 10, 0)  # 2026-11-18 is a Wednesday
TAKEN_TEXT = "a flow with this name already exists"


@pytest.fixture
def wednesday_after_nine(monkeypatch):
    """Pin ``datetime.now`` inside flow_service to Wednesday 2026-11-18, 10:00 local."""

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return WEDNESDAY_AFTER_NINE

    monkeypatch.setattr(flow_service, "datetime", FrozenDatetime)


def register_under_file_name(home, flow_name, file_stem):
    """Register flow ``flow_name`` from ``flows/<file_stem>.flow.md``."""
    path = home / "flows" / f"{file_stem}.flow.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'---\nname: {flow_name}\nschedule: "0 9 * * *"\nagent_profile: developer\n'
        "provider: claude_code\n---\nOriginal.\n",
        encoding="utf-8",
    )
    flow_service.add_flow(str(path))
    return path


def make_due(name):
    """Put a flow's next_run in the past without touching its enabled flag."""
    with database.SessionLocal() as db:
        db.query(database.FlowModel).filter(database.FlowModel.name == name).update(
            {"next_run": datetime(2020, 1, 1, 9, 0)}
        )
        db.commit()


# --------------------------------------------------------------------------- #
# POST /flows/validate
# --------------------------------------------------------------------------- #


class TestValidateRoute:
    def test_valid_definition_lists_three_ascending_weekday_runs_at_nine(self, client, flows_home):
        response = client.post("/flows/validate", json=GOOD)

        assert response.status_code == 200
        body = response.json()
        assert body["valid"] is True
        assert body["errors"] == []
        runs = runs_of(body)
        assert len(runs) == 3
        assert runs == sorted(runs) and len(set(runs)) == 3
        assert all((t.hour, t.minute) == (9, 0) for t in runs)
        assert all(t.weekday() < 5 for t in runs)
        assert not flow_file(flows_home).exists()

    @pytest.mark.parametrize(
        ("schedule", "expected"),
        [
            # the scheduler counts days from Monday as 0, so 1-5 is Tuesday to Saturday
            ("0 9 * * 1-5", [(2026, 11, 19), (2026, 11, 20), (2026, 11, 21)]),
            ("0 9 * * mon-fri", [(2026, 11, 19), (2026, 11, 20), (2026, 11, 23)]),
        ],
    )
    def test_weekday_schedules_are_read_the_way_the_scheduler_reads_them(
        self, client, flows_home, wednesday_after_nine, schedule, expected
    ):
        body = validated(client, {**GOOD, "schedule": schedule})

        assert body["valid"] is True
        assert [t.replace(tzinfo=None) for t in runs_of(body)] == [
            datetime(*day, 9, 0) for day in expected
        ]

    def test_bad_name_and_bad_cron_are_two_field_errors_and_not_a_422(self, client, flows_home):
        response = client.post(
            "/flows/validate", json={**GOOD, "name": "bad/name", "schedule": "61 * * * *"}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["valid"] is False
        assert sorted(e["field"] for e in body["errors"]) == ["name", "schedule"]
        assert all(e["message"] for e in body["errors"])
        assert body["next_runs"] == []

    @pytest.mark.parametrize(
        ("override", "field"),
        [
            ({"name": "../evil"}, "name"),
            ({"name": "foo/bar"}, "name"),
            ({"name": "a\\b"}, "name"),
            ({"name": "a" * 65}, "name"),
            ({"name": "名前"}, "name"),
            ({"name": "good\n"}, "name"),
            ({"schedule": "0 9 * * *\nscript: /tmp/x"}, "schedule"),
            ({"agent_profile": ""}, "agent_profile"),
            ({"agent_profile": "dev\nscript: /tmp/x"}, "agent_profile"),
            ({"provider": ""}, "provider"),
            ({"provider": "a\nb"}, "provider"),
        ],
    )
    def test_a_value_the_create_body_would_refuse_comes_back_as_a_field_error(
        self, client, flows_home, override, field
    ):
        response = client.post("/flows/validate", json={**GOOD, **override})

        assert response.status_code == 200
        body = response.json()
        assert body["valid"] is False
        assert [e["field"] for e in body["errors"]] == [field]

    @pytest.mark.parametrize(
        ("override", "field"),
        [
            ({"schedule": None}, "schedule"),
            ({"name": 123}, "name"),
            ({"name": None}, "name"),
            ({"name": ["a"]}, "name"),
            ({"name": {"a": 1}}, "name"),
            ({"schedule": 12.5}, "schedule"),
            ({"agent_profile": 5}, "agent_profile"),
            ({"provider": ["a"]}, "provider"),
            ({"provider": True}, "provider"),
        ],
    )
    def test_a_value_that_is_not_a_string_reaches_the_validator_as_a_field_error(
        self, client, flows_home, override, field
    ):
        response = client.post("/flows/validate", json={**GOOD, **override})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["valid"] is False
        assert [e["field"] for e in body["errors"]] == [field]
        assert body["errors"][0]["message"]
        assert len(body["next_runs"]) == (0 if field == "schedule" else 3)
        assert files_under(flows_home) == []

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            ({k: v for k, v in GOOD.items() if k != "prompt_template"}, "is required"),
            ({**GOOD, "prompt_template": None}, "is required"),
            ({**GOOD, "prompt_template": 5}, "must be a string"),
            ({**GOOD, "prompt_template": ["a"]}, "must be a string"),
            ({**GOOD, "prompt_template": {"a": 1}}, "must be a string"),
            ({**GOOD, "prompt_template": True}, "must be a string"),
        ],
    )
    def test_a_prompt_template_that_create_refuses_is_a_field_error_here_too(
        self, client, flows_home, body, message
    ):
        answer = validated(client, body)

        assert answer["valid"] is False
        assert answer["errors"] == [{"field": "prompt_template", "message": message}]
        assert len(answer["next_runs"]) == 3
        # Create refuses the same body while parsing it, so validate must not say "valid".
        assert client.post("/flows", json=body).status_code == 422
        assert files_under(flows_home) == []

    def test_an_empty_prompt_template_is_valid_because_create_accepts_it(self, client, flows_home):
        body = {**GOOD, "prompt_template": ""}

        assert validated(client, body)["valid"] is True
        assert client.post("/flows", json=body).status_code == 201

    def test_a_name_at_the_length_limit_is_valid(self, client, flows_home):
        body = validated(client, {**GOOD, "name": "a" * 64})

        assert body["valid"] is True and body["errors"] == []

    def test_a_newline_in_the_provider_is_a_provider_error(self, client, flows_home):
        body = validated(client, {**GOOD, "provider": "a\nb"})

        assert body["valid"] is False
        assert [e["field"] for e in body["errors"]] == ["provider"]
        assert len(body["next_runs"]) == 3

    def test_an_existing_name_is_an_error_but_the_schedule_still_gets_its_runs(
        self, client, flows_home
    ):
        register_flow(flows_home)

        body = validated(client, GOOD)

        assert body["valid"] is False
        assert [e["field"] for e in body["errors"]] == ["name"]
        assert "exist" in body["errors"][0]["message"].lower()
        assert len(body["next_runs"]) == 3

    def test_a_cron_that_never_fires_is_a_schedule_error_with_no_runs(self, client, flows_home):
        body = validated(client, {**GOOD, "schedule": "0 0 30 2 *"})

        assert body["valid"] is False
        assert [e["field"] for e in body["errors"]] == ["schedule"]
        assert body["next_runs"] == []

    def test_an_empty_body_is_field_errors_and_not_a_server_error(self, client, flows_home):
        response = client.post("/flows/validate", json={})

        assert response.status_code == 200
        body = response.json()
        assert body["valid"] is False
        assert {"name", "schedule", "agent_profile"} <= {e["field"] for e in body["errors"]}
        assert body["next_runs"] == []

    def test_provider_defaults_when_it_is_left_out(self, client, flows_home):
        without_provider = {k: v for k, v in GOOD.items() if k != "provider"}

        body = validated(client, without_provider)

        assert body["valid"] is True and body["errors"] == []

    def test_it_writes_no_file_and_no_row(self, client, flows_home):
        register_flow(flows_home, name="other-flow")
        files_before = files_under(flows_home)
        rows_before = database.list_flows()

        assert validated(client, GOOD)["valid"] is True
        assert validated(client, {**GOOD, "name": "bad/name"})["valid"] is False

        assert files_under(flows_home) == files_before
        assert database.list_flows() == rows_before

    def test_it_changes_neither_the_file_nor_the_row_of_an_existing_flow(self, client, flows_home):
        path = register_flow(flows_home)
        raw, row = path.read_bytes(), database.get_flow("daily-report")

        assert validated(client, {**GOOD, "schedule": "30 8 * * *"})["valid"] is False

        assert path.read_bytes() == raw
        assert database.get_flow("daily-report") == row

    def test_it_takes_no_script_field(self, client, flows_home):
        from cli_agent_orchestrator.api import main as api_main

        assert "script" not in api_main.FlowDefinitionInput.model_fields

    def test_a_script_in_the_body_changes_nothing_and_no_path_is_checked(
        self, client, flows_home, monkeypatch
    ):
        marker = "definitely-missing-script-xyz.sh"
        probed = []

        def spy(real):
            def wrapper(*args, **kwargs):
                probed.append(str(args[0]) if args else "")
                return real(*args, **kwargs)

            return wrapper

        plain = validated(client, GOOD)
        for owner, names in (
            (Path, ("exists", "is_file", "is_dir")),
            (os.path, ("exists", "isfile", "isdir")),
        ):
            for attr in names:
                monkeypatch.setattr(owner, attr, spy(getattr(owner, attr)))

        with_script = validated(client, {**GOOD, "script": marker})

        assert plain["valid"] is True
        assert with_script == plain
        assert not [p for p in probed if marker in p or str(flows_home) in p]

    def test_a_one_megabyte_prompt_is_accepted_and_ignored(self, client, flows_home):
        response = client.post(
            "/flows/validate", json={**GOOD, "prompt_template": "x" * (1024 * 1024)}
        )

        assert response.status_code == 200 and response.json()["valid"] is True
        assert files_under(flows_home) == []

    def test_a_huge_garbage_schedule_is_refused_quickly(self, client, flows_home):
        started = time.monotonic()

        body = validated(client, {**GOOD, "schedule": "1-," * 100_000})

        assert time.monotonic() - started < 5
        assert body["valid"] is False
        assert [e["field"] for e in body["errors"]] == ["schedule"]

    def test_a_list_schedule_that_never_fires_is_refused_quickly(self, client, flows_home):
        # 31 February, listed 250 times: it parses, so only the fire-time scan can refuse it.
        never = "0 0 " + ",".join(["31"] * 250) + " 2 *"
        started = time.monotonic()

        body = validated(client, {**GOOD, "schedule": never})

        assert time.monotonic() - started < 1
        assert body["valid"] is False
        assert [e["field"] for e in body["errors"]] == ["schedule"]
        assert body["next_runs"] == []

    def test_a_schedule_at_the_length_cap_is_valid_with_three_runs(self, client, flows_home):
        body = validated(client, {**GOOD, "schedule": schedule_of_length(SCHEDULE_CAP)})

        assert body["valid"] is True
        assert len(body["next_runs"]) == 3

    def test_a_schedule_one_past_the_length_cap_is_too_long(self, client, flows_home):
        body = validated(client, {**GOOD, "schedule": schedule_of_length(SCHEDULE_CAP + 1)})

        assert body["valid"] is False
        assert [e["field"] for e in body["errors"]] == ["schedule"]
        assert "too long" in body["errors"][0]["message"]

    @pytest.mark.parametrize("scope", [auth.SCOPE_READ, auth.SCOPE_WRITE, auth.SCOPE_ADMIN])
    def test_any_one_of_the_three_scopes_is_admitted(self, scoped, flows_home, scope):
        response = scoped([scope]).post("/flows/validate", json=GOOD)

        assert response.status_code == 200
        assert response.json()["valid"] is True

    def test_a_token_with_no_known_scope_is_refused(self, scoped, flows_home):
        assert scoped([]).post("/flows/validate", json=GOOD).status_code == 403

    def test_the_read_token_that_validates_is_refused_on_create(self, scoped, flows_home):
        client = scoped([auth.SCOPE_READ])

        validated = client.post("/flows/validate", json=GOOD)
        created = client.post("/flows", json=GOOD)

        assert validated.status_code == 200
        assert created.status_code == 403
        assert files_under(flows_home) == [] and database.list_flows() == []


# --------------------------------------------------------------------------- #
# POST /flows
# --------------------------------------------------------------------------- #


class TestCreateRoute:
    def test_a_bad_cron_is_a_422_with_field_errors_and_leaves_nothing_behind(
        self, client, flows_home
    ):
        response = client.post("/flows", json={**GOOD, "schedule": "not a cron"})

        assert response.status_code == 422
        errors = response.json()["detail"]["errors"]
        assert [e["field"] for e in errors] == ["schedule"]
        assert errors[0]["message"]
        assert files_under(flows_home) == []
        assert database.list_flows() == []

    @pytest.mark.parametrize(
        "name", ["has space", "a" * 65, "a.b", ".", "名前", "good\n", "ｆｌｏｗ"]
    )
    def test_a_name_the_body_model_lets_through_is_still_refused_before_any_write(
        self, client, flows_home, name
    ):
        response = client.post("/flows", json={**GOOD, "name": name})

        assert response.status_code == 422
        assert [e["field"] for e in response.json()["detail"]["errors"]] == ["name"]
        assert files_under(flows_home) == []
        assert database.list_flows() == []

    def test_every_definition_error_is_reported_together(self, client, flows_home):
        response = client.post(
            "/flows", json={**GOOD, "name": "has space", "schedule": "61 * * * *"}
        )

        assert response.status_code == 422
        errors = response.json()["detail"]["errors"]
        assert sorted(e["field"] for e in errors) == ["name", "schedule"]

    @pytest.mark.parametrize("name", ["../evil", "../../etc/cron", "foo/bar", "a\\b", ".."])
    def test_a_body_that_fails_parsing_keeps_fastapis_shape_and_writes_nothing(
        self, client, flows_home, name
    ):
        response = client.post("/flows", json={**GOOD, "name": name})

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert isinstance(detail, list)
        assert {"loc", "msg", "type"} <= set(detail[0])
        assert files_under(flows_home) == []

    def test_a_missing_field_keeps_fastapis_shape(self, client, flows_home):
        body = {k: v for k, v in GOOD.items() if k != "prompt_template"}

        response = client.post("/flows", json=body)

        assert response.status_code == 422
        assert isinstance(response.json()["detail"], list)
        assert files_under(flows_home) == []

    def test_a_duplicate_name_is_a_409_that_touches_neither_file_nor_row(self, client, flows_home):
        assert client.post("/flows", json=GOOD).status_code == 201
        raw = flow_file(flows_home).read_bytes()
        row = database.get_flow("daily-report")

        response = client.post(
            "/flows", json={**GOOD, "schedule": "30 8 * * *", "prompt_template": "Other."}
        )

        assert response.status_code == 409
        assert flow_file(flows_home).read_bytes() == raw
        assert database.get_flow("daily-report") == row
        assert files_under(flows_home) == ["flows/daily-report.flow.md"]

    def test_a_duplicate_of_a_disabled_flow_is_also_a_409(self, client, flows_home):
        path = register_flow(flows_home, enabled=False)
        raw, row = path.read_bytes(), database.get_flow("daily-report")

        response = client.post("/flows", json={**GOOD, "enabled": True})

        assert response.status_code == 409
        assert path.read_bytes() == raw
        assert database.get_flow("daily-report") == row

    def test_a_duplicate_of_a_flow_registered_elsewhere_creates_no_file(self, client, flows_home):
        elsewhere = register_flow(flows_home, subdir="elsewhere")
        raw, row = elsewhere.read_bytes(), database.get_flow("daily-report")

        response = client.post("/flows", json=GOOD)

        assert response.status_code == 409
        assert not flow_file(flows_home).exists()
        assert elsewhere.read_bytes() == raw
        assert database.get_flow("daily-report") == row

    def test_a_taken_name_with_a_bad_cron_is_a_422_because_the_definition_comes_first(
        self, client, flows_home
    ):
        assert client.post("/flows", json=GOOD).status_code == 201
        raw = flow_file(flows_home).read_bytes()
        row = database.get_flow("daily-report")

        response = client.post("/flows", json={**GOOD, "schedule": "not a cron"})

        assert response.status_code == 422
        assert "schedule" in [e["field"] for e in response.json()["detail"]["errors"]]
        assert flow_file(flows_home).read_bytes() == raw
        assert database.get_flow("daily-report") == row

    def test_a_taken_name_is_a_409_whatever_wording_the_validator_uses(
        self, client, flows_home, monkeypatch
    ):
        path = register_flow(flows_home)
        raw, row = path.read_bytes(), database.get_flow("daily-report")
        monkeypatch.setattr(flow_service, "NAME_TAKEN_MESSAGE", "already registered", raising=False)

        response = client.post("/flows", json=GOOD)

        assert response.status_code == 409
        assert path.read_bytes() == raw
        assert database.get_flow("daily-report") == row

    def test_a_definition_error_is_a_422_even_when_its_text_is_the_taken_message(
        self, client, flows_home, monkeypatch
    ):
        monkeypatch.setattr(
            flow_service,
            "validate_flow_definition",
            lambda fields, *, must_be_new: [{"field": "schedule", "message": TAKEN_TEXT}],
        )

        response = client.post("/flows", json=GOOD)

        assert response.status_code == 422
        assert response.json()["detail"] == {
            "errors": [{"field": "schedule", "message": TAKEN_TEXT}]
        }
        assert files_under(flows_home) == []

    def test_a_value_error_from_registration_is_a_422_and_not_a_404(
        self, client, flows_home, monkeypatch
    ):
        def refuse(*_args, **_kwargs):
            raise ValueError("cannot register this file")

        monkeypatch.setattr(flow_service, "add_flow", refuse)

        response = client.post("/flows", json=GOOD)

        assert response.status_code == 422

    def test_a_name_whose_file_is_another_flows_file_is_a_409_and_changes_nothing(
        self, client, flows_home
    ):
        other = register_under_file_name(flows_home, flow_name="a", file_stem="b")
        raw, rows = other.read_bytes(), database.list_flows()

        response = client.post("/flows", json={**GOOD, "name": "b", "prompt_template": "Mine."})

        assert response.status_code == 409
        assert other.read_bytes() == raw
        assert database.list_flows() == rows
        assert database.get_flow("b") is None
        assert files_under(flows_home) == ["flows/b.flow.md"]

    @pytest.mark.parametrize("link", ["symlink", "hardlink"])
    def test_a_target_that_links_to_another_flows_file_is_a_409_and_the_file_is_untouched(
        self, client, flows_home, link
    ):
        moved = register_flow(flows_home, name="a", subdir="elsewhere")
        target = flow_file(flows_home, "b")
        target.parent.mkdir(parents=True)
        if link == "symlink":
            target.symlink_to(moved)
        else:
            os.link(moved, target)
        raw, rows = moved.read_bytes(), database.list_flows()

        response = client.post("/flows", json={**GOOD, "name": "b", "prompt_template": "Mine."})

        assert response.status_code == 409
        assert moved.read_bytes() == raw
        assert database.list_flows() == rows

    def test_a_stray_unregistered_file_at_the_target_is_overwritten_and_registered(
        self, client, flows_home
    ):
        stray = flow_file(flows_home, "b")
        stray.parent.mkdir(parents=True)
        stray.write_text("not a flow, left over from a failed create")

        response = client.post("/flows", json={**GOOD, "name": "b"})

        assert response.status_code == 201
        assert frontmatter.load(str(stray)).metadata["name"] == "b"
        assert database.get_flow("b") is not None

    def test_a_registered_flow_whose_file_is_gone_does_not_block_another_create(
        self, client, flows_home
    ):
        database.create_flow(
            name="a",
            file_path=str(flows_home / "gone" / "a.flow.md"),
            schedule="0 9 * * *",
            agent_profile="developer",
            provider="claude_code",
            script="",
            next_run=datetime(2030, 1, 1, 9, 0),
        )

        response = client.post("/flows", json={**GOOD, "name": "b"})

        assert response.status_code == 201

    def test_created_disabled_is_stored_listed_and_never_run(self, client, flows_home):
        response = client.post("/flows", json={**GOOD, "enabled": False})

        assert response.status_code == 201
        assert response.json()["enabled"] is False
        listed = {f["name"]: f for f in client.get("/flows").json()}
        assert listed["daily-report"]["enabled"] is False
        assert database.get_flow("daily-report").enabled is False
        make_due("daily-report")
        assert flow_service.get_flows_to_run() == []

    def test_an_enabled_flow_that_is_due_does_run(self, client, flows_home):
        client.post("/flows", json=GOOD)
        make_due("daily-report")

        assert [f.name for f in flow_service.get_flows_to_run()] == ["daily-report"]

    def test_without_enabled_the_flow_is_created_enabled_exactly_as_before(
        self, client, flows_home
    ):
        response = client.post("/flows", json=GOOD)

        assert response.status_code == 201
        body = response.json()
        assert body["enabled"] is True
        assert (body["name"], body["schedule"], body["agent_profile"], body["provider"]) == (
            "daily-report",
            "0 9 * * mon-fri",
            "developer",
            "claude_code",
        )
        post = frontmatter.load(str(flow_file(flows_home)))
        assert set(post.metadata) == {"name", "schedule", "agent_profile", "provider"}
        assert post.content.strip() == "Report."
        assert database.get_flow("daily-report").enabled is True
        assert [f["enabled"] for f in client.get("/flows").json()] == [True]

    def test_an_explicit_true_is_the_same_as_leaving_it_out(self, client, flows_home):
        response = client.post("/flows", json={**GOOD, "enabled": True})

        assert response.status_code == 201 and response.json()["enabled"] is True

    @pytest.mark.parametrize("name", ["true", "null", "no", "123", "1e3", "a" * 64])
    def test_names_yaml_could_misread_are_stored_as_strings_and_can_be_edited(
        self, client, flows_home, name
    ):
        created = client.post("/flows", json={**GOOD, "name": name})

        assert created.status_code == 201
        assert created.json()["name"] == name
        assert frontmatter.load(str(flow_file(flows_home, name))).metadata["name"] == name
        assert client.get(f"/flows/{name}").json()["name"] == name
        assert edited(client, name, {"schedule": "30 8 * * *"})["schedule"] == "30 8 * * *"

    def test_a_prompt_that_looks_like_front_matter_cannot_add_a_script(self, client, flows_home):
        prompt = "---\nscript: /tmp/evil.sh\n---\nHi"

        response = client.post("/flows", json={**GOOD, "prompt_template": prompt})

        assert response.status_code == 201
        assert "script" not in frontmatter.load(str(flow_file(flows_home))).metadata
        assert database.get_flow("daily-report").script == ""

    def test_a_one_megabyte_prompt_is_stored_whole(self, client, flows_home):
        prompt = "x" * (1024 * 1024)

        response = client.post("/flows", json={**GOOD, "prompt_template": prompt})

        assert response.status_code == 201
        assert client.get("/flows/daily-report").json()["prompt_template"] == prompt

    def test_a_write_token_creates_and_a_read_token_does_not(self, scoped, flows_home):
        assert scoped([auth.SCOPE_READ]).post("/flows", json=GOOD).status_code == 403
        assert files_under(flows_home) == []

        assert scoped([auth.SCOPE_WRITE]).post("/flows", json=GOOD).status_code == 201


# --------------------------------------------------------------------------- #
# PUT /flows/{name}
# --------------------------------------------------------------------------- #


class TestUpdateRoute:
    def test_rescheduling_an_enabled_flow_updates_response_file_and_row(self, client, flows_home):
        path = register_flow(flows_home)

        response = client.put("/flows/daily-report", json={"schedule": "30 8 * * *"})

        assert response.status_code == 200
        body = response.json()
        assert body["schedule"] == "30 8 * * *"
        next_run = datetime.fromisoformat(body["next_run"])
        assert (next_run.hour, next_run.minute) == (8, 30)
        assert frontmatter.load(str(path)).metadata["schedule"] == "30 8 * * *"
        row = database.get_flow("daily-report")
        assert row.schedule == "30 8 * * *"
        assert (row.next_run.hour, row.next_run.minute) == (8, 30)
        assert client.get("/flows/daily-report").json()["schedule"] == "30 8 * * *"

    def test_a_flow_removed_while_its_file_is_rewritten_is_a_404(
        self, client, flows_home, monkeypatch
    ):
        register_flow(flows_home)
        write = flow_service._atomic_write_text

        def write_then_lose_the_row(path, text):
            write(path, text)
            database.delete_flow("daily-report")

        monkeypatch.setattr(flow_service, "_atomic_write_text", write_then_lose_the_row)

        response = client.put("/flows/daily-report", json={"schedule": "5 4 * * *"})

        assert response.status_code == 404
        assert "was removed while its file was being rewritten" in response.json()["detail"]
        assert database.get_flow("daily-report") is None

    def test_every_editable_field_can_change_in_one_request(self, client, flows_home):
        path = register_flow(flows_home)

        response = client.put(
            "/flows/daily-report",
            json={
                "schedule": "30 8 * * *",
                "agent_profile": "reviewer",
                "provider": "q_cli",
                "prompt_template": "New prompt.",
            },
        )

        assert response.status_code == 200
        post = frontmatter.load(str(path))
        assert (post.metadata["agent_profile"], post.metadata["provider"]) == ("reviewer", "q_cli")
        assert post.content.strip() == "New prompt."
        row = database.get_flow("daily-report")
        assert (row.agent_profile, row.provider) == ("reviewer", "q_cli")
        assert client.get("/flows/daily-report").json()["prompt_template"] == "New prompt."

    def test_fields_left_out_keep_their_values(self, client, flows_home):
        path = register_flow(flows_home)

        edited(client, "daily-report", {"prompt_template": "Only the prompt."})

        meta = frontmatter.load(str(path)).metadata
        assert (meta["schedule"], meta["agent_profile"], meta["provider"]) == (
            "0 9 * * *",
            "developer",
            "claude_code",
        )

    def test_an_invalid_schedule_is_a_422_with_field_errors_and_changes_nothing(
        self, client, flows_home
    ):
        path = register_flow(flows_home)
        raw, row = path.read_bytes(), database.get_flow("daily-report")

        response = client.put("/flows/daily-report", json={"schedule": "nope"})

        assert response.status_code == 422
        errors = response.json()["detail"]["errors"]
        assert [e["field"] for e in errors] == ["schedule"]
        assert path.read_bytes() == raw
        assert database.get_flow("daily-report") == row

    @pytest.mark.parametrize(
        ("body", "fields"),
        [
            ({"agent_profile": ""}, ["agent_profile"]),
            ({"provider": "a\nb"}, ["provider"]),
            ({"schedule": "0 0 30 2 *"}, ["schedule"]),
            ({"schedule": "nope", "provider": "   "}, ["provider", "schedule"]),
        ],
    )
    def test_an_invalid_merged_definition_names_every_bad_field(
        self, client, flows_home, body, fields
    ):
        path = register_flow(flows_home)
        raw = path.read_bytes()

        response = client.put("/flows/daily-report", json=body)

        assert response.status_code == 422
        assert sorted(e["field"] for e in response.json()["detail"]["errors"]) == fields
        assert path.read_bytes() == raw

    def test_a_body_with_a_wrong_type_is_refused_and_changes_nothing(self, client, flows_home):
        path = register_flow(flows_home)
        raw = path.read_bytes()

        response = client.put("/flows/daily-report", json={"schedule": 123})

        assert response.status_code == 422
        assert path.read_bytes() == raw

    def test_an_unknown_flow_is_a_404_and_writes_nothing(self, client, flows_home):
        response = client.put("/flows/missing", json={"schedule": "30 8 * * *"})

        assert response.status_code == 404
        assert files_under(flows_home) == []

    @pytest.mark.parametrize("subdir", ["flows", "elsewhere"])
    def test_a_registered_flow_whose_file_is_gone_is_a_409_and_is_not_recreated(
        self, client, flows_home, subdir
    ):
        path = register_flow(flows_home, subdir=subdir)
        path.unlink()
        row = database.get_flow("daily-report")

        response = client.put("/flows/daily-report", json={"schedule": "30 8 * * *"})

        assert response.status_code == 409
        assert not path.exists()
        assert database.get_flow("daily-report") == row

    @pytest.mark.parametrize("mode", [0o600, 0o640, 0o644, 0o664, 0o755])
    def test_an_edit_keeps_the_files_permission_bits(self, client, flows_home, mode):
        path = register_flow(flows_home)
        path.chmod(mode)

        edited(client, "daily-report", {"schedule": "30 8 * * *"})

        assert stat.S_IMODE(path.stat().st_mode) == mode

    @pytest.mark.parametrize("name", ["..%2F..%2Fetc%2Fpasswd", "bad%20name", "a%5Cb"])
    def test_a_name_that_is_not_a_flow_name_is_a_404_and_writes_nothing(
        self, client, flows_home, name
    ):
        response = client.put(f"/flows/{name}", json={"schedule": "30 8 * * *"})

        assert response.status_code == 404
        assert files_under(flows_home) == []

    def test_a_flow_file_outside_the_flows_directory_is_a_409_and_is_not_touched(
        self, client, flows_home
    ):
        path = register_flow(flows_home, subdir="elsewhere")
        raw, row = path.read_bytes(), database.get_flow("daily-report")

        response = client.put("/flows/daily-report", json={"schedule": "30 8 * * *"})

        assert response.status_code == 409
        assert path.read_bytes() == raw
        assert database.get_flow("daily-report") == row

    def test_the_script_stays_in_the_file_and_the_row(self, client, flows_home):
        path = register_flow(flows_home, extra="script: gate.py\nengine: v3\nowner: team-a\n")

        response = client.put("/flows/daily-report", json={"schedule": "30 8 * * *"})

        assert response.status_code == 200
        meta = frontmatter.load(str(path)).metadata
        assert (meta["script"], meta["engine"], meta["owner"]) == ("gate.py", "v3", "team-a")
        assert database.get_flow("daily-report").script == "gate.py"
        assert response.json()["script"] == "gate.py"
        assert response.json()["engine"] == "v3"

    def test_a_script_in_the_body_never_changes_the_flows_script(self, client, flows_home):
        path = register_flow(flows_home, extra="script: gate.py\n")

        response = client.put("/flows/daily-report", json={"script": "/tmp/evil.sh"})

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert isinstance(detail, list)
        assert {"loc", "msg", "type"} <= set(detail[0])
        assert frontmatter.load(str(path)).metadata["script"] == "gate.py"
        assert database.get_flow("daily-report").script == "gate.py"

    def test_a_disabled_flow_stays_disabled(self, client, flows_home):
        path = register_flow(flows_home, enabled=False)

        response = client.put("/flows/daily-report", json={"schedule": "30 8 * * *"})

        assert response.status_code == 200
        assert response.json()["enabled"] is False
        assert database.get_flow("daily-report").enabled is False
        assert frontmatter.load(str(path)).metadata["schedule"] == "30 8 * * *"
        make_due("daily-report")
        assert flow_service.get_flows_to_run() == []

    def test_the_same_edit_twice_is_the_same_result(self, client, flows_home):
        path = register_flow(flows_home)
        body = {"schedule": "30 8 * * *", "prompt_template": "Same."}

        first = client.put("/flows/daily-report", json=body)
        raw = path.read_bytes()
        second = client.put("/flows/daily-report", json=body)

        assert (first.status_code, second.status_code) == (200, 200)
        assert path.read_bytes() == raw

    def test_a_flow_named_validate_is_edited_and_the_validator_still_answers(
        self, client, flows_home
    ):
        register_flow(flows_home, name="validate")

        edited = client.put("/flows/validate", json={"schedule": "30 8 * * *"})
        validated = client.post("/flows/validate", json={**GOOD, "name": "validate"})

        assert edited.status_code == 200 and edited.json()["schedule"] == "30 8 * * *"
        assert validated.status_code == 200
        assert [e["field"] for e in validated.json()["errors"]] == ["name"]

    def test_a_prompt_that_looks_like_front_matter_cannot_add_a_script(self, client, flows_home):
        path = register_flow(flows_home)

        edited(client, "daily-report", {"prompt_template": "---\nscript: /tmp/evil.sh\n---\nHi"})

        assert "script" not in frontmatter.load(str(path)).metadata
        assert database.get_flow("daily-report").script == ""

    def test_a_write_token_edits_and_a_read_token_does_not(self, scoped, flows_home):
        path = register_flow(flows_home)
        raw = path.read_bytes()

        refused = scoped([auth.SCOPE_READ]).put(
            "/flows/daily-report", json={"schedule": "5 4 * * *"}
        )
        assert refused.status_code == 403
        assert path.read_bytes() == raw

        assert (
            scoped([auth.SCOPE_WRITE])
            .put("/flows/daily-report", json={"schedule": "5 4 * * *"})
            .status_code
            == 200
        )
        assert (
            scoped([auth.SCOPE_ADMIN])
            .put("/flows/daily-report", json={"schedule": "6 4 * * *"})
            .status_code
            == 200
        )
        assert (
            scoped([]).put("/flows/daily-report", json={"schedule": "7 4 * * *"}).status_code == 403
        )


# --------------------------------------------------------------------------- #
# Exception text stays in the server log
# --------------------------------------------------------------------------- #

LEAK = "boom at /srv/private/path.py line 42"


class TestExceptionTextStaysOutOfResponses:
    def test_validate_gives_the_fixed_cron_message_and_logs_the_schedulers_text(
        self, client, flows_home, caplog
    ):
        with caplog.at_level("INFO", logger=flow_service.logger.name):
            response = client.post("/flows/validate", json={**GOOD, "schedule": "* * * *"})

        assert response.status_code == 200
        assert response.json()["errors"] == [
            {"field": "schedule", "message": flow_service.SCHEDULE_INVALID_MESSAGE}
        ]
        assert "Wrong number of fields" not in response.text
        assert any(
            "Wrong number of fields" in str(r.exc_info[1]) for r in caplog.records if r.exc_info
        )

    def test_validate_does_not_echo_an_exception_the_scheduler_raises(
        self, client, flows_home, monkeypatch
    ):
        def raise_leak(*_args, **_kwargs):
            raise RuntimeError(LEAK)

        monkeypatch.setattr(flow_service, "next_fire_times", raise_leak)

        response = client.post("/flows/validate", json=GOOD)

        assert response.status_code == 200
        assert response.json()["errors"] == [
            {"field": "schedule", "message": flow_service.SCHEDULE_INVALID_MESSAGE}
        ]
        assert LEAK not in response.text

    def test_create_does_not_echo_a_registration_error(
        self, client, flows_home, monkeypatch, caplog
    ):
        def refuse(*_args, **_kwargs):
            raise ValueError(LEAK)

        monkeypatch.setattr(flow_service, "add_flow", refuse)

        with caplog.at_level("WARNING", logger="cli_agent_orchestrator.api.main"):
            response = client.post("/flows", json=GOOD)

        assert response.status_code == 422
        assert LEAK not in response.text
        assert any(r.exc_info and LEAK in str(r.exc_info[1]) for r in caplog.records)

    def test_create_does_not_echo_an_unexpected_error(
        self, client, flows_home, monkeypatch, caplog
    ):
        def fail(*_args, **_kwargs):
            raise RuntimeError(LEAK)

        monkeypatch.setattr(flow_service, "add_flow", fail)

        with caplog.at_level("ERROR", logger="cli_agent_orchestrator.api.main"):
            response = client.post("/flows", json=GOOD)

        assert response.status_code == 500
        assert LEAK not in response.text
        assert any(r.exc_info and LEAK in str(r.exc_info[1]) for r in caplog.records)

    def test_update_does_not_echo_an_unexpected_error(
        self, client, flows_home, monkeypatch, caplog
    ):
        register_flow(flows_home)

        def fail(*_args, **_kwargs):
            raise RuntimeError(LEAK)

        monkeypatch.setattr(flow_service, "update_flow", fail)

        with caplog.at_level("ERROR", logger="cli_agent_orchestrator.api.main"):
            response = client.put("/flows/daily-report", json={"schedule": "5 4 * * *"})

        assert response.status_code == 500
        assert LEAK not in response.text
        assert any(r.exc_info and LEAK in str(r.exc_info[1]) for r in caplog.records)
