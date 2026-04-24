# TSK-080 — Task 9 follow-up: terminal_service test migration

## Files touched
- `test/services/test_terminal_service_full.py`
- `test/services/test_terminal_service_coverage.py`
- `test/services/test_plugin_event_emission.py`
- `spikes/TSK-080-result.md`

## Migration summary
- `test/services/test_terminal_service_full.py`: 17 decorator patches + 0 setattr + 4 assertion references migrated.
- `test/services/test_terminal_service_coverage.py`: 10 decorator patches + 0 setattr + 8 assertion references migrated.
- `test/services/test_plugin_event_emission.py`: 6 decorator patches + 0 setattr + 0 assertion references migrated.

## Tests
- targeted (4 services tests): 65 pass / 0 fail
- full (excl. e2e): 1107 pass / 43 fail — must equal 43
