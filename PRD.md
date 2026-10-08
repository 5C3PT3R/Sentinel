# PRD — Sentinel: Autonomous Root-Cause Analysis Agent

> **For Claude Code:** Read this entire document before writing any code. Build in the milestone order in §12. Do not skip the synthetic data generator and ground-truth harness. They are what make this project measurable. When something here is ambiguous, pick the simplest option that keeps the invariants in §4 true, and note the decision in `DECISIONS.md`.

---

## 1. One-line summary

A multi-agent system that watches a business data source every day, detects when a key metric moves, works out *why* it moved by drilling through dimensions and a metric tree, challenges its own conclusion, and produces an evidence-backed report with a confidence score. It also includes a benchmark that proves it beats a plain "LLM + pandas" agent.

**Core promise:** *"Something changed. Figure out what happened, why it happened, and tell me whether I need to act."*

---

## 2. Problem

Dashboards show that revenue dropped. They don't say why. An analyst then spends hours slicing by platform, region and payment method to find the cause. Generic "chat with your CSV" LLM tools are unreliable here. They hallucinate numbers, stop at the first plausible story, and never check alternatives.

## 3. Goals and non-goals

### Goals
1. Ingest a daily batch of event-level e-commerce data (CSV/Parquet, later Postgres).
2. Validate data quality and refuse to analyze broken data.
3. Detect statistically meaningful changes in top-level KPIs.
4. Find the root cause by decomposing metrics and drilling into dimensions, and quantify how much of the change each cause explains.
5. Have a Critic agent challenge every conclusion before it is reported.
6. Produce a human-readable report with evidence, contribution %, confidence and a recommended action.
7. Benchmark against a baseline single-agent LLM on synthetic data with known, injected root causes.

### Non-goals (v1)
- No real-time streaming. Daily batch only.
- No auto-remediation. The system recommends; it never acts.
- No multi-tenant auth or billing.
- No general-purpose BI or arbitrary-chart builder.
- No forecasting beyond what anomaly detection needs.

---

## 4. Invariants (must never be violated)

1. **The LLM never computes numbers.** Every number in a report is computed by deterministic Python (DuckDB/pandas/scipy) and passed to the LLM as structured data. The LLM reasons, names things and writes text. It does not do arithmetic.
2. **Every claim cites evidence.** Each finding in the report links to an `evidence_id` that points to a stored query plus its result. A claim without evidence gets dropped before reporting.
3. **No analysis on bad data.** If the Data Quality agent returns `BLOCK`, the pipeline stops and emits a data-quality alert instead of a business insight.
4. **The Critic can veto.** A root cause the Critic rejects cannot be reported as the primary cause. It can only appear as "considered and rejected".
5. **Every run is reproducible.** Inputs, config, prompts, model name, all tool calls and all outputs are persisted per `run_id`. Re-running with the same seed and cached LLM responses gives identical output.
6. **Contribution numbers must reconcile.** Contributions of the reported causes must sum to ≤ 100% of the observed change, and the remainder is shown explicitly as "unexplained".

---

## 5. Users and use case

**Primary persona:** a growth or ops lead at a mid-size e-commerce company who reads a morning report and needs to know whether to escalate.

**Primary scenario:**
Every morning at 07:00 the system ingests yesterday's data and compares it to a baseline (same weekday over the last 4 weeks, plus trailing 7-day). If nothing material changed, it sends a 3-line "all normal" summary. If something changed, it sends a report like:

> **Revenue −14.2% vs baseline (high significance)**
> **Root cause (confidence 0.86):** Android checkout payment failures rose 52%, mostly on UPI. This explains ~71% of the revenue drop.
> **Evidence chain:** Revenue ↓14% → Orders ↓9% (AOV flat) → Mobile orders ↓23% → Android ↓41% → Sessions normal → Checkout conversion ↓38% → Payment failures ↑52% (UPI: 4.1% → 31.7%).
> **Alternatives rejected:** traffic drop (sessions within normal range); pricing change (AOV flat; no catalog change); seasonality (same-weekday baseline used).
> **Unexplained:** ~29%, spread across small segments.
> **Recommended action:** Check the UPI payment-gateway integration on Android (app version 5.3.x). Escalate: **Yes**.

