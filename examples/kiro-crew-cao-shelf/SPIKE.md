# Appendix: verified Kiro Crew and CAO behaviour

What was actually run before and while building the CAO Shelf (2026-11-15, Linux aarch64), kept
as the evidence behind [PROPOSAL.md](PROPOSAL.md) and the [README](README.md). Every claim here was
run or read at the cited file; everything else is marked **INFERRED**. The runs used an isolated
Kiro Crew 0.12.2 home on port 5499 and a scratch `cao-server` on 9990 built from CAO main
(`/health` says 2.5.4; main is 201 commits past the `v2.5.4` tag). The only requests to a live
CAO 2.5.4 were GETs, and only key sets and types were kept.

**Rule from the one incident.** `kirocrew stop`, `restart`, `service` and `doctor` act on the
service of the real user, whatever `HOME` or `KIROCREW_PORT` say: a `kirocrew stop` meant for an
isolated gateway stopped the real one. The rig never calls them and stops processes by recorded pid
and start time only.

## 1. Kiro Crew: what an App is

* **Install and trust.** `kirocrew app install <dir>`, `app enable <name>`, `app update <name>
  --source <dir>` (update needs a running gateway). With `agent.apps_allow_third_party=false`
  (default) enable is refused (`app_execution_denied`); `install.sh --trust-app` adds the app name to
  `agent.apps_trusted` and `agent.apps_trusted_local` and enable then works with the blanket flag
  still false. Uninstall removes that grant, so the script grants trust after installing. Revocation
  stopping the backend within one health interval is INFERRED.
* **Page.** A prebuilt ES module mounted in-process as a React component; the host import map
  provides `@kirocrew/app-sdk`, `react` and `react-dom`. The host's Tailwind is precompiled, so the
  App ships its own scoped `<style>` and uses the host CSS variables. `ui.entry` is relative to the
  App's `ui/` folder (`bundle/index.mjs`). `localStorage` works. `permissions.api` is enforced by the
  SDK in the page.
* **Backend.** A child process spawned with the gateway's own Python (3.12 in 0.12.2), cwd = the app
  dir, `PORT` injected (9100 to 9199), and a fixed environment (no app-specific variables). The
  gateway checks `GET /health` unsigned.
* **Proxy signature.** Calls arrive as `/api/<path>` with `X-KiroCrew-Proxy: <ts>:<hmac>` and, on
  every proxied request, the v2 pair `X-KiroCrew-Proxy-V2` and `X-KiroCrew-Proxy-Nonce` (source:
  `kiro_crew/apps/routes_proxy.py` in the 0.12.2 install sets all three unconditionally). The
  backend requires both; the e2e journeys run through the real gateway and pass, so the real
  gateway signs v2 correctly.
* **Sandbox is not a boundary here.** The backend, at the standard sandbox level, could write
  outside its folder and reach loopback ports. macOS and Windows behaviour is unknown.
* **Registry and panel tabs.** A team registry install (clone and build) and `contributes.panelTabs`
  rendering were not rehearsed (INFERRED from source and docs).

## 2. The page cannot call CAO directly

The dashboard's CSP allows `connect-src http://127.0.0.1:*`, but CAO's CORS list is localhost on
3000, 5173 and 5174 only, and CAO refuses a mutating call with a foreign `Origin` (403). Through a
tunnel the browser's `127.0.0.1` is not the CAO host anyway. So the backend proxies; it reached the
scratch CAO in 26 to 29 ms.

## 3. CAO facts that shape the design

1. `GET /sessions` rows carry `id, name, status, working_directory, agent_profile`; `status` is
   `active` on herdr and `detached` on tmux, so it says nothing about activity.
2. `GET /sessions/{name}` returns `{session, terminals[]}`; each terminal has `status`,
   `last_active`, `pending_inbox_count`, `tmux_window`, `agent_profile`, `provider`; **no
   `caller_id`**. (`GET /sessions/{name}/terminals` has `status` only on main, not on 2.5.4.)
3. `caller_id` is only on `GET /terminals/{id}`. It never changes, so it is read once per terminal
   (every terminal, the first one too) and cached.
4. Statuses seen: `idle`, `completed`, `waiting_user_answer`; `processing` only in CAO's own tests
   (the mock answers in 50 ms). Status is inferred from the screen.
5. **Answering a waiting terminal** is `POST /terminals/{id}/input?message=y`; the terminal went
   `waiting_user_answer` to `completed`. The inbox route (`send_session_message`) is wrong for
   providers that block orchestrated input while waiting (source: `terminal_service.py`); the mock
   does not opt in, so that refusal was read, not exercised.
6. `POST /terminals/{id}/inbox/messages` to an idle conductor was `delivered` at once; to a busy one
   the row stays `pending`. The POST answer carries no status, so Ask reads the row back
   (`GET /terminals/{id}/inbox/messages`) to say which. `sender_id` is not checked to be a terminal.
7. **History survives the helper.** Deleting a helper leaves its rows in the conductor's inbox with
   the dead `sender_id`. Limits: `limit` max 100, no offset, only the 8-hex id (no name), rows swept
   after 14 days.
8. `DELETE /sessions/cao-<name>` returns `{"success":true,"deleted":[...],"errors":[]}`; it needs
   `cao:admin` when auth is on.
9. `POST /sessions?agent_profile&provider&session_name&working_directory` creates a session with one
   terminal (201); `idempotency_key` makes a retry of the same request return the first terminal
   (scoped by a fingerprint of the request). `POST /sessions/{n}/terminals?...&caller_id=` adds a
   helper; **without `caller_id` it is refused (403)**, so a real fleet has no peers.
10. CAO lets you delete a caller terminal that has live helpers; the helper keeps its `caller_id`.
    That is an orphan, and the case where the first terminal of a session is itself an orphaned
    helper (its conductor was deleted).
11. `terminals.last_active` is naive UTC text in the database; the API returns an ISO string.
12. Auth is off by default (`CAO_AUTH_LOCAL_TOKEN` turns on bearer auth): not supported, reported as
    `cao_auth_required`.
13. `ui://cao/dashboard` (MCP Apps) is a flat grid with no `caller_id`, no inbox and no history.

## 4. Not exercised

A real `claude_code` or `kiro_cli` provider (no keyless way), so quick answers exist for `mock_cli`
only; a team-registry install; `panelTabs` rendering; the approval flow when an update adds
`permissions.api` entries; macOS and Windows.
