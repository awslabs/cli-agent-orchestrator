# MCP Apps — deferred security follow-ups (H3 & H4)

**Status:** RESOLVED — H3 and H4 implemented (plus the JWKS-robustness,
`/events/history` input-hardening, and UI-scope items below). See the
"Resolution" notes under each section.
**Origin:** review of PR [#332](https://github.com/awslabs/cli-agent-orchestrator/pull/332)
("sandboxed host-rendered fleet UI"). The Copilot comments (C1–C3) and the
default-off / auth-correctness items (H1, H2, M1, M2) were addressed in the
review branch. The two items below were **intentionally deferred** at the time to
keep that change focused and behavior-preserving; this document recorded the plan
and now records how each was implemented.

> **Resolved:** auth-enabled mutation via the MCP Apps surface is now supported
> when an operator provisions `CAO_AUTH_LOCAL_TOKEN` (see H3). The surface remains
> safe in its default posture (default-off) and in its intended posture (enabled
> on a trusted loopback host with no IdP).

---

## H3 — Internal MCP→API calls are unauthenticated

### Problem
The MCP server (`cao-mcp-server`) reaches Backplane state over loopback HTTP to
the FastAPI app (`API_BASE_URL`). PR #332 added real scope enforcement
(`Depends(require_any_scope(...))`) to the mutation endpoints, but the MCP-side
HTTP helpers attach **no `Authorization` header**:

- `mcp_server/app_tools.py` — `_post_json` / `_delete_json` (used by
  `submit_command` to route `create_session` / `send_message` / `assign` /
  `interrupt` / `shutdown_session`).
- `mcp_server/utils.py` — `get_terminal_record` (GET; reads are not gated, so it
  is unaffected today, but should be made consistent).

Consequently, **with auth enabled** (`AUTH0_DOMAIN` / `CAO_AUTH_JWKS_URI` set),
every mutation routed through `submit_command` — and the pre-existing MCP
mutation tools that hit the same endpoints — receives `401 Unauthorized` from the
FastAPI boundary. The "two-layer enforcement" story (UX pre-check on the MCP side,
real enforcement at the HTTP boundary) only triggers in the one posture where it
breaks the surface entirely.

Note: `app_tools` already *reads* `CAO_AUTH_LOCAL_TOKEN` for the local scope
pre-check (`get_scopes_for_local_token`) but never sends it on the wire.

### Options
1. **Forward a service/local token (preferred).** When auth is enabled, attach
   `Authorization: Bearer <CAO_AUTH_LOCAL_TOKEN>` to outgoing MCP→API requests in
   `app_tools` (and `utils` for consistency). The token plumbing is half-present;
   this completes it. Requires documenting how an operator provisions the local
   token (a machine token from the same IdP with the scopes the surface needs).
2. **Loopback trust exemption.** Exempt loopback callers from the scope
   dependency (e.g. trust `127.0.0.1` with a shared secret / Unix-socket marker).
   Simpler operationally but widens the trust boundary and is easy to misconfigure
   behind a reverse proxy; would need to interact carefully with
   `forwarded_allow_ips`.
3. **Document loopback-trust-only.** Keep the surface unauthenticated on the
   internal hop, drop the "real enforcement at the FastAPI boundary" claim from
   the docs, and state plainly that auth-enabled mutation is unsupported.

### Recommendation
Option **1** (forward `CAO_AUTH_LOCAL_TOKEN`), with a clear failure mode: if auth
is enabled and no local token is configured, `submit_command` should return a
structured, actionable error ("auth enabled but CAO_AUTH_LOCAL_TOKEN not set")
rather than surfacing a raw 401.

### Acceptance criteria
- With auth enabled and a valid `CAO_AUTH_LOCAL_TOKEN`, `submit_command`
  mutations succeed end to end (no 401).
- With auth enabled and **no** local token, `submit_command` returns a clear
  `{"success": false, "error": ...}` explaining the misconfiguration.
- With auth disabled (default), behavior is byte-for-byte unchanged.
- The dashboard/agent UI sources its `scopes` from the caller's token (not the
  full local set) so read-only operators do not see controls that will 403
  (relates to the deferred UI-scope item).
- Tests: a unit/integration test for the bearer-forwarding path and the
  missing-token error; a default-off regression.

### Files likely touched
`mcp_server/app_tools.py`, `mcp_server/utils.py`, `security/auth.py` (a
`get_local_bearer()` helper), `docs/mcp-apps.md`, and tests under
`test/mcp_server/` + `test/api/`.

