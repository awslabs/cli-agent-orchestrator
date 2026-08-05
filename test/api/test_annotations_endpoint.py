"""GET /annotations — the one additive fork seam (work-state design §9.5).

The seam's whole value is that it never needs a second edit, so most of what is
asserted here is an ABSENCE: no caller-reachable path, no kind vocabulary, no
role vocabulary, no schema pin, and no error surface for anything read off
disk. A test suite that only checked the happy path would let any of those be
added back without noticing.

Every test points the reader at a scratch directory by patching
``annotations.annotation_root``. That function is the test seam and not an
operator knob — ``test_the_location_is_not_configurable_from_the_environment``
pins that no environment variable moves it.
"""

import json
import os
import time
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.services import annotations

#: Conductor vocabulary the service may never name. KEPT IDENTICAL, TERM FOR
#: TERM, TO THE RENDERER'S MIRROR in `web/src/test/annotations.test.tsx` — the
#: two guards cover the two halves of one seam and a term in only one of them is
#: a hole in the other. The last nine arrived with the lane/VCS chips; the
#: renderer's list gained them and this one had not, which left the module that
#: actually reads the field off the wire unguarded against exactly the shortcut
#: the guard exists for.
#:
#: ONLY UNAMBIGUOUS TOKENS, for the same reason the mirror says so: matching is
#: a substring test, and `lane` is inside `plane` while `base` is inside
#: `basename`. A term that false-positives must be narrowed, never dropped.
_CONDUCTOR_TERMS = (
    "work-state",
    "work_item",
    "human-gate",
    "route-breaker",
    "parked",
    "in-round",
    "finalized",
    "supervisor",
    "track_id",
    "lane_source",
    "lane_scope",
    "cross-lane",
    "task-prefix",
    "worktree",
    "base_branch",
    "commit_short",
    "vcs",
)


def _item(**overrides):
    """A minimal valid annotation, so each test can perturb exactly one thing."""
    item = {
        "namespace": "cao-conductor",
        "kind": "work-state.waiting",
        "version": 1,
        "label": "waiting",
        "semantic_role": "warning",
        "priority": 60,
        "subject": {
            "type": "terminal",
            "terminal_id": "t-native",
            "generation": "generation-1",
        },
        "valid_until": "2999-01-01T00:00:00Z",
        "details": {"task": "p0-09b", "round": "12"},
    }
    item.update(overrides)
    return item


def _publish(root, project, document):
    directory = os.path.join(root, project)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, annotations.ANNOTATIONS_FILENAME)
    with open(path, "w", encoding="utf-8") as handle:
        if isinstance(document, (bytes, str)):
            handle.write(document if isinstance(document, str) else document.decode())
        else:
            json.dump(document, handle)
    return path


@pytest.fixture
def root(tmp_path):
    """A scratch conductor state root, patched in for the whole request."""
    scratch = tmp_path / "cao-conductor"
    scratch.mkdir()
    with patch.object(annotations, "annotation_root", return_value=str(scratch)):
        yield str(scratch)


class TestFixedLocation:
    """The route reads one place, and nothing a caller says can move it."""

    def test_the_route_accepts_no_parameters_at_all(self):
        """No query, path or body parameter — so none can reach the filesystem.

        Confinement here is a property of the route's SHAPE. A route with a
        ``project`` or ``path`` parameter would have to sanitise; this one has
        nothing to sanitise, which is the property §9.5 asks for.
        """
        route = next(r for r in app.routes if getattr(r, "path", None) == "/annotations")
        caller_supplied = [
            param.name
            for param in route.dependant.query_params
            + route.dependant.path_params
            + route.dependant.header_params
            + route.dependant.cookie_params
        ]
        assert caller_supplied == []
        assert route.dependant.body_params == []

    def test_the_location_is_not_configurable_from_the_environment(self, monkeypatch):
        """No environment variable relocates the conductor's directory.

        ``CAO_STATE_ROOT`` deliberately does not apply: it relocates *CAO's*
        state, and this directory belongs to the conductor, whose own producer
        resolves it with no override.
        """
        for name in (
            "CAO_STATE_ROOT",
            "CAO_ANNOTATIONS_ROOT",
            "CONDUCTOR_STATE_ROOT",
            "XDG_STATE_HOME",
        ):
            monkeypatch.setenv(name, "/tmp/attacker-controlled")
        assert annotations.annotation_root() == os.path.expanduser("~/.local/state/cao-conductor")

    def test_the_one_environment_input_is_the_process_own_home(self, monkeypatch):
        """Stated explicitly rather than by tautology.

        ``expanduser`` does read ``HOME``, so "consults no environment
        variable" was too strong a claim to leave in a docstring unexamined.
        The honest property is narrower and is what the producer relies on: the
        path is resolved relative to the SERVER PROCESS'S OWN home directory,
        exactly as the conductor's sentinel resolves it, and no CAO or
        conductor configuration knob moves it off that.
        """
        monkeypatch.setenv("HOME", "/var/empty/pretend-home")
        assert annotations.annotation_root() == "/var/empty/pretend-home/.local/state/cao-conductor"

    def test_the_reader_takes_no_arguments(self):
        """``read_annotations()`` has no parameter a route could ever forward."""
        import inspect

        assert list(inspect.signature(annotations.read_annotations).parameters) == []


