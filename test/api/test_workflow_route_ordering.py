"""Route-resolution pins for the /workflows/runs/... read surface (FR-6.5, BR-5).

U3 (issue #504) registers two run-scoped read routes — ``GET
/workflows/runs/{run_id}`` (enriched inspect) and ``GET
/workflows/runs/{run_id}/events`` (batch event-timeline replay). Both are
2-segment paths under ``/workflows/runs/...`` and MUST resolve to their own
handlers, NEVER to the single-segment ``GET /workflows/{name}`` catch-all
(api/main.py) that would otherwise treat ``runs`` as a workflow name.

These tests exercise the LIVE FastAPI/Starlette router the app actually mounts
(``app.router.routes``), matching the way the server dispatches a request. They
FAIL if a future reorder moves the ``/workflows/{name}`` catch-all ahead of a
``/workflows/runs`` route (shadowing it) — the exact regression FR-6.5 guards.
The set starts with U3's routes and expands to U4/U6/U7 routes as they land.
"""

from __future__ import annotations

import pytest
from starlette.routing import Match

from cli_agent_orchestrator.api.main import app


def _resolve(method: str, path: str):
    """Return the FIRST fully-matching mounted route for (method, path).

    Mirrors Starlette's own dispatch: routes are checked in registration order
    and the first ``Match.FULL`` wins. Returning that route lets a test assert
    WHICH handler a path lands on — the whole point of a route-ordering pin.
    """
    scope = {"type": "http", "method": method, "path": path, "headers": []}
    for route in app.router.routes:
        try:
            match, _ = route.matches(scope)
        except Exception:
            continue
        if match == Match.FULL:
            return route
    return None


def test_inspect_route_resolves_to_run_endpoint_not_catch_all():
    route = _resolve("GET", "/workflows/runs/run-abc123")
    assert route is not None
    assert route.name == "get_workflow_run_endpoint"
    # The path template is the run-scoped one, NOT the /workflows/{name} catch-all.
    assert route.path == "/workflows/runs/{run_id}"


def test_events_route_resolves_to_events_endpoint_not_catch_all():
    route = _resolve("GET", "/workflows/runs/run-abc123/events")
    assert route is not None
    assert route.name == "get_workflow_run_events_endpoint"
    assert route.path == "/workflows/runs/{run_id}/events"


def test_compare_route_resolves_to_compare_endpoint_not_catch_all():
    # U6 (issue #504, FR-8): GET /workflows/runs/{run_id}/compare must land on the
    # run-comparison handler, NEVER the /workflows/{name} catch-all that would
    # otherwise treat "runs" as a workflow name.
    route = _resolve("GET", "/workflows/runs/run-abc123/compare")
    assert route is not None
    assert route.name == "compare_workflow_runs_endpoint"
    assert route.path == "/workflows/runs/{run_id}/compare"


def test_diagnostics_route_resolves_to_diagnostics_endpoint_not_catch_all():
    # U6 (issue #504, FR-9): GET /workflows/runs/{run_id}/diagnostics must land on
    # the diagnostic-bundle handler, NEVER the /workflows/{name} catch-all.
    route = _resolve("GET", "/workflows/runs/run-abc123/diagnostics")
    assert route is not None
    assert route.name == "get_workflow_run_diagnostics_endpoint"
    assert route.path == "/workflows/runs/{run_id}/diagnostics"


def test_delete_run_route_resolves_to_delete_endpoint_not_catch_all():
    # U7 (issue #504, FR-11): DELETE /workflows/runs/{run_id} must land on the
    # per-run delete handler, NEVER the DELETE /workflows/{name} spec-delete
    # catch-all that would otherwise treat "runs" as a workflow name.
    route = _resolve("DELETE", "/workflows/runs/run-abc123")
    assert route is not None
    assert route.name == "delete_workflow_run_endpoint"
    assert route.path == "/workflows/runs/{run_id}"


def test_delete_run_route_is_distinct_from_delete_workflow_name():
    # The DELETE spec catch-all remains reachable for a genuine single-segment
    # workflow name, and the two DELETE routes never collapse onto one handler.
    run_delete = _resolve("DELETE", "/workflows/runs/run-abc123")
    name_delete = _resolve("DELETE", "/workflows/my-workflow")
    assert run_delete is not None and name_delete is not None
    assert run_delete.name == "delete_workflow_run_endpoint"
    assert name_delete.name == "delete_workflow_endpoint"
    assert run_delete.name != name_delete.name


def test_events_route_is_distinct_from_inspect_route():
    # A regressing reorder that let inspect swallow the /events path (or vice
    # versa) would make these resolve to the same handler — they must not.
    inspect = _resolve("GET", "/workflows/runs/run-abc123")
    events = _resolve("GET", "/workflows/runs/run-abc123/events")
    assert inspect is not None and events is not None
    assert inspect.name != events.name


def test_workflow_name_catch_all_still_matches_a_bare_name():
    # The catch-all remains reachable for a genuine single-segment workflow name
    # (proving U3 did not over-broaden or shadow it).
    route = _resolve("GET", "/workflows/my-workflow")
    assert route is not None
    assert route.name == "get_workflow_endpoint"
    assert route.path == "/workflows/{name}"


@pytest.mark.parametrize(
    "path, expected_name",
    [
        ("/workflows/runs/r1", "get_workflow_run_endpoint"),
        ("/workflows/runs/r1/events", "get_workflow_run_events_endpoint"),
        ("/workflows/runs/r1/compare", "compare_workflow_runs_endpoint"),
        ("/workflows/runs/r1/diagnostics", "get_workflow_run_diagnostics_endpoint"),
    ],
)
def test_run_paths_are_not_captured_by_the_catch_all(path: str, expected_name: str):
    """The 2-segment run paths resolve to the run handlers, never the catch-all.

    The ``/workflows/{name}`` catch-all is single-segment, so it is structurally
    unable to match a 2-segment ``/workflows/runs/...`` path REGARDLESS of
    registration order (per the design's route-ordering analysis). The genuine
    regression this guards is a future edit widening the catch-all to a greedy
    ``/workflows/{name:path}`` converter (or otherwise broadening it) so it
    swallows the run paths — that would flip this resolution to
    ``get_workflow_endpoint`` and fail here.
    """
    route = _resolve("GET", path)
    assert route is not None
    assert route.name == expected_name
    assert route.name != "get_workflow_endpoint"
