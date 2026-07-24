# Herdr Integration Modernization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete CAO's herdr-0.6.x-era defensive code by adopting herdr 0.7.x capabilities — broadcast event subscription, single-call `api snapshot` reconcile, and native `--env` injection.

**Architecture:** Three independent workstreams against `services/herdr_inbox_service.py` (R1 subscription, R2 reconcile) and `backends/herdr_backend.py` (R3 env), then a dependent cleanup (R4) that deletes the pane-ID-resolution machinery once R2's snapshot rebuild proves stable IDs. R5 (kiro) is a decision, not code. Phase E (env-survival) is gated on an investigation.

**Tech Stack:** Python 3.12, asyncio, pytest (`uv run pytest`), herdr 0.7.5 socket API (protocol 17). Tests live in `test/backends/` (both backend and inbox-service suites).

---

## Verified facts this plan rests on

All tested firsthand against herdr 0.7.5 on the dev host:

- **Broadcast `pane.updated` (no `pane_id`) works** and its payload wraps the pane under `data.pane` with `agent_status`, `pane_id`, `terminal_id`. Wire name is `pane_updated` (underscore).
- **`pane.agent_status_changed` still requires `pane_id`** — cannot be a broadcast; `pane.updated` is the broadcast source.
- **A second `events.subscribe` on one connection still resets it in 0.7.5** — "exactly one subscribe per connection" stays mandatory.
- **`herdr --session <s> api snapshot`** returns `result.snapshot` with `panes[]` (each has `pane_id`, `terminal_id`, `agent_status`, `tab_id`, `workspace_id`), `tabs[]` (each has `tab_id`, `label`, `workspace_id`), `workspaces[]` (each has `workspace_id`, `label`).
- **Public IDs are stable across sibling-tab close** (only a full server restart compacts them).
- **`herdr tab create --env KEY=VALUE` is accepted**; env does NOT survive a server restart (Phase E).

## File structure

| File | Responsibility | Workstream |
|---|---|---|
| `src/cli_agent_orchestrator/services/herdr_inbox_service.py` | socket subscription + reconcile | R1, R2, R4 |
| `src/cli_agent_orchestrator/backends/herdr_backend.py` | env injection, arg allowlist, pane-id resolution | R3, R4 |
| `test/backends/test_herdr_inbox_service.py` | inbox-service unit tests | R1, R2, R4 |
| `test/backends/test_herdr_backend.py` | backend unit tests | R3, R4 |

Test helper (already present in both suites): `def _run_async(coro): return asyncio.run(coro)`.

---

## Phase 1 — R1: single broadcast subscribe

### Task 1: Switch subscription to broadcast pane.updated

**Files:**
- Modify: `src/cli_agent_orchestrator/services/herdr_inbox_service.py:486-517` (`_subscribe_all_events`)
- Test: `test/backends/test_herdr_inbox_service.py`

- [ ] **Step 1: Replace the two existing subscribe tests with broadcast expectations**

In `test/backends/test_herdr_inbox_service.py`, replace `test_subscribe_all_events_sends_single_combined_message` (lines 155-180) and `test_subscribe_all_events_with_no_panes_still_includes_lifecycle` (lines 182-198) with:

```python
    def test_subscribe_all_events_sends_single_broadcast_message(self):
        """One events.subscribe with broadcast pane.updated + lifecycle, NO pane_id.

        herdr 0.7.5 resets the connection on a second events.subscribe, so this
        must stay a single call. pane.updated is a broadcast (no pane_id) that
        carries agent_status for every pane, so per-pane subscriptions are gone.
        """
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service._writer = AsyncMock()
        service._pane_to_terminal = {"w1:p1": "tid1", "w1:p2": "tid2"}

        _run_async(service._subscribe_all_events())

        service._writer.write.assert_called_once()
        msg = json.loads(service._writer.write.call_args[0][0].decode().strip())
        assert msg["method"] == "events.subscribe"
        types = {s["type"] for s in msg["params"]["subscriptions"]}
        assert types == {"pane.updated", "pane.closed", "workspace.closed"}
        # Broadcast subscriptions carry no pane_id.
        assert all("pane_id" not in s for s in msg["params"]["subscriptions"])
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest --no-cov -p no:cacheprovider test/backends/test_herdr_inbox_service.py -k subscribe_all_events_sends_single_broadcast -v`
Expected: FAIL — current code emits `pane.agent_status_changed` per pane, so `types` will not equal the broadcast set.

- [ ] **Step 3: Rewrite `_subscribe_all_events` to broadcast**

Replace the body of `_subscribe_all_events` (lines 500-517) with:

```python
        subscriptions = [
            {"type": "pane.updated"},
            {"type": "pane.closed"},
            {"type": "workspace.closed"},
        ]
        message = {
            "id": "sub_all",
            "method": "events.subscribe",
            "params": {"subscriptions": subscriptions},
        }
        await self._send(message)
        logger.info(
            "Subscribed to broadcast pane.updated + lifecycle events "
            "in one events.subscribe call"
        )
```

Also update the method docstring (lines 487-499): the subscription is now broadcast and independent of `_pane_to_terminal`; the "one subscribe per connection" rule remains because the second-subscribe reset persists in 0.7.5.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest --no-cov -p no:cacheprovider test/backends/test_herdr_inbox_service.py -k subscribe -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/cli_agent_orchestrator/services/herdr_inbox_service.py test/backends/test_herdr_inbox_service.py
git commit -m "feat(herdr): broadcast pane.updated subscription instead of per-pane"
```

### Task 2: Parse the pane.updated payload shape in _event_loop

**Files:**
- Modify: `src/cli_agent_orchestrator/services/herdr_inbox_service.py:563-565` (payload extraction in `_event_loop`)
- Test: `test/backends/test_herdr_inbox_service.py`

- [ ] **Step 1: Write the failing test**

Add to the `TestHerdrInboxServiceEventParsing` class in `test/backends/test_herdr_inbox_service.py`:

```python
    def test_event_loop_reads_pane_updated_nested_pane(self):
        """pane.updated wraps the pane object under data.pane; extraction must
        read pane_id/agent_status from there and deliver for a managed pane."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        callback = MagicMock()
        service._delivery_callback = callback
        service._pane_to_terminal = {"w1:p1": "tid1"}

        frame = {
            "event": "pane_updated",
            "data": {"pane": {"pane_id": "w1:p1", "agent_status": "idle"}},
        }
        reader = AsyncMock()
        reader.readline.side_effect = [
            (json.dumps(frame) + "\n").encode(),
            b"",  # EOF ends the loop
        ]
        service._reader = reader
        try:
            _run_async(service._event_loop())
        except ConnectionError:
            pass  # EOF raises ConnectionError("Socket closed") — expected

        callback.assert_called_once_with("tid1")

    def test_event_loop_ignores_pane_updated_for_unmanaged_pane(self):
        """Broadcast now delivers events for ALL panes; the managed-pane filter
        must drop events for panes CAO does not track."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        callback = MagicMock()
        service._delivery_callback = callback
        service._pane_to_terminal = {"w1:p1": "tid1"}

        frame = {
            "event": "pane_updated",
            "data": {"pane": {"pane_id": "w9:p9", "agent_status": "idle"}},
        }
        reader = AsyncMock()
        reader.readline.side_effect = [(json.dumps(frame) + "\n").encode(), b""]
        service._reader = reader
        try:
            _run_async(service._event_loop())
        except ConnectionError:
            pass

        callback.assert_not_called()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest --no-cov -p no:cacheprovider test/backends/test_herdr_inbox_service.py -k pane_updated -v`
Expected: FAIL — current extraction reads `data.get("pane_id")` at top level, which is absent in the nested `pane.updated` shape, so `terminal_id` is None and the callback is never called.

- [ ] **Step 3: Update extraction to unwrap data.pane**

In `_event_loop`, replace lines 563-565:

```python
            data = event.get("data", {})
            pane_id = data.get("pane_id", "")
            status = data.get("agent_status", "")
```

with:

```python
            data = event.get("data", {})
            # pane.updated wraps the pane object under data.pane; agent-status
            # events put fields at the top of data. Handle both.
            pane_obj = data.get("pane", data)
            pane_id = pane_obj.get("pane_id", "")
            status = pane_obj.get("agent_status", "")
```

Leave the lifecycle branch (lines 558-561) and the managed-pane guard (line 568) unchanged.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest --no-cov -p no:cacheprovider test/backends/test_herdr_inbox_service.py -k "pane_updated or event_loop" -v`
Expected: PASS (new tests plus the existing data-wrapper tests)

- [ ] **Step 5: Commit**

```bash
git add src/cli_agent_orchestrator/services/herdr_inbox_service.py test/backends/test_herdr_inbox_service.py
git commit -m "feat(herdr): parse nested data.pane from broadcast pane.updated events"
```

### Task 3: Remove force-reconnect on register_terminal

**Files:**
- Modify: `src/cli_agent_orchestrator/services/herdr_inbox_service.py:99-127` (`register_terminal`), delete `_force_reconnect` (519-534)
- Test: `test/backends/test_herdr_inbox_service.py` (remove the obsolete reconnect test)

