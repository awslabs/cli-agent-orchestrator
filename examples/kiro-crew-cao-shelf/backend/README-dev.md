# CAO Shelf backend: developer notes

Stdlib-only Python 3.11 or newer. Nothing in `backend/` needs installing.

## Run it

Kiro Crew starts it as `python backend/server.py` and sets the environment. To run it by hand:

```
PORT=9993 KIROCREW_PROXY_SECRET=dev-secret CAO_SHELF_DATA_DIR=<scratch>/data \
  python3 backend/server.py
```

| Variable | Set by | Meaning |
|---|---|---|
| `PORT` | Kiro Crew (or `--port N`) | Port to listen on. Always bound to `127.0.0.1`; any other address is refused. |
| `KIROCREW_PROXY_SECRET` | Kiro Crew | Key for the proxy signature. Empty means every route except `/health` answers 401. |
| `CAO_SHELF_DATA_DIR` | tests and the dev harness only | Where `config.json` and `ledger.json` live. Ignored when `KIROCREW_SPAWNED` is set; the default is `<app>/data`. |
| `CAO_SHELF_CAO_URL` | tests and the dev harness only | Overrides `cao_url`. Ignored when `KIROCREW_SPAWNED` is set. Must be `http://127.0.0.1:<port>`, `localhost` or `[::1]`. |

Health route: `GET /health` returns `{"status":"ok"}`, needs no signature and never calls CAO.
Every other route needs the gateway's signature headers. To call the backend by hand or from a dev
harness, build them with `cao_shelf.auth.signed_headers(secret, method, raw_target, body)`; the
signed target is the exact request line target, query included.

SIGTERM and SIGINT shut it down cleanly (exit code 0). `--port` is the only argument.

## Files

| File | Job |
|---|---|
| `server.py` | Entry point (puts `backend/` on `sys.path`, calls `cao_shelf.http.main`). |
| `cao_shelf/tiering.py` | Pure: tiers, roles, orphans, counts, ordering, change signature. |
| `cao_shelf/history.py` | Pure: closed helpers from inbox rows, ledger merge. |
| `cao_shelf/cao.py` | Read-only CAO client (loopback only, no redirects, 4 MiB cap, timeouts). |
| `cao_shelf/poller.py` | Poller thread, cadence classes, caches, revisions. Reads only. |
| `cao_shelf/actions.py` | The only module that writes to CAO: ask, answer, start, stop plan and execute. |
| `cao_shelf/http.py`, `auth.py` | Server, routes, signatures (v1 and v2 both required; v2 has a nonce cache). |
| `cao_shelf/config.py`, `ledger.py` | `data/config.json` and `data/ledger.json`, both mode 0600; the ledger header holds the CAO address and a ledger of another address is renamed aside. |
| `cao_shelf/prompts.py`, `text.py` | Prompt tail cleaning and the recognised yes/no shapes (`mock_cli` provider only); `text.py` removes bidi, zero-width and C1 characters. |

## Checks (run from the CAO repository root)

`make check-backend` runs these (from the example folder). `uv run`
supplies pytest, jsonschema, ruff, black, isort and mypy. Inside the example folder the same tests
run as `uv run --project ../.. python -m pytest tests -q`.

```
uv run python -m pytest examples/kiro-crew-cao-shelf/tests -q
uv run python -m pytest examples/kiro-crew-cao-shelf/tests/test_cao_shelf_contract.py -q -k "shelf or config or health or signature or error"
uv run python -m pytest examples/kiro-crew-cao-shelf/tests/test_cao_shelf_stop.py -q
uv run ruff check examples/kiro-crew-cao-shelf/backend examples/kiro-crew-cao-shelf/tests
uv run black --check examples/kiro-crew-cao-shelf/backend examples/kiro-crew-cao-shelf/tests
uv run isort --check-only examples/kiro-crew-cao-shelf/backend examples/kiro-crew-cao-shelf/tests
(cd examples/kiro-crew-cao-shelf && uv run mypy --strict backend)
```

Test servers use only the loopback ports 9991 to 9995 and pick the first free one. Tests that
need no socket use an in-process fake CAO (`tests/cao_shelf_support.py`).

## Behaviour worth knowing

* `revision` ignores the clock entirely (`generated_at`, `snapshot_age_seconds` and every
  `idle_seconds` are left out of the change signature). Tier, `warm` and `poll_class` carry the
  thresholds the clock can cross, so a quiet fleet keeps its revision and gets the stub; the page
  computes ages from `last_active` itself.
* A history request (drawer or "All history") re-reads an inbox whose cache is older than 15 s
  (`HISTORY_OPEN_MAX_AGE`); the background poller still re-reads at most once per 5 minutes and
  only when the conductor's `last_active` moved. "All history" reads at most 4 sessions per call,
  oldest cache first.
* A failed Start returns `cao_error` with `detail.cao_status` and a fixed message; CAO's text is
  never copied.
* `GET /health` of CAO is read at start, after an outage and every 5 minutes (not every minute):
  the proposal's request budget of 2.75 requests per tick leaves no room for it.
* Closed-helper history is read four sessions per tick; `history_total` stays `null` until every
  session was read once.
* `GET /api/status` reports `cao_error` as the error code string or `null`.
* The ledger also keeps `last_status` for each helper (a status word, never text).