class TestAbsentSource:
    """An unset or unreadable source renders exactly as today."""

    def test_missing_state_root_is_unavailable_not_an_error(self, client, tmp_path):
        with patch.object(annotations, "annotation_root", return_value=str(tmp_path / "nope")):
            response = client.get("/annotations")
        assert response.status_code == 200
        body = response.json()
        assert body["coverage"] == "unavailable"
        assert body["annotations"] == []
        assert body["reasons"] == [{"source": "conductor-state-root", "reason": "missing"}]

    def test_a_project_with_no_document_is_not_a_failure(self, client, root):
        os.makedirs(os.path.join(root, "quiet-campaign"))
        response = client.get("/annotations")
        assert response.status_code == 200
        body = response.json()
        assert body["coverage"] == "complete"
        assert body["annotations"] == []
        assert body["sources_failed"] == 0

    def test_an_unreadable_root_never_500s(self, client, tmp_path):
        """Even a reader that blows up degrades to an empty, typed answer."""
        with patch.object(annotations, "annotation_root", side_effect=RuntimeError("boom")):
            response = client.get("/annotations")
        assert response.status_code == 200
        assert response.json()["coverage"] == "unavailable"
        assert response.json()["annotations"] == []


class TestSymlinkConfinement:
    """Symlink escape is refused explicitly, at both components."""

    def test_a_symlinked_project_directory_is_not_followed(self, client, root, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        with open(outside / annotations.ANNOTATIONS_FILENAME, "w") as handle:
            json.dump({"annotations": [_item(label="LEAKED")]}, handle)
        os.symlink(str(outside), os.path.join(root, "sneaky"))

        response = client.get("/annotations")
        assert response.status_code == 200
        body = response.json()
        assert body["annotations"] == []
        assert "LEAKED" not in response.text
        # Refused, and SAID SO. A directory-shaped entry the operator expects
        # to see, skipped with no reason, reported `complete` coverage over a
        # source that was never read.
        assert {"source": "sneaky", "reason": "symlink-refused"} in body["reasons"]
        assert body["coverage"] == "partial"

    def test_an_ordinary_file_beside_the_projects_is_not_charged_as_a_failure(self, client, root):
        """§9.7: the state root legitimately holds non-project files.

        Charging ``conductor-repo.json`` and friends as failed sources would
        make every healthy fleet permanently ``partial``, and a warning that is
        always on is a warning nobody reads.
        """
        _publish(root, "campaign", {"annotations": [_item()]})
        for name in ("conductor-repo.json", "pending-issues.ndjson", "deploy.json"):
            with open(os.path.join(root, name), "w") as handle:
                handle.write("{}")

        body = client.get("/annotations").json()
        assert body["reasons"] == []
        assert body["coverage"] == "complete"

    def test_a_document_larger_than_one_read_is_not_reported_malformed(self, client, root):
        """A single ``os.read`` can short-read a perfectly valid document.

        Half a JSON object parses as malformed, so a large-but-in-contract
        document would have degraded for a reason that is not true of it.
        """
        items = [_item(label=f"chip-{i}", details={"pad": "p" * 150}) for i in range(200)]
        _publish(root, "campaign", {"annotations": items})
        path = os.path.join(root, "campaign", annotations.ANNOTATIONS_FILENAME)
        assert os.path.getsize(path) > 65536

        body = client.get("/annotations").json()
        assert len(body["annotations"]) == 200
        assert body["reasons"] == []

    def test_a_symlinked_document_is_refused_not_followed(self, client, root, tmp_path):
        target = tmp_path / "elsewhere.json"
        with open(target, "w") as handle:
            json.dump({"annotations": [_item(label="LEAKED")]}, handle)
        project = os.path.join(root, "campaign")
        os.makedirs(project)
        os.symlink(str(target), os.path.join(project, annotations.ANNOTATIONS_FILENAME))

        response = client.get("/annotations")
        assert response.status_code == 200
        body = response.json()
        assert body["annotations"] == []
        # Refused by the resolved-path gate, which runs first. Both gates are
        # load-bearing: this one names the escape, ``O_NOFOLLOW`` closes the
        # check-then-open race for a link that stays inside the root.
        assert body["reasons"] == [{"source": "campaign", "reason": "outside-root"}]
        assert "LEAKED" not in response.text

    def test_a_relative_traversal_symlink_is_refused(self, client, root, tmp_path):
        """``../../`` inside the root is the same refusal as an absolute one."""
        secret = tmp_path / "secret.json"
        with open(secret, "w") as handle:
            json.dump({"annotations": [_item(label="LEAKED")]}, handle)
        project = os.path.join(root, "campaign")
        os.makedirs(project)
        os.symlink(
            os.path.join("..", "..", "secret.json"),
            os.path.join(project, annotations.ANNOTATIONS_FILENAME),
        )

        body = client.get("/annotations").json()
        assert body["annotations"] == []
        assert body["reasons"][0]["reason"] == "outside-root"

    def test_a_symlink_that_stays_inside_the_root_is_still_refused(self, client, root):
        """``O_NOFOLLOW``'s own case: an in-root link passes the path gate.

        Confinement alone would admit this file, and admitting it would mean
        one campaign could publish another campaign's chips. The open refuses
        it atomically instead.
        """
        _publish(root, "real", {"annotations": [_item(label="borrowed")]})
        project = os.path.join(root, "campaign")
        os.makedirs(project)
        os.symlink(
            os.path.join(root, "real", annotations.ANNOTATIONS_FILENAME),
            os.path.join(project, annotations.ANNOTATIONS_FILENAME),
        )

        body = client.get("/annotations").json()
        assert [a["source"] for a in body["annotations"]] == ["real"]
        assert {"source": "campaign", "reason": "symlink-refused"} in body["reasons"]

    def test_a_directory_named_like_the_document_is_not_read(self, client, root):
        os.makedirs(os.path.join(root, "campaign", annotations.ANNOTATIONS_FILENAME))
        body = client.get("/annotations").json()
        assert body["annotations"] == []
        assert body["reasons"] == [{"source": "campaign", "reason": "not-a-regular-file"}]


class TestMalformedInput:
    """Never 500; degrade to an empty or partial list with a typed reason."""

    @pytest.mark.parametrize(
        "document",
        [
            "{not json at all",
            "[]",
            '{"annotations": {}}',
            '{"annotations": null}',
            '"a bare string"',
        ],
    )
    def test_a_malformed_document_degrades_with_a_reason(self, client, root, document):
        _publish(root, "campaign", document)
        response = client.get("/annotations")
        assert response.status_code == 200
        body = response.json()
        assert body["annotations"] == []
        assert body["coverage"] == "partial"
        assert body["reasons"] == [{"source": "campaign", "reason": "malformed"}]

    def test_one_malformed_project_does_not_erase_a_healthy_one(self, client, root):
        _publish(root, "broken", "{{{")
        _publish(root, "healthy", {"annotations": [_item(label="alive")]})
        body = client.get("/annotations").json()
        assert [a["label"] for a in body["annotations"]] == ["alive"]
        assert body["coverage"] == "partial"
        assert body["sources_read"] == 1
        assert body["sources_failed"] == 1

    @pytest.mark.parametrize(
        "bad",
        [
            {"namespace": ""},
            {"kind": None},
            {"label": ""},
            {"semantic_role": 7},
            {"subject": None},
            {"subject": {"terminal_id": "t"}},
        ],
    )
    def test_an_unrepresentable_item_is_dropped_and_counted(self, client, root, bad):
        _publish(root, "campaign", {"annotations": [_item(**bad), _item(label="ok")]})
        body = client.get("/annotations").json()
        assert [a["label"] for a in body["annotations"]] == ["ok"]
        assert body["items_dropped"] == 1
        assert body["coverage"] == "partial"

    @pytest.mark.parametrize("bad", ["one", 0, -3, None, {"a": 1}])
    def test_a_bookkeeping_version_never_costs_the_item(self, client, root, bad):
        """``version`` has no reader in the fork, so it cannot justify a drop.

        Dropping a renderable chip because a field nothing consumes was a
        string is the fork asserting an opinion it does not need to have.
        """
        _publish(root, "campaign", {"annotations": [_item(label="kept", version=bad)]})
        body = client.get("/annotations").json()
        assert [a["label"] for a in body["annotations"]] == ["kept"]
        assert body["annotations"][0]["version"] == 1
        assert body["items_dropped"] == 0

    def test_a_numeric_string_version_is_carried_as_the_number(self, client, root):
        _publish(root, "campaign", {"annotations": [_item(version="7")]})
        assert client.get("/annotations").json()["annotations"][0]["version"] == 7

    def test_a_deeply_nested_document_does_not_erase_a_healthy_sibling(self, client, root):
        """The failure mode that made ONE bad producer look like NO conductor.

        ``json.loads`` raises ``RecursionError`` — a ``RuntimeError``, not a
        ``ValueError`` — on a deeply-nested document. That escaped the
        per-source handler, unwound the whole fan-out, and was caught by the
        blanket handler, which answered ``coverage: unavailable`` with reason
        ``conductor-state-root: unreadable``: the operator was told there is no
        conductor state root while it sat there, perfectly readable, with 22
        healthy producers in it.
        """
        _publish(root, "aaa-healthy", {"annotations": [_item(label="HEALTHY")]})
        _publish(root, "zzz-bad", '{"annotations":[' + "[" * 100_000 + "]" * 100_000 + "]}")

        body = client.get("/annotations").json()
        assert [a["label"] for a in body["annotations"]] == ["HEALTHY"]
        assert body["coverage"] == "partial"
        assert body["sources_read"] == 1
        assert {"source": "zzz-bad", "reason": "malformed"} in body["reasons"]
        # The root itself is never blamed for one producer's document.
        assert all(r["source"] != "conductor-state-root" for r in body["reasons"])

    def test_a_named_pipe_is_refused_in_bounded_time(self, client, root, tmp_path):
        """``open(O_RDONLY)`` on a FIFO blocks until a writer appears — forever.

        ``O_NOFOLLOW`` does not help, because a FIFO is not a symlink, so the
        ``S_ISREG`` check could never run for the one non-regular file type
        that actually hurts. The route is off-loaded with ``asyncio.to_thread``
        onto the loop's DEFAULT executor, shared with every other blocking call
        in the API, and the dashboard polls every 5s — one stale FIFO would
        park the whole pool and stop the server running any blocking work.
        """
        import threading

        _publish(root, "aaa-healthy", {"annotations": [_item(label="HEALTHY")]})
        pipe_dir = os.path.join(root, "zzz-pipe")
        os.makedirs(pipe_dir)
        os.mkfifo(os.path.join(pipe_dir, annotations.ANNOTATIONS_FILENAME))

        result = {}
        worker = threading.Thread(
            target=lambda: result.update(body=annotations.read_annotations()), daemon=True
        )
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive(), "read_annotations blocked on a FIFO"

        body = result["body"]
        assert [a["label"] for a in body["annotations"]] == ["HEALTHY"]
        assert {"source": "zzz-pipe", "reason": "not-a-regular-file"} in body["reasons"]

    def test_a_reason_never_echoes_a_path_or_a_payload(self, client, root):
        _publish(root, "campaign", '{"annotations": [ SECRET-PAYLOAD-BYTES')
        text = client.get("/annotations").text
        assert "SECRET-PAYLOAD-BYTES" not in text
        assert root not in text
        assert "/" not in json.dumps(client.get("/annotations").json()["reasons"])


class TestUnknownKindsAreIgnored:
    """The property that makes this the LAST fork change."""

    def test_a_kind_the_fork_has_never_seen_is_served_unchanged(self, client, root):
        """Ignored means "not interpreted", not "not delivered".

        Nothing in the fork branches on ``kind``, so a kind invented years from
        now arrives with no release here. Dropping it instead would make every
        new kind a fork change, which is the exact outcome §9.5 forbids.
        """
        _publish(
            root,
            "campaign",
            {"annotations": [_item(kind="quantum-lease-reconciliation-2031")]},
        )
        body = client.get("/annotations").json()
        assert body["annotations"][0]["kind"] == "quantum-lease-reconciliation-2031"
        assert body["coverage"] == "complete"
        assert body["items_dropped"] == 0

    def test_an_unknown_semantic_role_is_carried_not_rejected(self, client, root):
        _publish(root, "campaign", {"annotations": [_item(semantic_role="chartreuse")]})
        body = client.get("/annotations").json()
        assert body["annotations"][0]["semantic_role"] == "chartreuse"
        assert body["items_dropped"] == 0

    def test_an_unknown_subject_type_is_carried_not_rejected(self, client, root):
        _publish(
            root,
            "campaign",
            {"annotations": [_item(subject={"type": "fleet", "campaign": "aegix"})]},
        )
        body = client.get("/annotations").json()
        assert body["annotations"][0]["subject"]["type"] == "fleet"

    def test_the_service_holds_no_kind_or_role_vocabulary(self):
        """A structural guard: no constant here may enumerate conductor terms.

        This is the regression that would quietly reintroduce the coupling —
        one well-meant ``KNOWN_KINDS`` and every future kind needs a fork
        release again.
        """
        import ast

        source = open(annotations.__file__, encoding="utf-8").read()
        tree = ast.parse(source)
        # Docstrings are exempt (they explain the design); every other string
        # literal is not, because a vocabulary would live in one of those.
        docstring_ids = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if not isinstance(body, list) or not body:
                continue
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                if isinstance(first.value.value, str):
                    docstring_ids.add(id(first.value))
        literals = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstring_ids
        ]
        conductor_terms = _CONDUCTOR_TERMS
        offenders = [text for text in literals if any(term in text for term in conductor_terms)]
        assert offenders == []
        for forbidden in ("KNOWN_KINDS", "SUPPORTED_KINDS", "SEMANTIC_ROLES", "SUBJECT_TYPES"):
            assert forbidden not in source

    def test_the_guard_is_not_vacuous_and_covers_this_features_vocabulary(self):
        """The non-vacuity control, and the widening's own control.

        THE TYPESCRIPT GUARD NEXT DOOR WAS WIDENED AND THIS ONE WAS NOT. The
        renderer's mirror gained the nine terms the lane/VCS producer speaks;
        the Python guard — over the module that actually READS the field off the
        wire — kept the original eight, so `if item["kind"] == "vcs"` or a
        `LANE_SCOPES` table in the service would have passed here. Both sides of
        the seam are now guarded by the same list.
        """
        for term in (
            "track_id",
            "lane_source",
            "lane_scope",
            "cross-lane",
            "task-prefix",
            "worktree",
            "base_branch",
            "commit_short",
            "vcs",
        ):
            assert term in _CONDUCTOR_TERMS
        # It must be able to FAIL: a term planted in a literal is caught.
        planted = ["annotations", "lane_scope", "coverage"]
        assert [t for t in planted if any(term in t for term in _CONDUCTOR_TERMS)] == ["lane_scope"]
        # And the field this feature added is not itself a vocabulary term.
        assert not any(term in "colour_key" for term in _CONDUCTOR_TERMS)

    def test_an_unrecognised_document_schema_is_not_a_gate(self, client, root):
        """A future document version still yields its v1-valid items."""
        _publish(
            root,
            "campaign",
            {
                "schema": "cao-annotations-v9",
                "future_top_level": {"x": 1},
                "annotations": [_item(label="still here")],
            },
        )
        body = client.get("/annotations").json()
        assert [a["label"] for a in body["annotations"]] == ["still here"]
        assert body["coverage"] == "complete"