- [ ] **Step 1: Write the test asserting register does NOT reconnect**

Replace `test_register_while_connected_triggers_reconnect_not_second_subscribe` (starts line 72) with:

```python
    def test_register_while_connected_does_not_touch_socket(self):
        """With broadcast subscription, a newly registered pane's events already
        arrive — registration must NOT close the socket or write anything."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        writer = MagicMock()
        service._writer = writer
        service._connected = True
        service._loop = asyncio.new_event_loop()

        service.register_terminal("tid1", "w1:p1", is_kiro=False)

        assert service._pane_to_terminal["w1:p1"] == "tid1"
        writer.close.assert_not_called()
        writer.write.assert_not_called()
        service._loop.close()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest --no-cov -p no:cacheprovider test/backends/test_herdr_inbox_service.py -k register_while_connected -v`
Expected: FAIL — current `register_terminal` schedules `_force_reconnect` which calls `writer.close()`.

- [ ] **Step 3: Remove the reconnect block from register_terminal**

Delete lines 114-127 of `register_terminal` (the comment block plus `if self._connected and self._loop is not None: asyncio.run_coroutine_threadsafe(self._force_reconnect(), self._loop)`). The method ends after the `logger.info(...)` registration line. Then delete the entire `_force_reconnect` method (lines 519-534). Update the module docstring (lines 7-11): subscription is broadcast; new panes need no reconnect.

- [ ] **Step 4: Verify no orphan references**

Run: `grep -n "_force_reconnect" src/ test/`
Expected: no output.

- [ ] **Step 5: Run the scoped suite**

Run: `uv run pytest --no-cov -p no:cacheprovider -q test/backends/test_herdr_inbox_service.py`
Expected: PASS (all tests)

- [ ] **Step 6: Live check (real herdr, not mocks)**

With `cao-server` on the herdr backend, launch two agents in one session. Confirm the server log shows a single "Subscribed to broadcast" line and NO reconnect loop when the second agent registers.

- [ ] **Step 7: Commit**

```bash
git add src/cli_agent_orchestrator/services/herdr_inbox_service.py test/backends/test_herdr_inbox_service.py
git commit -m "feat(herdr): drop force-reconnect on register — broadcast covers new panes"
```

---

## Phase 2 — R2: session snapshot reconcile

### Task 4: Add a _fetch_snapshot helper

**Files:**
- Modify: `src/cli_agent_orchestrator/services/herdr_inbox_service.py` (new method near `_reconcile`)
- Test: `test/backends/test_herdr_inbox_service.py`

- [ ] **Step 1: Write the failing test**

Add a new test class:

```python
class TestHerdrInboxSnapshot:
    """_fetch_snapshot returns the parsed snapshot dict from `api snapshot`."""

    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    def test_fetch_snapshot_parses_result(self, mock_run):
        service = HerdrInboxService(socket_path="/tmp/test.sock", herdr_session="cao")
        snap = {
            "result": {
                "snapshot": {
                    "panes": [
                        {"pane_id": "w1:p1", "terminal_id": "term_a",
                         "agent_status": "idle", "tab_id": "w1:t1", "workspace_id": "w1"}
                    ],
                    "tabs": [{"tab_id": "w1:t1", "label": "conductor", "workspace_id": "w1"}],
                    "workspaces": [{"workspace_id": "w1", "label": "sess-a"}],
                }
            }
        }
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(snap), stderr="")

        result = service._fetch_snapshot()

        assert [p["pane_id"] for p in result["panes"]] == ["w1:p1"]
        assert result["workspaces"][0]["label"] == "sess-a"
        # Invoked `api snapshot` for the configured session.
        args = mock_run.call_args[0][0]
        assert args[:2] == ["herdr", "--session"]
        assert args[-2:] == ["api", "snapshot"]

    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    def test_fetch_snapshot_returns_none_on_failure(self, mock_run):
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")
        assert service._fetch_snapshot() is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest --no-cov -p no:cacheprovider test/backends/test_herdr_inbox_service.py -k fetch_snapshot -v`
Expected: FAIL — `_fetch_snapshot` does not exist (AttributeError).

- [ ] **Step 3: Implement `_fetch_snapshot`**

Add above `_reconcile` (line 281):

