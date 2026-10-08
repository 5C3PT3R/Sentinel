# HANDOFF — Sentinel

_Last updated: 2026-10-08 (M4 built, not run against a live LLM)_

## 1. What the project is

**Sentinel** is an autonomous root-cause-analysis (RCA) system for e-commerce business metrics. Every day it ingests a batch of event data, checks the data quality, detects which KPIs moved significantly, works out *why* by decomposing a metric tree and drilling through dimensions, has a Critic agent challenge the conclusion, and writes an evidence-backed report with a confidence score and an escalate yes/no.

It ships with its own benchmark: a synthetic data simulator with injected, known root causes, used to show that Sentinel beats a plain "LLM + pandas" agent.

Source of truth: [PRD.md](PRD.md). Read it in full before writing code.

## 2. The idea (why it exists)

- Dashboards show *that* revenue dropped. They don't show *why*. Analysts spend hours slicing by platform, region and payment method to find out.
- "Chat with your CSV" LLM tools are unreliable for this. They invent numbers, stop at the first plausible story and don't check alternatives.
- Sentinel's approach:
  1. **Deterministic core.** Python/DuckDB computes every number.
  2. **The LLM only reasons and writes.** It picks which branches to explore and turns the findings into text, citing results by `evidence_id`.
  3. **Adversarial Critic** with veto power.
  4. **Measurable.** Ground-truth scenarios plus an eval harness against baselines.

Core promise: *"Something changed. Figure out what happened, why it happened, and tell me whether I need to act."*

### Pipeline

```
Ingestion → Data Quality (PASS/WARN/BLOCK) → Detection → Investigation (LLM + tools)
   → Critic (≤2 revise loops) → Reporting → Delivery (Streamlit / stored report / webhook)
```

A plain-Python state-machine Orchestrator drives the stages, enforces budgets and persists everything under `runs/<run_id>/`.

### Invariants (non-negotiable, PRD §4)

1. The LLM never computes numbers.
2. Every claim cites an `evidence_id`. A claim without one is dropped.
3. If Data Quality returns `BLOCK`, there is no analysis, only a data-quality alert.
4. The Critic can veto. A rejected cause appears only as "considered and rejected".
5. Every run is reproducible (seed plus cached LLM responses).
6. Contributions sum to ≤ 100%, and the "unexplained" remainder is shown explicitly.

## 3. What is done

| Item | Status |
|---|---|
| PRD (`PRD.md`) | ✅ Written |
| Repo / git init | ✅ `git init` done, `.gitignore`, no commits yet |
| M0 Skeleton | ✅ layout, `pyproject.toml`, models, CLI stub, config yamls, CI workflow; `ruff` + `pytest` pass locally |
| M1 Simulator | ✅ `sentinel/simulator/{generator,injector}.py`, 28 scenarios in `scenarios/*.yaml`, `sentinel simulate` (writes parquet per table/day, `ground_truth.json`, `revenue.png`); 5 tests pass, ruff clean |
| M2 Ingestion + DQ | ✅ `sentinel/ingestion/__init__.py` (per-day parquet → DuckDB views, history lookup), `sentinel/quality/__init__.py` (`check_day(data_dir, day) -> DQReport`); `tests/test_quality.py` checks every sim day against ground truth (PASS/WARN/BLOCK all match; skipped if `data/sim` is absent); see DECISIONS D18–D19 |
| M3 Detection + tools | ✅ `analysis/{cube,metric_tree,drilldown,tools}.py`, `detection/__init__.py`, `evidence/__init__.py`; `tests/test_analysis.py` (LMDI/segment reconciliation exact, Baseline C top-3 ≥ 70%, tools smoke); see DECISIONS D20–D23 |
| M4 Investigation Agent | ⚠️ Built and tested with a scripted fake LLM only: `llm/__init__.py` (cache, token tally), `agents/investigation.py` + `prompts/investigation.md`, `evidence/verifier.py`, `Tools.segment_contribution`; `tests/test_investigation.py`. **Never run against the real API (no `ANTHROPIC_API_KEY` in this environment)**; see DECISIONS D24–D25 |
| M5–M8 | ❌ None |
| `DECISIONS.md` | ✅ Seeded (D1–D4, D6, D8, D9, D16, D17) |
| Scenarios | ✅ 28 (12 business, 8 data-issue, 2 Simpson/mix-shift, 2 two-cause, 4 no-anomaly incl. sale day and sub-threshold wobble) |