class TestSubjects:
    """Terminal, task and campaign subjects all survive the round trip."""

    def test_a_campaign_subject_is_served(self, client, root):
        _publish(
            root,
            "campaign",
            {
                "annotations": [
                    _item(
                        kind="gate.pending",
                        label="gate pending",
                        subject={"type": "campaign", "campaign": "aegix-mobile"},
                    )
                ]
            },
        )
        subject = client.get("/annotations").json()["annotations"][0]["subject"]
        assert subject["type"] == "campaign"
        assert subject["campaign"] == "aegix-mobile"
        assert subject["terminal_id"] is None

    def test_a_task_subject_keeps_its_task_id(self, client, root):
        _publish(
            root,
            "campaign",
            {"annotations": [_item(subject={"type": "task", "task_id": "p0-09b-r1"})]},
        )
        subject = client.get("/annotations").json()["annotations"][0]["subject"]
        assert subject["type"] == "task"
        assert subject["task_id"] == "p0-09b-r1"

    def test_a_terminal_subject_carries_its_generation(self, client, root):
        _publish(root, "campaign", {"annotations": [_item()]})
        subject = client.get("/annotations").json()["annotations"][0]["subject"]
        assert subject["terminal_id"] == "t-native"
        assert subject["generation"] == "generation-1"