```python
    def _fetch_snapshot(self) -> Optional[dict]:
        """Return herdr's full live session snapshot in one socket call.

        `herdr api snapshot` returns result.snapshot with panes[]/tabs[]/
        workspaces[]. Each pane carries pane_id, terminal_id, agent_status,
        tab_id, workspace_id; each tab carries tab_id, label, workspace_id;
        each workspace carries workspace_id, label. Replaces the former
        pane-list + workspace-list + tab-list subprocess fan-out.
        """
        result = subprocess.run(
            ["herdr", "--session", self._herdr_session, "api", "snapshot"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.warning(f"Snapshot: `api snapshot` failed: {result.stderr}")
            return None
        try:
            return json.loads(result.stdout)["result"]["snapshot"]
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Snapshot: failed to parse: {e}")
            return None
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest --no-cov -p no:cacheprovider test/backends/test_herdr_inbox_service.py -k fetch_snapshot -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/cli_agent_orchestrator/services/herdr_inbox_service.py test/backends/test_herdr_inbox_service.py
git commit -m "feat(herdr): add _fetch_snapshot helper over `api snapshot`"
```

### Task 5: Rewrite _reconcile to consume the snapshot

**Files:**
- Modify: `src/cli_agent_orchestrator/services/herdr_inbox_service.py:281-479` (`_reconcile`)
- Test: `test/backends/test_herdr_inbox_service.py` (adapt existing reconcile tests)

- [ ] **Step 1: Capture a real snapshot fixture**

Run (isolated session, torn down after):
```bash
herdr --session cao-fix server &   # then create a labeled workspace + tab
herdr --session cao-fix api snapshot > /tmp/herdr_snapshot_fixture.json
```
Save the `result.snapshot` object into the test as a literal (do NOT commit the raw file). Confirm the field names match Task 4's shape.

- [ ] **Step 2: Rewrite the existing reconcile tests to feed one snapshot call**

The current reconcile tests (`test_reconcile_prunes_stale_pane`, `test_reconcile_no_op_when_all_panes_live`, `test_reconcile_continues_on_pane_list_failure`, and the DB-cross-check tests) mock three separate `subprocess.run` calls. Convert each to mock a single `api snapshot` return via `_fetch_snapshot`. Example for the no-op case:

```python
    @patch.object(HerdrInboxService, "_fetch_snapshot")
    def test_reconcile_no_op_when_all_panes_live(self, mock_snap):
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service._pane_to_terminal = {"w1:p1": "tid1"}
        service._terminal_to_pane = {"tid1": "w1:p1"}
        mock_snap.return_value = {
            "panes": [{"pane_id": "w1:p1", "terminal_id": "tid1",
                       "agent_status": "idle", "tab_id": "w1:t1", "workspace_id": "w1"}],
            "tabs": [{"tab_id": "w1:t1", "label": "conductor", "workspace_id": "w1"}],
            "workspaces": [{"workspace_id": "w1", "label": "sess-a"}],
        }
        _run_async(service._reconcile())
        assert service._pane_to_terminal == {"w1:p1": "tid1"}
```

Preserve the assertions of the stale-prune and ghost-DB tests; only the mock source changes (one `_fetch_snapshot` instead of three `subprocess.run`).

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest --no-cov -p no:cacheprovider test/backends/test_herdr_inbox_service.py -k reconcile -v`
Expected: FAIL — `_reconcile` still calls `subprocess.run` directly, ignoring the `_fetch_snapshot` mock.

- [ ] **Step 4: Rewrite `_reconcile` to derive everything from one snapshot**

Replace the three subprocess blocks (lines 293-371) with a single `_fetch_snapshot()` call and derive the three data structures from it. Keep the existing stale-prune, ghost-DB-delete, and workspace-teardown logic (lines 373-479) exactly — only the data source changes:

```python
        snapshot = self._fetch_snapshot()
        if snapshot is None:
            logger.warning("Reconcile: no snapshot, skipping")
            return

        panes = snapshot.get("panes", [])
        live_pane_ids = {p["pane_id"] for p in panes}

        # workspace_id -> label (= CAO session name)
        self._workspace_to_session = {
            ws["workspace_id"]: ws["label"] for ws in snapshot.get("workspaces", [])
        }

        # workspace_id -> set of live tab labels (= CAO window names)
        live_tabs_by_workspace: Dict[str, set] = {}
        for tab in snapshot.get("tabs", []):
            ws_id = tab.get("workspace_id", "")
            label = tab.get("label", "")
            if ws_id and label:
                live_tabs_by_workspace.setdefault(ws_id, set()).add(label)