**In short, M0–M3 are done; M4 is built but unproven live; the next work is M5 (Critic + Reporting) after a live smoke test of M4. Nothing is committed yet. `sentinel run` is still a stub; DQ isn't wired into the CLI.**

## 4. What is left (everything, in PRD milestone order)

| Milestone | Scope | Acceptance |
|---|---|---|
| **M0 Skeleton** | Repo layout (PRD §13), `config/*.yaml`, Pydantic models (§14), Typer CLI stub, `DECISIONS.md`, CI (pytest + ruff), `git init` | CI green |
| **M1 Simulator** | `simulator/generator.py` + `injector.py`, ≥ 25 YAML scenarios, `ground_truth.json` | `sentinel simulate --scenarios scenarios/` works; anomalies visible in a plot |
| **M2 Ingestion + DQ** | DuckDB load, schema diff, null/row-count/freshness/dup/range/category-drift checks | Schema-change and tracking-bug scenarios → BLOCK/WARN; clean days → PASS |
| **M3 Detection + analysis tools** | Baselines, robust-z detector, metric tree (log-diff decomposition), Adtributor-style drill-down, the 7 tools in §6.2, evidence store | Baseline C top-3 ≥ 70% on single-cause scenarios; contributions reconcile within 1% |
| **M4 Investigation Agent** | `llm/client.py` (caching + cost logging), tool-use loop, budgets (25 calls) | Hypotheses carry evidence ids; no numbers outside tool output |
| **M5 Critic + Reporting** | Six critic checks (§8), confidence computed in code, numeric-hallucination verifier, MD + JSON report | Critic fixes ≥ 50% of Investigation's wrong Simpson's-paradox and traffic answers; reports pass the verifier |
| **M6 Baselines + eval** | Baseline A (single agent + pandas), No-Critic, Stats-only; `sentinel eval` → `results.csv` + `report.md` | Full metrics table (§10.2) for all systems |
| **M7 UI + scheduling** | Streamlit (run list, report, click a claim to see its query and result, eval dashboard), daily scheduler, optional Slack webhook | — |
| **M8 Real data + write-up** | One public dataset adapter (Olist or GA4 sample), README with architecture, results and limitations | — |

Definition of done is in PRD §17.

## 5. Changes in the last session

- `PRD.md` was authored (outside this tool, or in an earlier session with no saved transcript or memory).
- No code changes. No earlier Claude Code session history exists for this folder.

## 6. Changes in this session

- Read `PRD.md` end to end.
- Confirmed the workspace contains only `PRD.md` (no code, no git, no earlier memory).
- Created this `HANDOFF.md`.
- Completed M0: `git init`, PRD §13 layout, `pyproject.toml` (ruff, pytest, `sentinel` entry point), Pydantic models in `sentinel/models/__init__.py` (with `RejectedHypothesis`), Typer stubs (`simulate`, `run`, `eval` exit 1 "not implemented"), `config/{settings,schema,metric_tree}.yaml`, `DECISIONS.md`, `.github/workflows/ci.yml`, 2 smoke tests.
- Verified with `python -m ruff check .` and `python -m pytest` (2 passed). Note `ruff` isn't on PATH here; use `python -m ruff`.
- Not done: no commit made, CI workflow never run remotely, `settings.yaml` isn't loaded by any code yet.

## 6b. M1 notes (read before M2/M3)

