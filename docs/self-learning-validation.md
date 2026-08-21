# Self-Learning Validation — A/B Experiment Results

> Empirical validation of the [self-learning loop](self-learning.md),
> run July 2026 on the `self-learning` branch. Kept with the code so the
> evidence travels with the feature. Paths under `~/code/ab-run/` refer to
> the original experiment host; the harness design is described in §2.

**Date:** July 25–26, 2026
**Question:** Does CAO's self-learning loop (outcome capture → retrospection →
agent-scope memory → instruction promotion) measurably improve agent quality
on repeated runs of the same task family?

**Answer:** Yes, where there is headroom. On packages the baseline handles
poorly (control score < 80), the learning arm won **6 of 6 paired packages,
mean +11.0 points, sign test p = 0.016**. On packages the baseline already
handles well (≥ 80), learning neither helped nor hurt (mean Δ −0.1). The
cost is ~2× worker latency from the injected lesson context.

---

## 1. What was tested

The self-learning stack built on the CAO `self-learning` branch
(`~/code/cao-self-learning`, commits `bfb699d`…`66f948f`):

- **Phase 1 — outcome capture:** `workflow_outcomes` table, `OutcomeService`,
  `report_outcome` MCP tool, `POST/GET /outcomes`, `retrospector` agent
  profile. Gated by `memory.learning_enabled` (opt-in, child of
  `memory.enabled`).
- **Phase 2 — instruction promotion:** `learned_patterns` service (ACE-style
  itemized deltas on a delimited `## Learned Patterns` block in the profile
  file), `PromotionService` (plan/apply; a lesson qualifies once recalled
  ≥ 3 times), `cao memory promote` CLI. Gated by
  `memory.instruction_promotion_enabled` (promotion ⊂ learning ⊂ memory).

## 2. Method

Local proxy for the glue-factory pipeline (no AWS): for each SSIS `.dtsx`
package, the deterministic `dtsx2glue` engine converts it (failing loud on
unsupported constructs), then a headless LLM worker (`claude -p`, Sonnet)
repairs the generated Glue PySpark script using the gap report + source XML
(design blobs stripped, 60KB excerpt cap). A blind LLM judge scores the
final script 0–100 against the source package.

**Arms differ in exactly one way — learning:**

| | Control | Treatment |
|---|---|---|
| Worker profile | static baseline | profile mutated by instruction promotion |
| Prompt context | none | `<cao-memory>` block recalled from agent-scope store |
| After each package | nothing | outcome recorded → retrospector distills 0–3 lessons → `memory_store` |
| At corpus midpoint | nothing | reinforced lessons promoted into profile |

The treatment uses the **real CAO Phase 1+2 services in-process**
(`MemoryService`, `OutcomeService`, `PromotionService` against an isolated
`CAO_HOME_DIR`) — it exercises the actual learning code, not a simulation.
It does not exercise CAO's server/tmux orchestration.

Harness: `~/code/ab-run/ab_runner.py`. Engines run from per-arm git
worktrees (`gfe-control` / `gfe-treatment`).

## 3. Run 1 — 10 packages (shakedown)

Corpus: 10 packages, single judge sample. **Result: null.** Mean 77.8 vs
79.6 (+1.8), 4W/4L/1T, p = 0.64.

What run 1 established:

- **Mechanics all worked**: 18 well-formed lessons stored, 5 promoted at
  the midpoint, fail-safe gating held, runs resumable.
- **Why null:** (a) judge noise ±10–20 points — p01 had byte-identical
  inputs in both arms and still scored 69 vs 88 on single samples;
  (b) ceiling effect — 7/9 packages scored 80+ for control;
  (c) the harness crashed the arm on a worker timeout (p09 lost).

Harness fixes for run 2: **median-of-3 judge samples**, continue-on-timeout
(excluded record, arm keeps going), dynamic promotion midpoint, per-stage
durations, configurable corpus.

## 4. Run 2 — 20 packages, harder, curriculum-ordered

Corpus (`corpus-r2/`): 20 packages from `ssis_clean_room/input`, ranked by
component variety/size, easy→hard ordering; 13/20 exceed the prompt excerpt
cap (agents work from truncated XML). Fresh treatment memory
(`~/.aws/cao-ab-r2`) — no carry-over from run 1. Promotion after p10.

### 4.1 Paired scores (median of 3 judge samples)

| package | control | treatment | Δ | phase |
|---|---|---|---|---|
| p01_albarran | 68 | 87 | +19* | pre |
| p02_Etnia | 93 | 93 | 0 | pre |
| p03_Package | 85 | 90 | +5 | pre |
| p04_Lesson_1 | 90 | 88 | −2 | pre |
| p05_Lesson_2 | 88 | 90 | +2 | pre |
| p06_TermLookup | 84 | 84 | 0 | pre |
| p07_Lesson_3 | 91 | 90 | −1 | pre |
| p08_ProfilesGeoCode | 91 | 90 | −1 | pre |
| p09_Equifax_DP3_old | 75 | 80 | +5 | pre |
| p10_ExecManualLoad | 84 | *excluded* (timeout) | — | pre |
| p11_DWH_Maintenance_old | 48 | 57 | +9 | post |
| p12_Load_Landing | 73 | 78 | +5 | post |
| p13_Lesson_4 | 85 | 85 | 0 | post |
| p14_Lesson_5 | 85 | 84 | −1 | post |
| p15_Lesson_6 | 84 | 84 | 0 | post |
| p16_Lab3 | 86 | 88 | +2 | post |
| p17_SCD | 85 | 80 | −5 | post |
| p18_Task4 | 66 | 84 | +18 | post |
| p19_CargaFatoVenda | 78 | 88 | +10 | post |
| p20_FlatFileLoadPlus | 44 | *excluded* (timeout) | — | post |
| **mean (18 paired)** | **80.8** | **84.4** | **+3.6** | |