---

## 6. System architecture

```
Source (CSV/Parquet/Postgres)
      ↓
[1] Ingestion  ── loads into DuckDB, snapshots by date
      ↓
[2] Data Quality Agent ── PASS / WARN / BLOCK
      ↓
[3] Detection Agent ── which KPIs moved, and how significantly
      ↓
[4] Investigation Agent ── metric-tree decomposition + dimension drill-down (tool-using loop)
      ↓
[5] Critic Agent ── challenges hypotheses, requests extra checks, assigns confidence
      ↓        ↑ (max 2 revision loops back to Investigation)
[6] Reporting Agent ── report (Markdown + JSON) + alert decision
      ↓
[7] Delivery ── dashboard, stored report, optional Slack/email webhook
```

An **Orchestrator** (plain Python state machine, not an LLM) drives the stages, passes typed state objects between them, enforces budgets (max tool calls, max tokens, max loops) and persists everything.

### 6.1 Agent responsibilities

| # | Component | Type | Responsibility | Output |
|---|-----------|------|----------------|--------|
| 1 | Ingestion | Deterministic | Load file/table, cast types, partition by date | `RawBatch` |
| 2 | Data Quality Agent | Deterministic checks + LLM summary | Schema diff vs registered schema; null-rate, row-count, freshness, duplicate, range and category-drift checks | `DQReport {status, issues[]}` |
| 3 | Detection Agent | Deterministic stats | Compare each KPI to baseline; flag significant moves | `Anomaly[]` |
| 4 | Investigation Agent | LLM with tools | Pick which branches to explore, call analysis tools, form hypotheses | `Hypothesis[]` with evidence ids |
| 5 | Critic Agent | LLM with tools | Attack each hypothesis, run falsification checks, score confidence | `CriticVerdict[]` |
| 6 | Reporting Agent | LLM (text only) | Write the explanation from verified findings only | `Report` (MD + JSON) |

### 6.2 Investigation tools (deterministic Python, exposed to the LLM as tool calls)

- `get_metric(metric, date_range, filters)` → value
- `decompose_metric(metric, date, baseline)` → contribution of each child in the metric tree (see §7.2)
- `drill_down(metric, dimension, date, baseline, filters)` → per-segment values, deltas and contribution to the parent's change, ranked
- `compare_distributions(column, segment, date, baseline)` → shift test result
- `check_traffic(date, baseline, filters)` → sessions vs baseline
- `get_events(event_type, filters, date)` → counts, e.g. payment failures by error code
- `correlate(metric_a, metric_b, window)` → correlation over recent days

Every tool call returns `{evidence_id, query, result, computed_at}` and is stored. The LLM refers to results only by `evidence_id`.

---

## 7. Analytical method (deterministic core)

### 7.1 Change detection
- **Baselines:** (a) same weekday over the last 4 weeks; (b) trailing 7-day mean. Use the more conservative one.
- **Significance:** robust z-score (median/MAD) on the baseline window, plus a minimum relative change threshold (configurable, default 5%) so that tiny segments don't trigger alerts.
- **Severity:** `low / medium / high` from z-score and absolute business impact (₹ or $).
- Optional v2: seasonal decomposition (STL) for trend-aware baselines.

### 7.2 Metric tree
Encode KPIs as a tree with explicit relationships:

```
revenue = orders × AOV
orders  = sessions × conversion_rate
conversion_rate = add_to_cart_rate × checkout_start_rate × payment_success_rate
```

For multiplicative nodes, attribute the change with **log-difference decomposition** (contribution of child *i* = Δln(child_i) / Δln(parent)), so the parts sum exactly to the whole. For additive splits (e.g. revenue = Σ revenue by platform), use simple Δ contribution.