- Run: `python -m sentinel.cli simulate --out data/sim` (180 days, 10k sessions/day, ~15s, ~90MB; `--sessions`, `--days`, `--seed` override). Start date 2026-01-01; scenarios run 2026-02-10 to 2026-06-25, one per 5 days; sale days 2026-03-14, 05-23, 06-20 (all Saturdays, built into the data, not scenarios).
- Tables: `sessions` (+`category`), `events`, `orders`. `events.payment_method` is set only on payment_* events; `sessions` has no payment_method.
- `ground_truth.json` fields: id, date, type, category (business/data_issue/none), expected_root_cause, expected_filters (list, two for two-cause), expected_dq_status, expected_detect, true_revenue, counterfactual_revenue, effect_revenue_delta. See DECISIONS D7, D10-D12.
- Known weak spots: the data-issue scenarios (S13-S20) don't change true revenue, so only DQ can catch them. S13/S14 tracking bugs are subtle (missing events vs orders). The 28 scenarios share one dataset, so earlier scenario days sit inside later baselines (realistic contamination, robust baseline should cope). Per-segment true contribution isn't stored. I did not eyeball every scenario's effect; only S01, S02, S07, S23 deltas and the revenue plot were checked (all business scenarios show -3% to -42% except S08 -6.7%, S12 -7%, S06 -8%).
- The `ruff` warning-free state needs `python -m ruff`; bash heredocs with very long content failed in this shell, use the Write tool for large files.

## 6c. M3 notes (read before M4)

- Everything reads from a per-day **cube** (`analysis/cube.py`: all dims x S/A/C/P/O/R counts, cached). `Tools(data_dir, store)` in `analysis/tools.py` exposes get_metric, decompose_metric, drill_down, compare_distributions, check_traffic, get_events, correlate, plus `find_causes` (automatic tree walk + drill-down = Baseline C). Each returns `{evidence_id, query, result, computed_at}` and stores Evidence. Dates are ISO strings, baseline is `"weekday"` or `"trailing7"`.
- Not done: tool JSON schemas for the LLM (M4); `sentinel run` still a stub, so DQ, detection and tools are not wired into the CLI or an orchestrator; `Tools.find_causes` results contain plain dicts that may hold numpy scalars (fine for `store.save`, check if you add other serializers).
- Baseline C: 10/14 exact, 12/14 overlap on single-cause scenarios (D23). Weak spots: scenarios with a true effect under ~6% (noise floor), S05, and the second cause in two-cause scenarios (S23 misses paid-traffic drop, S24 misses BR). Hypothesis `contribution_pct` should come from `find_causes` `contribution` (product of tree shares and segment shares, capped at 1).
- Detection needs a clean data day; schema-change days (S15/S16) break the cube, so always run `check_day` first and stop on BLOCK (invariant 3).
- Test suite takes ~40s (two full sweeps need data/sim).

## 6d. M4 notes (read before M5)

- Entry point: `investigate(tools, client, day, anomalies, baseline="weekday", budget=None) -> InvestigationResult` (hypotheses, rejected_alternatives, unexplained_pct, tool_calls, budget_exhausted, dropped). Get anomalies from `detect(data_dir, day, store)` with the same `EvidenceStore` as `Tools`.
- To try it live: set `ANTHROPIC_API_KEY`, then `investigate(Tools("data/sim"), LLMClient(), date(2026,4,1), detect(...))`. Check that `model: claude-sonnet-5-5` and `temperature: 0` are accepted by the API; this has not been tried. The prompt and tool schemas are untested against a real model, so expect to tune them.
- "No numbers outside tool output" is enforced by the verifier on statements only; M5's report writer needs the same check on the full report text.
- Acceptance "hypotheses carry evidence ids; no numbers outside tool output" is proven only for scripted inputs. Real accuracy vs Baseline C is an M6 question.
- `sentinel run` is still a stub; no orchestrator exists yet (DQ -> detect -> investigate -> critic -> report).

