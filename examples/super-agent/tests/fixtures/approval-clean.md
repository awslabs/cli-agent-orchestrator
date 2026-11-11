## Review at `c0de0001`: APPROVE (0 P1, 0 P2, 0 P3)

Thanks. The diagnosis is correct, and the new regression test reproduces the race without timers.

### What I checked

| Area | Result |
|---|---|
| Cause | The test fixture shares one in-memory connection across sessions, so a background reader's rollback can undo the claim's open update. |
| Production | The real store opens one connection per session. The bug is in the test fixture only. |
| New test | It is deterministic: a hook runs the read between the claim's update and its commit. |
| Mutants | Two mutants of the fixture each make the new test fail. The previous P3 on the fixture name is fixed. |
| Stability | 25 runs of the module on this branch merged with current main: 0 failures. |
| Regression | The affected modules pass on the merge with main. CI: all checks pass. |

### Findings

None.
