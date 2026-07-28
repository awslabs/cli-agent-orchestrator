"""U6 REST route tests (issue #511): route ORDERING (relationships before the
{key} catch-all, FR-5.2) and that the routes are registered with the right
methods. Route resolution is asserted structurally against the app's route table
(no server needed)."""

from starlette.routing import Route

from cli_agent_orchestrator.api.main import app


def _memory_routes():
    return [r for r in app.routes if isinstance(r, Route) and r.path.startswith("/memory")]


def test_relationships_routes_registered():
    # GET + POST on the collection, with the expected methods present.
    by_name = {r.name: r for r in _memory_routes()}
    assert "GET" in (by_name["list_relationships_endpoint"].methods or [])
    assert "POST" in (by_name["create_relationship_endpoint"].methods or [])
    assert "DELETE" in (by_name["delete_relationship_endpoint"].methods or [])
    # all named routes exist
    names = {r.name for r in _memory_routes()}
    assert "list_relationships_endpoint" in names
    assert "create_relationship_endpoint" in names
    assert "patch_relationship_endpoint" in names
    assert "promote_relationship_endpoint" in names
    assert "reject_relationship_endpoint" in names
    assert "delete_relationship_endpoint" in names


def test_relationships_before_key_catchall():
    """FR-5.2: the literal /memory/relationships* routes MUST be registered
    before the single-segment /memory/{key} catch-all, or FastAPI captures
    'relationships' as a key. This is the route-ordering hazard."""
    routes = [r for r in app.routes if isinstance(r, Route)]
    rel_idx = [i for i, r in enumerate(routes) if "relationships" in r.path]
    key_idx = [i for i, r in enumerate(routes) if r.path == "/memory/{key}"]
    assert rel_idx, "relationships routes must exist"
    assert key_idx, "/memory/{key} must exist"
    assert max(rel_idx) < min(
        key_idx
    ), "every /memory/relationships* route must be registered before /memory/{key}"


def test_get_memory_relationships_resolves_to_relationships_handler():
    """Resolving the path /memory/relationships must hit the relationships list
    handler, not get_memory_endpoint (which would treat 'relationships' as key)."""
    matched = None
    for r in app.routes:
        if isinstance(r, Route) and "GET" in (r.methods or []):
            scope = {"type": "http", "method": "GET", "path": "/memory/relationships"}
            match, _ = r.matches(scope)
            from starlette.routing import Match

            if match == Match.FULL:
                matched = r.name
                break
    assert matched == "list_relationships_endpoint"


def test_create_maps_unserialisable_attributes_to_400_not_500(monkeypatch):
    """FR-4.2 at the HTTP boundary: an unserialisable attribute value must
    surface as a 400, not an unhandled 500.

    Every relationship handler catches ONLY ValueError. The service's
    ``_validate_attributes`` previously let ``json.dumps``' TypeError escape, so
    this input produced an unhandled 500. The handler is driven directly (no
    server) and the service's real validator runs, so this asserts the actual
    exception-mapping contract rather than a stub's.
    """
    import asyncio

    from fastapi import HTTPException

    from cli_agent_orchestrator.api.main import (
        RelationshipCreateRequest,
        create_relationship_endpoint,
    )

    body = RelationshipCreateRequest(
        scope="global",
        source_key="a",
        target_key="b",
        type="relates_to",
        origin="human",
    )
    # A set is not JSON-serialisable. Bypass pydantic's own coercion by setting
    # the attribute post-construction — the point under test is the SERVICE's
    # validator and the handler's except clause, not request-model validation.
    object.__setattr__(body, "attributes", {"bad": {1, 2, 3}})

    try:
        asyncio.run(create_relationship_endpoint(body, _scopes=[]))
    except HTTPException as e:
        assert e.status_code == 400, f"must map to 400, got {e.status_code}"
        assert "JSON-serialisable" in str(e.detail)
    except TypeError as e:  # pragma: no cover - this is the pre-fix failure
        raise AssertionError(
            f"TypeError escaped the handler and would surface as a 500: {e}"
        ) from e
    else:
        raise AssertionError("expected an HTTPException(400)")
