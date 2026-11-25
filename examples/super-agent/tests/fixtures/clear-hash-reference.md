This PR applies the two P3 fixes from [my #101 review](https://github.com/your-org/your-repo/pull/101#pullrequestreview-1). Both texts now agree with the code. I found no problems (nil), so I approve.

### Other reviewers

[other-reviewer (review bot)](https://github.com/your-org/your-repo/pull/102#issuecomment-2):

- **Reinstall note: Confirmed, with one correction.** A copy that an earlier install saved does take precedence over the bundled profile, so an install by path is the correct way to get the new prompt. But the function that reads the local store first is `read_installed_profile` (`profiles.py:616`, local store at `:635-638`).
- **Docstring: Confirmed.** It now ends with "agent > federated", the same order as `SCOPE_PRECEDENCE`.
- **CI green: Confirmed.** 27 of 27 checks pass at this head.

### What I checked

- The full diff against main (2 files, +2/-2). The branch is one commit behind main. That commit (#99) does not touch these files, and `git merge-tree` is clean.
- `CHANGELOG.md:17`: the new last sentence is, word for word, the fix text in my #101 P3-1. The code that this text depends on has no change since that review. I checked each claim again:
  - "That copy takes precedence": the lookup reads the local store first (`profiles.py:635-638`) and the built-in store last (`:669`).
  - "this edit does not change your ids": the id takes the profile-set digest (`freeze.py:182-203`). For a user with a local copy, a change to the bundled file changes neither of them.
  - "install the bundled file by its path ... Then re-approve": the install by path replaces the saved copy. My #101 probe showed the new prompt after this step.
- CI: 27 of 27 checks pass.

### Notes

- Optional, from my #101 notes: the lesson bullet still says twice that agents find lessons through recall or a promoted block (`CHANGELOG.md:11` and `:13-14`).
