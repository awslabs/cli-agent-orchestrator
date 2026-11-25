## Review at `c0de0002`: APPROVE (0 P1, 0 P2, 1 P3)

Thanks for this follow-up. The list of removed tools is now complete and correct. One sentence needs a small correction.

### What I checked

| Area | Result |
|---|---|
| Inventory | I compared every tool definition in the old and the new release. The new release removes exactly the tools in the new comment. |
| Remaining tools | The tools the comment keeps are still present, with the same tags. |
| Scope | Only the comment changes. The branch merges cleanly with current main. CI: all checks pass. |

### Findings

1. **P3: "None is selectable" is not correct for one tool on the old release** (`schema.py:211-212`).
   On the old release this tool has the `write` and `shell` tags, so a helper with a shell or
   write grant can select it when the feature that registers it is on.
   Suggested wording:
   ```
   # Of these tools, only one had a tag that we render, and only on the old
   # release, when its feature flag enables it.
   ```

### Notes (not blocking)

1. The gap in Finding 1 is not caused by this PR. The new release removes the tool, so only the
   old release is affected.