class TestBounds:
    """Bounded output; truncation is always explicit."""

    def test_an_oversized_document_is_refused_with_a_marker(self, client, root):
        payload = {"annotations": [_item(label=f"chip-{i}") for i in range(20000)]}
        _publish(root, "campaign", payload)
        assert (
            os.path.getsize(os.path.join(root, "campaign", annotations.ANNOTATIONS_FILENAME))
            > annotations.MAX_SOURCE_BYTES
        )
        body = client.get("/annotations").json()
        assert body["annotations"] == []
        assert body["reasons"] == [{"source": "campaign", "reason": "oversize"}]
        assert body["coverage"] == "partial"

    def test_too_many_items_truncate_visibly(self, client, root):
        count = annotations.MAX_ITEMS + 25
        _publish(
            root,
            "campaign",
            {"annotations": [_item(label=f"c{i}", details={}) for i in range(count)]},
        )
        body = client.get("/annotations").json()
        assert len(body["annotations"]) == annotations.MAX_ITEMS
        assert body["items_omitted"] == 25
        assert body["coverage"] == "truncated"

    def test_the_highest_priority_items_survive_the_cap(self, client, root):
        items = [_item(label=f"low{i}", priority=1) for i in range(annotations.MAX_ITEMS)]
        items.append(_item(label="urgent", priority=99))
        _publish(root, "campaign", {"annotations": items})
        body = client.get("/annotations").json()
        assert body["annotations"][0]["label"] == "urgent"
        assert body["items_omitted"] == 1

    def test_too_many_project_directories_truncate_visibly(self, client, root):
        for i in range(annotations.MAX_SOURCES + 3):
            _publish(root, f"p{i:03d}", {"annotations": [_item(label=f"c{i}")]})
        body = client.get("/annotations").json()
        assert body["sources_read"] == annotations.MAX_SOURCES
        assert body["coverage"] == "truncated"
        assert {"source": "conductor-state-root", "reason": "source-limit"} in body["reasons"]

    def test_a_long_label_is_ellipsised_not_dropped(self, client, root):
        _publish(root, "campaign", {"annotations": [_item(label="x" * 500)]})
        label = client.get("/annotations").json()["annotations"][0]["label"]
        assert len(label) == annotations.MAX_LABEL
        assert label.endswith("…")

    def test_details_are_bounded_in_both_dimensions(self, client, root):
        details = {f"k{i}": "v" * 500 for i in range(40)}
        _publish(root, "campaign", {"annotations": [_item(details=details)]})
        served = client.get("/annotations").json()["annotations"][0]["details"]
        assert len(served) <= annotations.MAX_DETAIL_KEYS
        assert all(len(v) <= annotations.MAX_DETAIL_VALUE for v in served.values())

    def test_a_facet_past_the_cap_is_counted_and_reported_never_silent(self, client, root):
        """The detail bag is the seam's only growth path, so its cap must show.

        ``details`` is the ONE channel the conductor can widen without a fork
        change — every top-level item field is an explicit key in the reader —
        and it was the one bound that truncated with no counter, no reason and
        ``coverage: complete``. A chip that looks whole while a facet the
        operator needed was thrown away is the failure §9.7 names.
        """
        details = {f"facet_{i}": str(i) for i in range(annotations.MAX_DETAIL_KEYS + 8)}
        _publish(root, "campaign", {"annotations": [_item(details=details)]})
        body = client.get("/annotations").json()
        assert len(body["annotations"][0]["details"]) == annotations.MAX_DETAIL_KEYS
        assert body["facets_dropped"] == 8
        assert {"source": "campaign", "reason": "detail-truncated"} in body["reasons"]
        assert body["coverage"] == "truncated"

    def test_nested_detail_values_are_dropped_not_flattened_but_are_counted(self, client, root):
        _publish(
            root,
            "campaign",
            {"annotations": [_item(details={"nested": {"a": 1}, "flat": "ok"})]},
        )
        body = client.get("/annotations").json()
        assert body["annotations"][0]["details"] == {"flat": "ok"}
        assert body["facets_dropped"] == 1
        assert {"source": "campaign", "reason": "detail-truncated"} in body["reasons"]

    def test_a_list_facet_survives_as_a_joined_string(self, client, root):
        """A list is the natural shape for a facet like a dependency set.

        Dropping it left the conductor's most obvious representation vanishing
        with zero signal, so a chip that only worked because the producer
        happened to pre-join the string would break the day it stopped.
        """
        _publish(
            root,
            "campaign",
            {"annotations": [_item(details={"needs": ["gate p0-09b", "pr17"], "n": [1, 2]})]},
        )
        body = client.get("/annotations").json()
        assert body["annotations"][0]["details"]["needs"] == "gate p0-09b, pr17"
        assert body["annotations"][0]["details"]["n"] == "1, 2"
        assert body["facets_dropped"] == 0

    def test_an_over_long_detail_value_is_ellipsised_like_a_label(self, client, root):
        _publish(root, "campaign", {"annotations": [_item(details={"k": "v" * 500})]})
        served = client.get("/annotations").json()["annotations"][0]["details"]["k"]
        assert len(served) == annotations.MAX_DETAIL_VALUE
        assert served.endswith("…")

    def test_a_subject_identifier_the_fork_has_never_heard_of_still_arrives(self, client, root):
        """Placement is durable by construction; identity has to be carried.

        A fixed whitelist meant a subject type invented later reached the
        renderer with no identifier at all, and "something is wrong somewhere"
        is not an operator action on a 15-worker fleet.
        """
        _publish(
            root,
            "campaign",
            {
                "annotations": [
                    _item(
                        subject={
                            "type": "workstream",
                            "workstream_id": "ws-a3",
                            "lane_id": "lane-7",
                        }
                    )
                ]
            },
        )
        subject = client.get("/annotations").json()["annotations"][0]["subject"]
        assert subject["type"] == "workstream"
        assert subject["workstream_id"] == "ws-a3"
        assert subject["lane_id"] == "lane-7"

    def test_extra_subject_keys_are_bounded_like_every_other_passthrough(self, client, root):
        subject = {"type": "workstream"}
        subject.update({f"id_{i}": "x" * 500 for i in range(40)})
        _publish(root, "campaign", {"annotations": [_item(subject=subject)]})
        served = client.get("/annotations").json()["annotations"][0]["subject"]
        extra = {k: v for k, v in served.items() if k.startswith("id_")}
        assert len(extra) == annotations.MAX_SUBJECT_KEYS
        assert all(len(v) <= 200 for v in extra.values())

    def test_priority_is_clamped_never_rejected(self, client, root):
        _publish(
            root,
            "campaign",
            {
                "annotations": [
                    _item(label="huge", priority=10**9),
                    _item(label="negative", priority=-5),
                ]
            },
        )
        by_label = {a["label"]: a for a in client.get("/annotations").json()["annotations"]}
        assert by_label["huge"]["priority"] == annotations.MAX_PRIORITY
        assert by_label["negative"]["priority"] == 0