```

Then keep the existing DB-cross-check loop (the `from ...database import delete_terminal, list_terminals_by_session` block and the ghost-deletion loop) and the stale-pane logic below it verbatim.

- [ ] **Step 5: Run to verify they pass**

Run: `uv run pytest --no-cov -p no:cacheprovider test/backends/test_herdr_inbox_service.py -k reconcile -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/cli_agent_orchestrator/services/herdr_inbox_service.py test/backends/test_herdr_inbox_service.py
git commit -m "feat(herdr): reconcile from single api snapshot, not 3 subprocess calls"
```

### Task 6: Rewrite _startup_db_cleanup to use the snapshot

**Files:**
- Modify: `src/cli_agent_orchestrator/services/herdr_inbox_service.py:154-231` (`_startup_db_cleanup`)
- Test: `test/backends/test_herdr_inbox_service.py`

- [ ] **Step 1: Adapt the startup-cleanup tests to mock _fetch_snapshot**

Find the existing `_startup_db_cleanup` tests (they mock `workspace list` + `tab list`). Convert them to mock a single `_fetch_snapshot` return, preserving the ghost-deletion assertion. If no dedicated test exists, add:

```python
    @patch("cli_agent_orchestrator.services.herdr_inbox_service.delete_terminal")
    @patch("cli_agent_orchestrator.services.herdr_inbox_service.list_terminals_by_session")
    @patch.object(HerdrInboxService, "_fetch_snapshot")
    def test_startup_cleanup_deletes_ghost_from_snapshot(self, mock_snap, mock_list, mock_del):
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        mock_snap.return_value = {
            "panes": [], "workspaces": [{"workspace_id": "w1", "label": "sess-a"}],
            "tabs": [{"tab_id": "w1:t1", "label": "live-win", "workspace_id": "w1"}],
        }
        mock_list.return_value = [
            {"id": "ghost", "tmux_window": "dead-win"},
            {"id": "keep", "tmux_window": "live-win"},
        ]
        _run_async(service._startup_db_cleanup())
        mock_del.assert_called_once_with("ghost")
```

Adjust the `@patch` import targets to match how `_startup_db_cleanup` imports `delete_terminal` / `list_terminals_by_session` (module-level vs local import — check lines 154-231 and patch accordingly).

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest --no-cov -p no:cacheprovider test/backends/test_herdr_inbox_service.py -k startup_cleanup -v`
Expected: FAIL — current code calls `subprocess.run` for workspace/tab lists.

- [ ] **Step 3: Rewrite `_startup_db_cleanup` to use `_fetch_snapshot`**

Replace its `workspace list` + `tab list` subprocess calls with one `_fetch_snapshot()`, building `workspace_to_session` and `live_tabs_by_workspace` exactly as Task 5 does, then keep the existing ghost-deletion loop.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest --no-cov -p no:cacheprovider test/backends/test_herdr_inbox_service.py -k startup_cleanup -v`
Expected: PASS

- [ ] **Step 5: Run the full inbox-service suite**

Run: `uv run pytest --no-cov -p no:cacheprovider -q test/backends/test_herdr_inbox_service.py`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/cli_agent_orchestrator/services/herdr_inbox_service.py test/backends/test_herdr_inbox_service.py
git commit -m "feat(herdr): startup DB cleanup from api snapshot"
```

---

## Phase 3 — R3: native --env injection

### Task 7: Allow --env in the herdr arg sanitizer

**Files:**
- Modify: `src/cli_agent_orchestrator/backends/herdr_backend.py:53-62` (`_HERDR_ALLOWED_FLAGS`)
- Test: `test/backends/test_herdr_backend.py`

- [ ] **Step 1: Write the failing test**

Add to `test/backends/test_herdr_backend.py` (near the existing `_sanitize_herdr_args` tests):

```python
from cli_agent_orchestrator.backends.herdr_backend import _sanitize_herdr_args


def test_sanitize_allows_env_flag():
    args = ["tab", "create", "--workspace", "w1", "--env", "CAO_TERMINAL_ID=term_x"]
    assert _sanitize_herdr_args(args) == args


def test_sanitize_rejects_env_value_with_newline():
    import pytest
    with pytest.raises(ValueError):
        _sanitize_herdr_args(["tab", "create", "--env", "K=line1\nline2"])
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest --no-cov -p no:cacheprovider test/backends/test_herdr_backend.py -k "allows_env or rejects_env" -v`
Expected: FAIL on `test_sanitize_allows_env_flag` — `--env` is not in `_HERDR_ALLOWED_FLAGS`, so it raises ValueError.

- [ ] **Step 3: Add --env to the allowlist**

In `_HERDR_ALLOWED_FLAGS` (line 53-62), add `"--env",` to the frozenset.

- [ ] **Step 4: Run to verify both pass**

Run: `uv run pytest --no-cov -p no:cacheprovider test/backends/test_herdr_backend.py -k "allows_env or rejects_env" -v`
Expected: PASS — `--env KEY=VALUE` is allowed; the newline value is still rejected by `_SAFE_ARG_RE` (which excludes control chars). If the newline test does NOT fail as expected, the regex admits newlines and must be tightened; note it and stop.