## 7. Places to improve (PRD gaps and risks to settle before or while building)

Record each resolution in `DECISIONS.md`, as the PRD instructs.

### Analytical method
1. **Log-difference decomposition breaks at the edges.** If Δln(parent) ≈ 0 (the parent is flat while its children move in opposite directions), the result divides by zero. A child at 0 makes ln undefined. Define a fallback, such as a "no material change" cut-off or a symmetric/LMDI-style weighting.
2. **The metric tree is incomplete.** `conversion_rate = add_to_cart × checkout_start × payment_success` skips the view→ATC and checkout_start→payment_attempt steps, and it mixes session-level and attempt-level rates. Define every rate's numerator and denominator explicitly in `metric_tree.yaml` so the product holds exactly.
3. **The baselines are statistically weak.** "Same weekday over the last 4 weeks" gives n = 4, so MAD is unstable and can be 0 (z = ∞). Add a MAD floor, for example `max(MAD, k·median)`, or pool more weeks. Also define "more conservative baseline" precisely (the smaller |z|?).
4. **Contribution chaining (§7.4)** multiplies shares across additive (drill-down) and multiplicative (tree) steps. Specify how the two kinds compose, or the "≤ 100%" reconciliation can silently drift.
5. **Drill-down** recursion is greedy (top segment only), so it can miss two-cause scenarios. Consider keeping the top-k at each level.

### Data and scale
6. **The simulator's default size is too big for a laptop.** 180 days × 50k–200k sessions/day comes to roughly 9M–36M sessions and several hundred million events. Default to about 5k–20k sessions/day for tests and eval, and keep the large size as an option.
7. The **late-data and schema-change scenarios** need per-day file snapshots and an "arrival time" concept in ingestion. The PRD doesn't specify either.
8. The `schema.yaml` registered schema is listed under v2 (§9.2) but the DQ agent needs it in M2. Move it to M0/M2.

### LLM and agents
9. **The cache key for reproducibility** needs a definition: hash of model + system prompt + messages + tools + temperature. Set temperature to 0.
10. **The Critic's budget is tight.** It has 10 tool calls, runs 6 checks per hypothesis, and must also run an alternative search. With 2+ hypotheses it will run out. Decide which checks are pure code (traffic, magnitude, DQ overlap, calendar) and which need the LLM.
11. **The numeric-hallucination verifier** needs rules for rounding tolerance, derived figures ("~71%", "4.1% → 31.7%"), dates and version strings ("5.3.x").
12. **Baseline A executes LLM-written pandas code.** Sandbox it (subprocess, timeout, no network).
13. Model choice and the **≤ $0.50/run** target should be checked against current pricing. Eval volume is 25+ scenarios × 3 seeds × 3 LLM systems ≈ 225+ runs, so budget the cost of eval separately.

### Eval
14. **"Root-cause accuracy: match on filter set"** needs a defined partial-match rule (superset or subset of filters, extra dimensions such as app_version).
15. The two-cause scenarios need a scoring rule for @1 when both causes are correct.

### Scope and process
16. Twenty-five days is aggressive. FastAPI, Postgres and APScheduler aren't needed for the definition of done. The CLI plus Streamlit plus cron cover it, so they can be cut from v1.
17. `Report.rejected: list[tuple[Hypothesis, list[str]]]` serializes awkwardly. Use a small `RejectedHypothesis` model.
18. `git init` and a `.gitignore` (for `runs/`, `.env` and data files) are needed before M0 work starts.

## 8. Suggested next step

First a live smoke test of M4 with an API key (S11 on 2026-04-01 is the easy case, S05 or S23 the hard ones). Then M5: Critic checks (§8 of the PRD; decide which are pure code, §7 item 10), confidence in code, report writer with the numeric check, MD + JSON output. Make the first git commit first (nothing is committed yet).