class TestFreshness:
    """``valid_until`` reaches the renderer per item, not per response."""

    def test_a_document_default_valid_until_is_denormalised_onto_items(self, client, root):
        item = _item()
        del item["valid_until"]
        _publish(
            root,
            "campaign",
            {"valid_until": "2026-08-02T07:00:00Z", "annotations": [item]},
        )
        served = client.get("/annotations").json()["annotations"][0]
        assert served["valid_until"] == "2026-08-02T07:00:00Z"

    def test_a_document_that_declares_no_expiry_still_gets_a_derived_one(self, client, root):
        """The one field that governs "is this current?" cannot be unbounded.

        Every other field is bounded or defaulted; ``valid_until`` was
        pass-through only. A producer version that omits it, or a conductor
        process that dies leaving the file on disk, meant the fork served an
        amber "waiting" or a red "blocked" in full vivid colour forever,
        indistinguishable from one validated a second ago. Deriving a floor
        from the document's own mtime is the fork answering "I read this, and
        here is how long I am willing to vouch for it."
        """
        item = _item()
        del item["valid_until"]
        path = _publish(root, "campaign", {"annotations": [item]})
        os.utime(path, (1_000_000, 1_000_000))

        served = client.get("/annotations").json()["annotations"][0]
        assert served["valid_until"] is not None
        expected = annotations._derived_expiry(1_000_000)
        assert served["valid_until"] == expected
        # A floor, not an override: a declared expiry still wins.
        assert annotations._is_expired(served["valid_until"], time.time()) is True

    def test_a_declared_expiry_always_beats_the_derived_floor(self, client, root):
        item = _item()
        del item["valid_until"]
        path = _publish(root, "campaign", {"annotations": [item]})
        os.utime(path, (1_000_000, 1_000_000))
        _publish(
            root,
            "campaign",
            {"valid_until": "2999-01-01T00:00:00Z", "annotations": [item]},
        )
        os.utime(path, (1_000_000, 1_000_000))
        served = client.get("/annotations").json()["annotations"][0]
        assert served["valid_until"] == "2999-01-01T00:00:00Z"

    def test_a_live_item_outranks_an_expired_one_at_the_item_cap(self, client, root):
        """Freshness is the primary sort key, not a draw-time afterthought.

        Sorting on priority alone let a p99 claim that expired last week
        consume a served slot ahead of a live p10 ``danger``: at the cap the
        thing still happening is the thing discarded.
        """
        with patch.object(annotations, "MAX_ITEMS", 1):
            _publish(
                root,
                "campaign",
                {
                    "annotations": [
                        _item(label="expired", priority=99, valid_until="2020-01-01T00:00:00Z"),
                        _item(label="live", priority=10, valid_until="2999-01-01T00:00:00Z"),
                    ]
                },
            )
            body = client.get("/annotations").json()
        assert [a["label"] for a in body["annotations"]] == ["live"]
        assert body["items_omitted"] == 1

    def test_an_item_may_override_its_documents_valid_until(self, client, root):
        _publish(
            root,
            "campaign",
            {
                "valid_until": "2026-08-02T07:00:00Z",
                "annotations": [_item(valid_until="2026-08-02T09:00:00Z")],
            },
        )
        served = client.get("/annotations").json()["annotations"][0]
        assert served["valid_until"] == "2026-08-02T09:00:00Z"

    def test_two_producers_keep_their_own_expiries(self, client, root):
        """One response merges several envelopes; a stalled campaign must not
        grey a healthy one."""
        _publish(
            root,
            "alpha",
            {
                "valid_until": "2020-01-01T00:00:00Z",
                "annotations": [_item(label="stale", valid_until=None)],
            },
        )
        _publish(
            root,
            "beta",
            {
                "valid_until": "2999-01-01T00:00:00Z",
                "annotations": [_item(label="fresh", valid_until=None)],
            },
        )
        by_label = {a["label"]: a for a in client.get("/annotations").json()["annotations"]}
        assert by_label["stale"]["valid_until"] == "2020-01-01T00:00:00Z"
        assert by_label["fresh"]["valid_until"] == "2999-01-01T00:00:00Z"