- [ ] **Step 5: Commit**

```bash
git add src/cli_agent_orchestrator/backends/herdr_backend.py test/backends/test_herdr_backend.py
git commit -m "feat(herdr): allow --env flag in arg sanitizer"
```

### Task 8: Inject env natively at tab/workspace create

**Files:**
- Modify: `src/cli_agent_orchestrator/backends/herdr_backend.py` — `create_window:337-366`, `create_session:242-288`, replace `_inject_env_vars:714-770`, keep `_build_extra_env_exports` logic but rename to build pairs
- Test: `test/backends/test_herdr_backend.py`

- [ ] **Step 1: Write a helper to build --env args and test it**

Add test to `test/backends/test_herdr_backend.py`:

```python
def test_build_env_args_includes_identity_and_filters_blocked():
    from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend
    backend = HerdrBackend.__new__(HerdrBackend)  # no __init__ (avoids server spawn)
    pairs = backend._build_env_args(
        terminal_id="term_x",
        session_name="sess-a",
        extra_env={"AWS_REGION": "us-west-2"},
    )
    # Flattened --env KEY=VALUE pairs.
    assert "--env" in pairs
    joined = " ".join(pairs)
    assert "CAO_TERMINAL_ID=term_x" in joined
    assert "CAO_SESSION_NAME=sess-a" in joined
    assert "AWS_REGION=us-west-2" in joined
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest --no-cov -p no:cacheprovider test/backends/test_herdr_backend.py -k build_env_args -v`
Expected: FAIL — `_build_env_args` does not exist.

- [ ] **Step 3: Implement `_build_env_args` and delete the send-text path**

Add:

```python
    def _build_env_args(
        self,
        terminal_id: str,
        session_name: str,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        """Build `--env KEY=VALUE` argument pairs for a create command.

        CAO identity vars first, then operator-forwarded vars filtered with the
        same policy TmuxClient applies to its -e argv (blocked prefixes, byte
        cap). Native --env replaces the former shell `export` injection, which
        removes the command-line injection surface CodeQL flagged.
        """
        from cli_agent_orchestrator.clients.tmux import TmuxClient

        env: Dict[str, str] = {
            "CAO_TERMINAL_ID": terminal_id,
            "CAO_SESSION_NAME": session_name,
        }
        for key, value in (extra_env or {}).items():
            if TmuxClient._is_blocked_env_key(key):
                logger.warning("Dropping forwarded env var with blocked prefix: %s", key)
                continue
            if len(value.encode("utf-8")) >= TmuxClient._MAX_ENV_VALUE_BYTES:
                logger.warning("Dropping forwarded env var %s -- exceeds byte cap", key)
                continue
            env[key] = value

        args: List[str] = []
        for key, value in env.items():
            args.extend(["--env", f"{key}={value}"])
        return args
```

Then in `create_window` (line 354) append `self._build_env_args(terminal_id, session_name, extra_env)` to `args` BEFORE `self._run_herdr(args)`, and DELETE the `self._inject_env_vars(...)` call (lines 363-366). Do the same in `create_session` (line 255 build, delete the inject call at 283-285). Finally delete the now-unused `_inject_env_vars` (714-770) and `_build_extra_env_exports` (772-801) methods. If `_inject_env_vars` was the only writer of `self._pane_cache[terminal_id]`, seed the cache from the create response's `new_pane_id` directly in `create_window`/`create_session` instead (one line: `if new_pane_id: self._pane_cache[terminal_id] = (new_pane_id, time.time())`).

- [ ] **Step 4: Verify no orphan references and run backend suite**

Run: `grep -n "_inject_env_vars\|_build_extra_env_exports" src/`
Expected: no output.
Run: `uv run pytest --no-cov -p no:cacheprovider -q test/backends/test_herdr_backend.py`
Expected: PASS (adapt any test that asserted the old send-text injection — it should now assert `--env` appears in the create args).

- [ ] **Step 5: Live check env actually reaches the child**

On the herdr backend, launch a terminal with `cao launch --env CAO_PROBE=hi`, then in that pane run `echo $CAO_PROBE`. Expected: `hi`. (Reads the process env, not scrollback.)

- [ ] **Step 6: Commit**

```bash
git add src/cli_agent_orchestrator/backends/herdr_backend.py test/backends/test_herdr_backend.py
git commit -m "feat(herdr): inject env via native --env, remove shell-export path"
```

---

## Phase 4 — R4: delete ID-resolution machinery (depends on R2; do last)

### Task 9: Route get_pane_id through a snapshot-backed durable map