### Resolution (implemented)
Option **1** was implemented. `security/auth.py` gained `get_local_bearer()`
(returns `CAO_AUTH_LOCAL_TOKEN` when auth is enabled, else `None`) and
`local_auth_misconfig_error()` (the actionable "auth enabled but
CAO_AUTH_LOCAL_TOKEN not set" message). `mcp_server/app_tools.py` and
`mcp_server/utils.py` now attach `Authorization: Bearer <token>` to every
outgoing MCP→API request via a shared `_auth_headers()` helper (reads *and*
mutations, for consistency). `submit_command` returns the structured misconfig
error before routing when auth is on and the token is missing, instead of leaking
a raw `401`. Default-off attaches no header (byte-for-byte unchanged). Covered by
`test/mcp_server/test_app_tools.py` (bearer forwarding, default-off, missing-token
error) and `test/mcp_server/test_utils.py`.

---

## H4 — Scope coverage is incomplete across mutating routes

### Problem
PR #332 introduced the OAuth layer and wired `Depends(require_any_scope(...))`
into the five endpoints relevant to the MCP Apps mutation path:

- `POST /sessions` (write/admin)
- `DELETE /sessions/{name}` (admin)
- `POST /terminals/{id}/input` (write/admin)
- `POST /terminals/{id}/key` (write/admin)
- `POST /terminals/{receiver}/inbox/messages` (write/admin)

Plus the read endpoints `/events` and `/events/history` (read, added in this
review). However, **the other pre-existing mutating routes are not scope-gated**,
so when auth is enabled a `cao:read` token can still invoke them. Observed
ungated mutations include:

- `POST /sessions/{name}/terminals` (create terminal in session)
- `POST /terminals/{id}/run-step` (creates terminals, runs an agent step)
- `POST /terminals/{id}/exit`, `DELETE /terminals/{id}`
- `POST /flows`, `DELETE /flows/{name}`, `POST /flows/{name}/enable|disable|run`
- `DELETE /workflows/{name}`, `POST /workflows/runs/.../output`
- `DELETE /memory/{key}`, `DELETE /memory`
- `POST /settings/agent-dirs`, `POST /settings/skill-dirs`,
  `POST /agents/profiles/install`

### Why it was deferred
It is broader than the MCP Apps surface — it hardens the whole HTTP API and
touches subsystems (flows, workflows, memory, settings) outside this PR's remit.
Doing it well also requires a deliberate write-vs-admin classification per route.

### Plan
Add `Depends(require_any_scope(...))` to every mutating route, choosing the scope
by destructiveness:

- **`cao:write` (or admin):** create/update/run operations
  (`/sessions/{name}/terminals`, `/terminals/{id}/run-step`,
  `/terminals/{id}/exit`, `/settings/*`, `/agents/profiles/install`,
  `/flows` create + enable/disable/run, `/workflows/runs/.../output`).
- **`cao:admin`:** destructive deletes (`DELETE /terminals/{id}`,
  `DELETE /flows/{name}`, `DELETE /workflows/{name}`, `DELETE /memory/{key}`,
  `DELETE /memory`).

This is behavior-preserving when auth is disabled (the dependency returns the
full scope set), mirroring the five already-wired routes.

### Acceptance criteria
- With auth enabled, a `cao:read`-only token is `403`'d on **every** mutating
  route; a `cao:write`/`cao:admin` token succeeds per the classification above.
- With auth disabled (default), every route behaves byte-for-byte as today.
- A test enumerates the route table and asserts no mutating route is missing a
  scope dependency (a guard test, so future routes don't silently regress).

### Files likely touched
`src/cli_agent_orchestrator/api/main.py`, and tests under `test/api/`
(including a route-coverage guard test).

### Resolution (implemented)
Every mutating route now carries `Depends(require_any_scope(...))`, classified by
destructiveness exactly as planned above — `cao:write`/`cao:admin` for
create/update/run (`/sessions/{name}/terminals`, `/terminals/run-step`,
`/terminals/{id}/exit`, `/settings/agent-dirs`, `/settings/skill-dirs`,
`/agents/profiles/install`, `/flows` create + `enable`/`disable`/`run`,
`/workflows/runs/.../output`) and `cao:admin` for destructive deletes
(`DELETE /terminals/{id}`, `DELETE /flows/{name}`, `DELETE /workflows/{name}`,
`DELETE /memory/{key}`, `DELETE /memory`). `POST /workflows/validate` is left
ungated as a read-only operation (no state change). A guard test
(`test/api/test_scope_coverage.py`) enumerates the live route table and fails if
any mutating route is missing a scope dependency, plus enforcement tests assert a
`cao:read` token is `403`'d on a write route and a `cao:write` token is `403`'d on
an admin route. Behavior is unchanged with auth disabled (the dependency returns
the full scope set).

---

## Related smaller follow-ups

These were noted in the same review. The status of each is marked below.

- **UI scope source.** `render_dashboard` populates snapshot `scopes` from
  `get_scopes_for_local_token()`. With H3 implemented, an operator who configures
  `CAO_AUTH_LOCAL_TOKEN` now has the dashboard reflect exactly that token's
  granted scopes (the loopback caller's identity), so read-only operators no
  longer see controls that would `403`. **Addressed by H3** (no separate change
  needed); the full set is only surfaced in the default-off posture.
- **JWKS robustness.** ✅ **Done.** The cache forces a single refresh + retry on an
  unknown `kid` (rotation no longer waits out the 1 h TTL), and reuse-on-unreachable
  is bounded by a 24 h staleness cap (`_JWKS_MAX_STALENESS`) so it fails closed
  rather than trusting indefinitely-stale keys.
- **`/events/history` input hardening.** ✅ **Done.** `kinds` is validated against
  `event_primitives.KINDS` (unknown kind → `400`) and `limit` is clamped via
  `Query(ge=0, le=RING_CAPACITY)`.
- **`pause`/`resume` gestures.** Open (frontend). `TaskControl` renders these
  buttons but `submit_command` returns `unsupported` (no Backplane route); hide or
  disable them in `cao_mcp_apps/` until routes exist.
- **`topology.js` reconnect.** Open (frontend). Reconnect with capped backoff after
  a dropped SSE stream; add a restrictive CSP / `frame-ancestors` to
  `topology.html`.
- **Unused `requires_scopes` decorator.** Open. `security/decorators.py` is not
  wired into the production tool path (the `submit_command` choke point does its
  own inline scope pre-check) — wire it or remove it.
- **Reserved primitives.** Open. `file_mod` and `error` are in the `PRIMITIVES`
  vocabulary but `normalize_kind` never emits them; document as reserved or wire
  producing events.
- **`submit_command` audit trail (optional).** Open enhancement. The choke point
  does not emit an audit record today; add one if an audit trail is desired (the
  PR does not claim it is "audited", so this is an enhancement, not a regression).