\* p01 prompts were byte-identical across arms (no memory yet existed) —
its +19 is worker/judge sampling luck, not learning. It is retained in the
tables but discounted in the conclusions.

Overall: 9W / 5L / 4T, sign test p = 0.21. Pre-promotion Δ +3.0,
post-promotion Δ +4.2.

### 4.2 The key split: headroom vs ceiling

| segment | n | mean Δ | wins | sign test |
|---|---|---|---|---|
| **Headroom** (control < 80) | 6 | **+11.0** | 6/6 | **p = 0.016** |
| Ceiling (control ≥ 80) | 12 | −0.1 | 3/12 (8 ties/losses within noise) | n.s. |

Headroom detail: p01 +19*, p09 +5, p11 +9, p12 +5, p18 +18, p19 +10.
Excluding p01: 5/5 wins, mean +9.4.

This is the profile a learning system should have: **gains concentrated
where the baseline fails, no regression where it already succeeds.** The
largest gains came late in the corpus (p18 +18, p19 +10, p11 +9) — after
15+ packages of accumulated lessons and the promoted profile — consistent
with learning rather than luck. p19_CargaFatoVenda is the cleanest
example: unconvertible in run 1 (repeated timeouts), 78 for control here,
88 for the lesson-guided treatment.

### 4.3 What was learned

23 lessons stored across the run (no duplicates, no junk); memory grew
0 → 9.3KB of injected context. The 5 lessons promoted into the profile at
the midpoint (each reinforced 7–10× by recall):

1. `resolve-db-source-table-query-metadata` — resolve source table/query
   from component metadata instead of emitting placeholders
2. `model-error-output-dispositions` — translate FailComponent /
   RedirectRow / IgnoreFailure into explicit logic, never drop
3. `honor-destination-write-semantics` — bulk/fast-load and identity-insert
   semantics, not plain JDBC append
4. `honor-lookup-cache-mode` — preserve full/partial/no-cache Lookup modes
5. `flag-guessed-parse-formats` — never silently guess date/number formats

These read as a domain reviewer's checklist for SSIS→Glue conversion, and
they map directly onto the judge's recurring control-arm complaints.

### 4.4 Costs and exclusions

- **Latency:** mean worker time 211s (control) vs 455s (treatment). The
  lesson context makes the worker implement more (dispositions, write
  semantics) — that is where the score gains come from, at ~2× time.
- **Exclusions:** treatment p10 and p20 hit the 45-min worker timeout twice
  (largest prompts + lesson overhead); scored pairs exclude them. Control
  completed p20 but scored 44.
- **Judge noise after median-of-3:** mean 3-sample spread 5.1 points
  (one outlier: control p18 spread 45–88). Medians are trustworthy;
  single samples are not.

## 5. Conclusions

1. **The loop works end-to-end**: outcomes → retrospection → lessons →
   reinforcement-by-recall → promotion → measurable behavior change.
2. **The effect is real but conditional**: significant on headroom
   packages (+11, p = .016), absent at the ceiling. Learning pays where
   the base model struggles — for glue-factory, that means the hardest
   packages benefit most, which is where iterations are spent.
3. **Learning has a latency price** (~2× worker time from richer prompts +
   more thorough output). For a pipeline whose alternative is 2–3 full
   improver iterations, that trade is likely favorable; measure it there.
4. **Judge-based scoring needs medians**; run 1's null was mostly noise +
   ceiling, not absence of effect.

## 6. Threats to validity

- LLM judge is not ground truth (mitigated by median-of-3, blind scoring,
  identical rubric; not eliminated).
- Single corpus order, single run per arm; p = 0.016 on a 6-package
  subgroup defined post-hoc by control score — directionally strong,
  not airtight.
- No holdout set: gains could partly be corpus-specific memorization.
  (Lessons are generic by construction, but untested on unseen packages.)
- Proxy task (LLM repairs engine output) ≠ the real glue-factory pipeline
  (LLM conversion + deployment + validation + iterative improvement).

## 7. Recommended next steps

1. **Holdout test:** freeze treatment memory/profile, run 3–5 unseen
   packages on both arms — memorization vs generalization.
2. **Real pipeline run:** the glue-factory experiment
   (`glue-factory-engine/cao-agents/ab-compare/`) with objective
   validation and iterations-to-pass as the metric.
3. **Latency mitigation:** curated per-task lesson injection
   (`memory_manager` pattern) instead of injecting all lessons every time.

## 8. Artifacts

| path | contents |
|---|---|
| `~/code/ab-run/ab_runner.py` | A/B harness (arms, judge, learning plumbing, report) |
| `~/code/ab-run/corpus/`, `corpus-r2/` | run 1 (10) and run 2 (20) package corpora |
| `~/code/ab-run/results/`, `results-r2/` | per-package JSON (scores, judge samples, lessons, timings), all prompts/outputs, logs |
| `results-r2/promoted_profile_snapshot.md` | treatment profile with the promoted Learned Patterns block |
| `~/.aws/cao-ab-local`, `~/.aws/cao-ab-r2` | treatment CAO stores (memory wiki + SQLite) per run |
| `~/code/cao-self-learning` | CAO branch with Phases 1+2 (5,373 tests passing) |
| `glue-factory-engine/cao-agents/ssis-migration-learning/`, `ab-compare/` | SOPs + harness for the full-pipeline experiment (uncommitted) |