**Files:**
- Modify: `src/cli_agent_orchestrator/backends/herdr_backend.py` — `get_pane_id:609`, add a durable map + snapshot refresh
- Test: `test/backends/test_herdr_backend.py`

- [ ] **Step 1: Write the failing test**

```python
def test_get_pane_id_uses_snapshot_map_across_restart(monkeypatch):
    from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend
    backend = HerdrBackend.__new__(HerdrBackend)
    backend._herdr_session = "cao"
    backend._pane_id_map = {"term_a": "w1:p1"}  # durable map
    # A server restart compacts pane_ids; a refresh rebuilds from snapshot.
    def fake_refresh():
        backend._pane_id_map = {"term_a": "w2:p5"}
    monkeypatch.setattr(backend, "_refresh_pane_id_map", fake_refresh)

    assert backend.get_pane_id("term_a") == "w1:p1"       # hit
    backend._pane_id_map = {}                              # simulate stale/empty
    assert backend.get_pane_id("term_a") == "w2:p5"       # miss -> refresh -> hit
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest --no-cov -p no:cacheprovider test/backends/test_herdr_backend.py -k snapshot_map -v`
Expected: FAIL — `_pane_id_map` / `_refresh_pane_id_map` do not exist.

- [ ] **Step 3: Add the durable map + refresh, wire get_pane_id**

Add `self._pane_id_map: Dict[str, str] = {}` in `__init__`. Add:

```python
    def _refresh_pane_id_map(self) -> None:
        """Rebuild terminal_id -> pane_id from a live `api snapshot`.

        Public IDs are stable except across a full herdr server restart, so this
        only needs to run on a miss (or at reconcile time).
        """
        result = self._run_herdr(["api", "snapshot"], check=False)
        if result.returncode != 0:
            return
        try:
            snap = self._parse_herdr_json(result.stdout)
            snap = snap.get("snapshot", snap)
            self._pane_id_map = {
                p["terminal_id"]: p["pane_id"]
                for p in snap.get("panes", [])
                if p.get("terminal_id")
            }
        except (json.JSONDecodeError, KeyError, AttributeError):
            return
```