### 7.3 Dimension drill-down
- Dimensions: `platform`, `os`, `app_version`, `country/region`, `payment_method`, `traffic_source`, `device_type`, `category`, `new_vs_returning`.
- At each level, score segments by **explanatory power** (share of the parent change explained) and **surprise** (how differently the segment behaved vs its own baseline), in the style of Adtributor.
- Recurse into the top segment while it explains > 20% of the parent change and depth < 4.
- Prune segments under a minimum volume (default 1% of baseline).

### 7.4 Contribution reconciliation
Final report contribution = product of the shares along the chain, computed in code. The unexplained remainder is reported explicitly.

---

## 8. Critic Agent spec

For each hypothesis the Critic must run, at minimum:

1. **Traffic check:** did sessions fall in this segment? If yes, a conversion story may be wrong.
2. **Mix-shift check:** is the change caused by segment *mix* changing, not by in-segment behaviour (Simpson's paradox)?
3. **Data-artifact check:** do DQ warnings, a schema change or late-arriving data overlap this segment?
4. **Seasonality/calendar check:** holiday, sale or weekday effect?
5. **Magnitude check:** does the hypothesis explain enough of the change (> 30%) to be called *primary*?
6. **Alternative search:** ask Investigation for the top 2 competing explanations and compare their explanatory power.

Output per hypothesis: `{verdict: accept | revise | reject, reasons[], extra_checks_run[], confidence: 0–1}`.

`revise` sends a targeted request back to Investigation. Max 2 loops. Confidence combines (a) share of change explained, (b) number of checks passed, (c) statistical strength of the key evidence. Compute it in code from the Critic's structured output, not as a number the LLM writes.

---

## 9. Data

### 9.1 Synthetic data generator (required, build first)
`sentinel/simulator/` generates a realistic e-commerce event dataset:

- ~180 days, ~50k–200k sessions/day (configurable).
- Tables: `sessions`, `events` (view, add_to_cart, checkout_start, payment_attempt, payment_success, payment_failure with error_code), `orders` (order_id, amount, items, payment_method, …).
- Weekly seasonality, trend, noise and a couple of sale days.
- **Anomaly injector:** a YAML scenario file that injects known root causes on specific days and records ground truth:

```yaml
- id: S01
  date: 2026-05-14
  type: payment_failure_spike
  filters: {os: android, payment_method: upi}
  params: {failure_rate_from: 0.04, failure_rate_to: 0.32}
  expected_root_cause: "android + upi payment failures"
```

Ship **at least 25 scenarios** covering: payment failure spike, traffic drop from one source, app-version bug, pricing/AOV change, regional outage, inventory stock-out in one category, tracking/logging bug (should be caught as data issue, not business issue), schema change, late data, Simpson's-paradox mix shift, two simultaneous causes, and **no-anomaly days** (to measure false positives).

### 9.2 Real data (v2)
Adapters for one public dataset (e.g. Olist Brazilian e-commerce or the Google Merchandise Store GA4 sample) and a Postgres connector. Mapping is declared in a `schema.yaml` that the DQ agent uses as the registered schema.

---

## 10. Benchmark and evaluation

### 10.1 Systems compared
1. **Sentinel** (this system).
2. **Baseline A:** single LLM agent with a Python/pandas execution tool, given the same data and the prompt "Analyze yesterday vs baseline and explain any significant change."
3. **Baseline B (ablation):** Sentinel without the Critic.
4. Optional: **Baseline C:** detection + drill-down only, no LLM (pure statistics).

Use the same model for all LLM-based systems.

### 10.2 Metrics (per scenario, aggregated across all scenarios)

| Metric | Definition |
|--------|-----------|
| Detection precision / recall | Flagged anomalies vs injected anomalies (incl. no-anomaly days) |
| False-positive rate | Alerts raised on no-anomaly days |
| Root-cause accuracy@1 / @3 | Correct segment/cause ranked 1st / in top 3 (match on filter set) |
| Contribution error | Abs. error between reported and true contribution % |
| Evidence support rate | % of report claims with a valid evidence_id whose stored result actually supports the claim (checked by an automated verifier + manual sample) |
| Numeric hallucination rate | Numbers in the report text that don't match any evidence result |
| Data-issue classification | Did it correctly label tracking bugs / schema changes as data issues? |
| Latency | Wall-clock per run |
| Cost | Tokens and $ per run |

### 10.3 Harness
`sentinel eval --scenarios scenarios/ --systems sentinel,baseline_a,no_critic --runs 3` writes `eval/results/<timestamp>/results.csv` and a summary `report.md` with tables and charts. Each scenario is run 3 times with different seeds for variance.

---

## 11. Tech stack

- **Language:** Python 3.11+
- **Storage/compute:** DuckDB (analytics), Parquet files; SQLite or Postgres for run metadata
- **Stats:** pandas, numpy, scipy, statsmodels
- **LLM:** Claude via the Anthropic Python SDK, with native tool use. Wrap it in a thin `llm/` client that supports response caching (for reproducibility) and token/cost logging. Model name lives in config.
- **Schemas:** Pydantic v2 for every inter-agent object
- **Orchestration:** plain Python state machine (no LangChain/LangGraph required in v1)
- **API:** FastAPI
- **UI:** Streamlit for v1 (run history, report view, evidence drill-through, eval results)
- **Scheduling:** APScheduler or cron for the daily run
- **CLI:** Typer
- **Testing:** pytest, with fixtures built from the simulator
- **Config:** YAML + `.env` for secrets

---

## 12. Milestones (build in this order)

**M0 — Skeleton (day 1)**
Repo layout, config, Pydantic models, CLI stub, `DECISIONS.md`, CI running pytest + ruff.

**M1 — Simulator + ground truth (days 2–4)**
Data generator, anomaly injector, 25+ scenarios, ground-truth file.
*Acceptance:* `sentinel simulate --scenarios scenarios/` produces data plus `ground_truth.json`; injected anomalies are visible in a quick plot.

**M2 — Ingestion + Data Quality (days 5–6)**
*Acceptance:* the schema-change and tracking-bug scenarios return `BLOCK`/`WARN` with the right issue; clean days return `PASS`.

**M3 — Detection + metric tree + drill-down tools (days 7–10)**
All deterministic. Unit-tested.
*Acceptance:* on single-cause scenarios, pure-stats drill-down (Baseline C) ranks the true cause in the top 3 for ≥ 70% of cases; contributions reconcile to the parent change within 1%.

**M4 — Investigation Agent (days 11–13)**
LLM tool-use loop over the M3 tools, with evidence store and budgets.
*Acceptance:* produces hypotheses with evidence ids; no numbers outside tool outputs.

**M5 — Critic + Reporting (days 14–16)**
*Acceptance:* Simpson's-paradox and traffic-drop scenarios get corrected by the Critic in at least half the cases where Investigation got them wrong; reports pass the numeric-hallucination checker.

**M6 — Baseline A + eval harness (days 17–19)**
*Acceptance:* `sentinel eval` produces the full metrics table for all systems.

**M7 — UI + scheduler + alerts (days 20–22)**
Streamlit app: run list, report view, click any claim to see its query and result, eval dashboard. Daily scheduled run. Optional Slack webhook.

**M8 — Real dataset + write-up (days 23–25)**
One public dataset adapter. README with architecture diagram, benchmark results and limitations.

---

## 13. Repo structure

```
sentinel/
  config/            settings.yaml, schema.yaml, metric_tree.yaml
  sentinel/
    models/          pydantic schemas (RawBatch, DQReport, Anomaly, Hypothesis, CriticVerdict, Report)
    simulator/       generator.py, injector.py
    ingestion/
    quality/         checks.py, agent.py
    detection/       baselines.py, detector.py
    analysis/        metric_tree.py, drilldown.py, tools.py  (deterministic tool layer)
    agents/          investigation.py, critic.py, reporting.py, prompts/
    llm/             client.py (caching, cost logging)
    evidence/        store.py, verifier.py
    orchestrator.py
    baselines/       single_agent.py, no_critic.py, stats_only.py
    eval/            harness.py, metrics.py
    api/             FastAPI app
    ui/              streamlit_app.py
    cli.py
  scenarios/         *.yaml
  tests/
  runs/              per-run artifacts (gitignored)
  DECISIONS.md
  README.md
```

---

## 14. Key data contracts (Pydantic, abbreviated)

```python
class Evidence(BaseModel):
    evidence_id: str
    tool: str
    params: dict
    query: str | None
    result: dict
    computed_at: datetime

class Anomaly(BaseModel):
    metric: str
    date: date
    value: float
    baseline: float
    pct_change: float
    z_score: float
    severity: Literal["low", "medium", "high"]
    evidence_id: str

class Hypothesis(BaseModel):
    id: str
    statement: str
    segment_filters: dict[str, str]
    chain: list[str]             # ordered evidence_ids from KPI down to cause
    contribution_pct: float      # computed in code
    evidence_ids: list[str]

class CriticVerdict(BaseModel):
    hypothesis_id: str
    verdict: Literal["accept", "revise", "reject"]
    reasons: list[str]
    checks_run: list[str]
    confidence: float            # computed in code

class Report(BaseModel):
    run_id: str
    date: date
    status: Literal["normal", "anomaly", "data_issue"]
    headline: str
    primary_cause: Hypothesis | None
    secondary_causes: list[Hypothesis]
    rejected: list[tuple[Hypothesis, list[str]]]
    unexplained_pct: float
    confidence: float
    recommended_action: str
    escalate: bool
    markdown: str
```

---

## 15. Non-functional requirements

- **Cost:** ≤ $0.50 per daily run on the default scenario size (log actual).
- **Latency:** ≤ 3 min per run end to end on a laptop.
- **Budgets:** max 25 tool calls for Investigation, 10 for Critic, 2 revise loops. On budget exhaustion, report with lowered confidence; never fail silently.
- **Observability:** structured JSON logs per stage; every run writes `runs/<run_id>/` with inputs hash, config, prompts, tool calls, LLM I/O, report.
- **Testing:** ≥ 80% coverage on `analysis/`, `detection/`, `quality/`, `evidence/`.
- **Security:** API keys only from env; no data leaves the machine except LLM calls, which receive aggregated tool results, never raw row-level PII.

---

## 16. Risks and mitigations

| Risk | Mitigation |
|------|-----------|
| LLM invents numbers | Invariant 1 + numeric verifier that fails the report |
| Agent stops at first plausible story | Critic must request ≥ 2 alternatives |
| Synthetic data too easy, results don't generalize | Include noisy, multi-cause, mix-shift and no-anomaly scenarios; add one real dataset in M8 |
| Combinatorial explosion in drill-down | Volume pruning, depth cap, explanatory-power threshold |
| Costs creep up | Response caching, token budgets, cost logged per run |
| Tracking bugs reported as business problems | DQ agent runs first; Critic data-artifact check |

---

## 17. Definition of done (v1)

- `sentinel run --date YYYY-MM-DD` produces a report for any simulated day.
- `sentinel eval` runs all scenarios against Sentinel, Baseline A and No-Critic, and writes the metrics table.
- Sentinel beats Baseline A on root-cause accuracy@1 and numeric hallucination rate, and the No-Critic ablation shows a measurable drop on mix-shift/traffic scenarios. If it doesn't, the README reports that honestly.
- Streamlit UI lets a user open any report claim and see the exact query and result behind it.
- README explains architecture, method, benchmark results and limitations.