class TestReadScope:
    """Read-scope protected, and default-off exactly like every other route."""

    def test_the_route_declares_a_scope_dependency(self):
        route = next(r for r in app.routes if getattr(r, "path", None) == "/annotations")
        stack = list(route.dependant.dependencies)
        names = []
        while stack:
            dep = stack.pop()
            call = getattr(dep, "call", None)
            if call is not None:
                names.append(getattr(call, "__qualname__", ""))
            stack.extend(dep.dependencies)
        assert any("require_any_scope" in name for name in names)

    def test_it_is_readable_with_no_auth_configured(self, client, root):
        _publish(root, "campaign", {"annotations": [_item()]})
        assert client.get("/annotations").status_code == 200


class TestOpaqueIdentityToken:
    """``colour_key`` — an opaque carrier with THREE states, not two.

    The producer distinguishes "this is not an identity chip" (field absent)
    from "this is an identity chip with no colour" (empty string), and the
    renderer draws those two differently. The route is the only thing between
    them, so the empty string surviving it is the whole contract.
    """

    def test_an_empty_token_survives_as_an_empty_token(self, client, root):
        """The state a ``_bounded_required``-style read would delete.

        Reading this field the way every other bounded string is read collapses
        ``""`` to ``None``, which is indistinguishable on the wire from a chip
        that never carried the field — and the renderer would then draw an
        uncoloured identity chip as a severity chip in a role colour.
        """
        _publish(root, "campaign", {"annotations": [_item(colour_key="")]})
        body = client.get("/annotations").json()
        assert body["annotations"][0]["colour_key"] == ""
        assert body["items_dropped"] == 0

    def test_a_token_is_carried_verbatim(self, client, root):
        _publish(root, "campaign", {"annotations": [_item(colour_key="9f2c41a7be03d5e8")]})
        body = client.get("/annotations").json()
        assert body["annotations"][0]["colour_key"] == "9f2c41a7be03d5e8"

    def test_an_absent_token_is_null_and_not_an_empty_string(self, client, root):
        """The third state, and the one every pre-existing document is in."""
        _publish(root, "campaign", {"annotations": [_item()]})
        body = client.get("/annotations").json()
        assert body["annotations"][0]["colour_key"] is None

    def test_a_token_that_is_not_a_string_is_refused_rather_than_stringified(self, client, root):
        """A number is not an opaque token, and coercing one would invent a colour."""
        for value in (12, True, None, {"a": 1}, ["x"]):
            _publish(root, "campaign", {"annotations": [_item(colour_key=value)]})
            body = client.get("/annotations").json()
            assert body["annotations"][0]["colour_key"] is None, value
            assert body["items_dropped"] == 0, value

    def test_an_oversized_token_is_bounded_rather_than_dropped(self, client, root):
        """Bounded like every other string the fork carries.

        Truncation changes which colour the token resolves to, which is
        acceptable and cheap: a chip in the wrong bucket still draws its label,
        and colour is a scan filter rather than an identifier. Dropping the
        field instead would silently demote an identity chip to a severity one.
        """
        _publish(root, "campaign", {"annotations": [_item(colour_key="k" * 100)]})
        body = client.get("/annotations").json()
        assert body["annotations"][0]["colour_key"] == "k" * 64

    def test_the_token_is_not_smuggled_through_the_facet_bag(self, client, root):
        """It is a TOP-LEVEL field, and that placement is load-bearing.

        The facet bag drops empty values by design, so the uncoloured state
        could not cross it at all; and every facet is rendered as visible text,
        so a token carried there would draw a hash string on the chip, in its
        accessible name, and on the phone's facet line.
        """
        _publish(
            root,
            "campaign",
            {"annotations": [_item(colour_key="9f2c41a7be03d5e8", details={"task": "t"})]},
        )
        body = client.get("/annotations").json()
        item = body["annotations"][0]
        assert item["details"] == {"task": "t"}
        assert "colour_key" not in item["details"]

    def test_carrying_it_added_no_vocabulary_to_the_service(self):
        """The field is read BY NAME, and the name is not itself a vocabulary.

        ``test_the_service_holds_no_kind_or_role_vocabulary`` already scans the
        whole module for conductor terms and runs in this same suite; what this
        adds is the two things that test cannot know — that the field is read at
        all, and that reading it did not require a TABLE of values. A palette,
        a scope list or a class list on this side would move a decision the
        producer owns across the seam, and each has a plausible name.
        """
        source = open(annotations.__file__, encoding="utf-8").read()
        assert '"colour_key"' in source
        # The name introduced here must not itself trip the guard next door.
        assert not any(term in "colour_key" for term in _CONDUCTOR_TERMS)
        for forbidden in ("LANE_SCOPES", "PROVENANCE_CLASSES", "COLOUR_KEYS", "IDENTITY_PALETTE"):
            assert forbidden not in source
