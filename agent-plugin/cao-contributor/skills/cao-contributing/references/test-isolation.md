# Isolating a test from your own CAO state

Reference for the **cao-contributing** skill, expanding the "Testing gotchas" bullet on tests
that pass only on the machine that has an initialised CAO install. The fenced recipe below is
executed by `test/test_cao_contributing_skill_accuracy.py`, which asserts its shape (subshell,
trap, ordering, cleanup) rather than a paraphrase of it — keep it byte-identical when editing.

- **A test that touches CAO's own state can pass only on your machine.** If a code path
  reaches the real database, config dir, or a live session, it will be green on a
  developer box that has an initialised CAO install and red on a clean runner — where the
  tables exist but are *empty*, so the symptom is a missing row rather than
  `sqlite3.OperationalError: no such table: terminals`. The schema itself is no longer the
  variable: the root `test/conftest.py` runs `init_db()` once per session against the
  configured home when that home has no schema at all (`_cao_home_schema`, guarded by
  `test/test_isolated_home_has_schema.py`), so a throwaway `CAO_HOME_DIR` gets empty tables
  instead of no tables. Under `-n` that is once per *worker process*, since each xdist
  worker is a separate process with its own session — they all check and build the same
  home, so the check and `init_db()` run under an exclusive `flock` on
  `$CAO_HOME_DIR/db/.test-schema-init.lock` and the losers see the winner's finished schema
  instead of `table ... already exists`. Mock the store/DB seam explicitly;
  when a test exercises a service function, check what that function calls *today* — a
  rebase can introduce a new unmocked DB write into a path your test already covered.
  Verify by running under a throwaway `HOME` **and** a throwaway `CAO_HOME_DIR` —
  `HOME` alone is not enough, because `constants.py` prefers an exported
  `CAO_HOME_DIR` and derives the database and every other state path from it, so an
  absolute value you already export keeps the initialised store this is meant to
  exclude:
  ```bash
  TMPH=$(mktemp -d)
  ( trap 'rm -rf "$TMPH"' EXIT              # cleans up on every path
    HOME="$TMPH" CAO_HOME_DIR="$TMPH/cao" \
      uv run pytest test/path/to/test_x.py
    rc=$?                                   # not `status`: read-only in zsh
    echo "exit=$rc"                         # pytest's status, not rm's
    exit "$rc" )                            # ...and the block returns it too
  ```
  Keep the trap-in-a-subshell shape, and keep the diagnostic *inside* it. Cleaning up
  with `; rm -rf "$TMPH"` returns `rm`'s status instead of pytest's, so a failing run
  reports success; switching to `&&` fixes the status but leaks the temp directory on
  exactly the failures you wanted isolated. A trailing `echo "exit=$?"` *after* the
  subshell prints the right number but is itself the block's last command, so the block
  returns 0 and a script or agent checking `$?` still reads a failing run as a pass.
