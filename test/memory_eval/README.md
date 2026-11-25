# Synthetic memory-evaluation harness

This hermetic corpus has 60 invented Atlas-project notes and gold questions. It
measures keyword and natural-language precision at 1 and 3, multi-hop full and
coverage, whether a current note ranks above a stale note, tokens per recall,
and invisible notes. It runs the production `MemoryService` in-process against
a new temporary home; it never reads a configured CAO memory home.

The 60-note P0 corpus currently records these legacy baselines (rounded to two
decimal places):

| Arm | kw p@1 | kw p@3 | NL p@1 | NL p@3 | multi-hop full | multi-hop coverage | current first | tokens/recall | invisible |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `default` | 0.48 | 0.74 | 0.56 | 0.59 | 0.00 | 0.50 | 0.40 | 3825.54 | 7 |
| `bm25` | 0.56 | 0.74 | 0.56 | 0.59 | 0.00 | 0.50 | 0.00 | 3825.54 | 7 |

For every arm, `current first` is a strict measure: a current note counts only
when it ranks first with the wiki listing sorted in both ascending and
descending path order, so a stale/current tie does not count. Tokens per recall
can vary slightly with filesystem listing order.

Run the fixture evaluation with a fresh directory below the system temporary
directory:

```bash
uv run python -m test.memory_eval.harness --home "$(mktemp -d)/memory-home"
```

Later phases add an explicitly named entry to `ARMS` in `harness.py`. An arm
has a `settings` object, written to that arm's temporary `CAO_HOME_DIR`, and
its `recall` arguments. The harness starts a fresh process and home for every
arm, so settings cannot leak between measurements. Add only relation-based
assertions or deterministic corpus counts to `test_eval_baseline.py`: a phase
PR updates its own arm's target, never the legacy `default` arm's target. P1a
owns visibility, P3 owns default ranking, and P4 owns relationship expansion.

## Future operator-store evaluation

An operator-store evaluation runner is planned under #1329 but does not exist
yet. When implemented, it must evaluate only a read-only copy in a newly
created temporary directory and retain the same temporary-home refusal used by
this harness. Never point an evaluation process at the live CAO home.

The synthetic fixture runner above is the CI baseline and does not seed or
inspect operator data.
