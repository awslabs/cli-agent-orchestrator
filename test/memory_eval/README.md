# Synthetic memory-evaluation harness

This hermetic corpus has 60 invented Atlas-project notes and gold questions. It
measures keyword and natural-language precision at 1 and 3, multi-hop full and
coverage, whether a current note ranks above a stale note, tokens per recall,
and invisible notes. It runs the production `MemoryService` in-process against
a new temporary home; it never reads a configured CAO memory home.

Run the fixture evaluation with a fresh directory below the system temporary
directory:

```bash
uv run python -m test.memory_eval.harness --home "$(mktemp -d)/memory-home"
```

Later phases add an explicitly named entry to `ARMS` in `harness.py`, then add
only relation-based assertions or deterministic corpus counts to
`test_eval_baseline.py`. P1a owns visibility, P3 owns default ranking, and P4
owns relationship expansion.

To measure an operator store, first copy it into a newly created temporary
directory, make that copy read-only, and point the separate operator evaluation
runner at the copy. Never point an evaluation process at the live CAO home:

```bash
scratch="$(mktemp -d)"
cp -R "$CAO_HOME_DIR/memory" "$scratch/memory"
CAO_HOME_DIR="$scratch" HOME="$scratch/fake-home" <operator-eval-command>
```

The production-copy runner must retain the same temporary-home refusal used by
this harness. The synthetic fixture runner above is the CI baseline and does
not seed or inspect the copied operator data.
