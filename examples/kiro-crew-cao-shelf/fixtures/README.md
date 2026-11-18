# Fixtures

Anonymised, generated data for the CAO Shelf tests. Every name, id and time is invented
(`payments-fix`, `coder-1`, ...). The response shapes were recorded from a scratch `cao-server`
and from read-only key sets of a live 2.5.4 server; see [SPIKE.md](../SPIKE.md).

* `cao/quiet/`, `cao/busy/`: what CAO returns. `sessions.json` is `GET /sessions`;
  `session.<name>.json` is `GET /sessions/cao-<name>`; `terminal.<id>.json` is `GET /terminals/<id>`
  (the only place `caller_id` appears); `inbox.<conductor id>.json` is the conductor's
  `GET /terminals/<id>/inbox/messages?limit=100&order=desc`; `clock.json` is "now" for the tests.
* `expected/`: what the backend must answer. `quiet.shelf.json` and `busy.shelf.json` match
  `../schemas/shelf.schema.json`; `<day>/history.<name>.json` match `../schemas/history.schema.json`.
* `_gen/make_fixtures.py`: the generator and the reference implementation of tiering. Regenerate with
  `python3 fixtures/_gen/make_fixtures.py`. Changing it is a shared contract change: `schemas/` and `api-contract.md` move with it.

The quiet day has 12 sessions, 20 terminals, all idle: 0 needs you, 0 live, 3 parked, 9 stale, 20 closed
helpers. The busy day has 4 processing, 1 waiting, 15 idle: 1 needs you, 1 live, 1 parked, 9 stale.