Add `"api"` to `_HERDR_ALLOWED_SUBCOMMANDS` (line 33-40) so `_run_herdr(["api","snapshot"])` passes the sanitizer. Rewrite `get_pane_id` to prefer `self._pane_id_map`, calling `_refresh_pane_id_map()` once on a miss, then falling back to the existing resolution ONLY if still absent. Keep the old cache path intact for now (reversible).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest --no-cov -p no:cacheprovider test/backends/test_herdr_backend.py -k "snapshot_map or pane_id" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/cli_agent_orchestrator/backends/herdr_backend.py test/backends/test_herdr_backend.py
git commit -m "feat(herdr): snapshot-backed durable pane_id map"
```

### Task 10: Verify no close-replay, then delete obsolete guards

**Files:**
- Modify: `src/cli_agent_orchestrator/backends/herdr_backend.py` (remove `_pane_cache`/`_PANE_CACHE_TTL`), `services/herdr_inbox_service.py` (remove label re-mapping + `_label_still_live` if replay is gone)
- Test: `test/backends/` both suites

- [ ] **Step 1: Live-verify herdr 0.7.5 does NOT replay close history on subscribe**

In an isolated session: open a pane, close it, then open a fresh socket and `events.subscribe` to `pane.closed`. Observe whether a `pane_closed` for the already-closed pane replays. Record the result in this task. **If ANY replay occurs, STOP — keep `_label_still_live` and mark this task blocked.**

- [ ] **Step 2: Remove the TTL cache (only if Step 1 shows no replay)**

Delete `_PANE_CACHE_TTL` (line 115) and `_pane_cache` reads/writes now that `get_pane_id` uses the durable map. Delete the label-based re-mapping block inside `_reconcile` (the "renumbered pane" re-resolution, roughly lines 394-444 in the post-Task-5 file) — stable IDs make renumber-remap dead code. Delete `_label_still_live` and its call sites in the `pane.closed` handler.

- [ ] **Step 3: Update any tests that asserted the deleted behavior**

Remove/adjust tests for `_pane_cache` TTL, label re-mapping, and stale-replay. A `pane.closed` for a genuinely-gone pane must still trigger cleanup — keep that test.

- [ ] **Step 4: Run both scoped suites**

Run: `uv run pytest --no-cov -p no:cacheprovider -q test/backends/`
Expected: PASS. Then `grep -n "_pane_cache\|_label_still_live\|_PANE_CACHE_TTL" src/` → no output.

- [ ] **Step 5: Manual soak**

Open/close sibling tabs repeatedly under a live CAO+herdr session; confirm no live terminal is deleted and no "Terminal not found" spam.

- [ ] **Step 6: Commit**

```bash
git add src/cli_agent_orchestrator/backends/herdr_backend.py src/cli_agent_orchestrator/services/herdr_inbox_service.py test/backends/
git commit -m "refactor(herdr): delete pane-id cache + stale-replay guards (stable IDs)"
```

---

## Phase 5 — R5: kiro output_matched (DECISION)

### Task 11: Decide and record — output_matched vs keep polling

**Files:**
- Modify: this plan (record the decision) and possibly `services/herdr_inbox_service.py`

- [x] **Step 1: Record the decision**

`pane.output_matched` REQUIRES a `pane_id` per subscription. Adding it reintroduces per-pane subscribe + reconnect-on-register churn that Task 3 removed (the second-subscribe reset persists in 0.7.5). Recommendation: **keep the existing `_kiro_supplement_loop` 30s poll** — it is self-contained and does not perturb the broadcast model. Only pursue `output_matched` if kiro permission latency is a measured complaint, and if so, its subscription MUST be folded into the single combined `events.subscribe`, never a second call. Write the chosen option and rationale here; if "keep polling," no code change and this phase closes.

**DECISION (2026-07-23): Keep polling. No code change.** Confirmed the tension is real: `pane.output_matched` requires a `pane_id` (schema `required:[type,pane_id]`), so it cannot ride the broadcast subscription — adding it would reintroduce exactly the per-pane-subscribe + reconnect-on-register churn R1 (Task 3) removed, since herdr 0.7.5 still resets on a second `events.subscribe` (verified live this session). The `_kiro_supplement_loop` 30s poll is self-contained, backend-agnostic, and doesn't perturb the broadcast model. Not worth trading R1's win for a latency improvement no one has reported. Revisit only if kiro permission-prompt latency becomes a measured complaint. Phase 5 closed.

---

## Phase 6 — E: env-survival across restart (CONDITIONAL — gated)

### Task 12: Investigate herdr restart re-spawn behavior

**Files:** none (investigation)

- [x] **Step 1: Determine shell vs agent re-spawn**

Investigated live (2026-07-23) in an isolated `cao-respawn` session: launched a real `claude` agent in a pane (`agent=claude, agent_status=idle`), stopped and restarted the herdr server, and re-read the pane.

**RESULT: fresh SHELL.** After restart the pane returned as `agent=None, agent_status=unknown` with a bare `❯` prompt and the pre-restart scrollback preserved as static text — claude was NOT resurrected. herdr persists the pane's *topology and scrollback*, not the running process. (Consistent with the earlier finding that create-time `--env` also does not survive a restart — the shell is re-spawned without it.)

- [x] **Step 2: Branch on the result**

Fresh-shell path, but with a scope-reshaping caveat the investigation surfaced:

**DECISION (2026-07-23): Do NOT build env-survival now. Low value under current herdr restart semantics.** Because the agent process itself does not survive a herdr server restart (Step 1), there is no running agent consuming the env after restart — the pane is an inert shell until something re-launches an agent in it, and CAO's reconcile treats an agent-less restored pane as a ghost to clean up anyway. So "re-inject env on restart" would be injecting into a shell nothing is using. The env-survival feature only becomes meaningful if/when herdr gains agent-session resurrection across restart (it does not have it in 0.7.5). Recommendation: revisit ONLY if a future herdr release resurrects agents on restart; at that point CAO would persist the env dict in the terminal's SQLite row (it already stores terminal metadata) and re-inject on the R2 snapshot-reconcile rebuild. No CAO code or herdr feature request warranted today. Phase 6 closed.

---

## Sequencing

- **Phases 1, 2, 3 are independent** — implement/merge in any order or parallel.
- **Phase 4 depends on Phase 2** (needs `_fetch_snapshot` / snapshot map). Do it LAST — it deletes battle-tested code; Tasks 9 and 10 are split so "add new path" precedes "delete old path."
- **Phase 5** is a decision after Phase 1.
- **Phase 6** is gated on Task 12's investigation.

Recommended merge order: **R1 → R2 → R3** (independent, high-value), then **R4**, then R5/E as decisions land.

## Test discipline (per CLAUDE.md)

- One `pytest` invocation at a time; serial for scoped runs; never `pkill` a run.
- Scope every run: `test/backends/` covers both the backend and inbox-service suites here.
- Steps that change wire/subprocess behavior include a LIVE herdr check — the entire class of past bugs came from tests using fabricated wire shapes that never occur live. Capture real `api snapshot` / `pane.updated` frames as fixtures before writing parsing code.
