# Sentinel

Autonomous root-cause analysis for e-commerce business metrics.

Dashboards tell you *that* revenue dropped. Sentinel works out *why*. For each day of event data it
checks data quality, detects which KPIs moved significantly, decomposes the metric tree, drills
through dimensions (platform, country, payment method, app version, ...) and produces
evidence-backed hypotheses about the cause.

> "Something changed. Figure out what happened, why it happened, and tell me whether I need to act."

**Status:** work in progress. Milestones M0–M3 are done, M4 (LLM investigation agent) is built and
tested against a scripted fake LLM but has not been run against the live API yet. `sentinel run` and
`sentinel eval` are still stubs. See [Team](#team) for who owns what, [HANDOFF.md](HANDOFF.md) for detailed status and
[PRD.md](PRD.md) for the full spec.

## How it works

```
Ingestion → Data Quality (PASS/WARN/BLOCK) → Detection → Investigation (LLM + tools)
   → Critic (≤2 revise loops) → Reporting
```

The design rule is that the deterministic core computes the numbers and the LLM only reasons and
writes:

1. The LLM never computes numbers. Python/DuckDB does.
2. Every claim cites an `evidence_id`. A claim with no citation, or with a number that can't be
   traced to its cited evidence, is dropped ([sentinel/evidence/verifier.py](sentinel/evidence/verifier.py)).
3. If data quality is `BLOCK`, there is no analysis, only a data-quality alert.
4. The Critic can veto a hypothesis (planned, M5).
5. Runs are reproducible: seeded data and cached LLM responses (temperature 0).
6. Contributions add up to at most 100%, and the unexplained remainder is shown.

Sentinel ships with its own benchmark: a simulator that generates synthetic sessions, events and
orders with **28 injected scenarios** (payment failures, traffic drops, app-version bugs, stock-outs,
Simpson's-paradox mix shifts, two-cause days, tracking bugs, schema changes, late data, and
no-anomaly controls) plus a `ground_truth.json` to score against.

## Quick start

Requires Python 3.11+.

```bash
pip install -e .[dev]

# Generate 180 days of synthetic data (~15 s, ~90 MB) into data/sim/
sentinel simulate --out data/sim

# Lint and test (the data-dependent tests skip if data/sim is missing)
ruff check .
pytest
```

`simulate` options: `--days`, `--sessions` (per day, default 10k), `--seed`, `--no-plot`. It
writes one parquet file per table per day, `ground_truth.json` and a `revenue.png` with the
scenario days marked.

### Using the pieces from Python

```python
from datetime import date
from pathlib import Path

from sentinel.quality import check_day
from sentinel.detection import detect
from sentinel.analysis.tools import Tools

data, day = Path("data/sim"), date(2026, 4, 1)   # S11: electronics stock-out
print(check_day(data, day).status)                # PASS / WARN / BLOCK

tools = Tools(data)
anomalies = detect(data, day, tools.store)        # KPIs that moved, with evidence ids
print(tools.find_causes(str(day)))                # deterministic tree walk + drill-down (Baseline C)
```

To try the LLM investigation agent, set `ANTHROPIC_API_KEY` and call
`sentinel.agents.investigation.investigate(tools, LLMClient(), day, anomalies)`. The model is set in
[config/settings.yaml](config/settings.yaml).

## Project layout

| Path | What it is |
|---|---|
| `sentinel/simulator/` | Synthetic data generator and scenario injector |
| `sentinel/ingestion/` | Per-day parquet files → DuckDB views |
| `sentinel/quality/` | Data-quality checks: schema diff, volume, nulls, duplicates, category drift, tracking invariants |
| `sentinel/detection/` | Robust-z change detection against weekday and trailing-7-day baselines |
| `sentinel/analysis/` | Metric cube, metric-tree decomposition (LMDI), Simpson-safe drill-down, LLM-facing tools |
| `sentinel/evidence/` | Evidence store and numeric-hallucination verifier |
| `sentinel/llm/` | Anthropic client with a deterministic response cache and token tally |
| `sentinel/agents/` | Investigation agent (tool-use loop) and its prompt |
| `config/` | Settings, registered schema, metric tree |
| `scenarios/` | Scenario definitions (YAML) |
| `tests/` | Pytest suite |

`sentinel/api`, `baselines`, `eval` and `ui` are placeholders for later milestones.

## Team

### Done so far: Eashan Singh

- **M0** Skeleton: repo layout, Pydantic models, CLI, config, CI
- **M1** Simulator: data generator, scenario injector, 28 scenarios, ground truth
- **M2** Ingestion and data-quality checks
- **M3** Detection, metric-tree decomposition, drill-down, analysis tools, evidence store
- **M4** LLM client and investigation agent with the numeric verifier (tested against a scripted fake LLM)

### Remaining work

| Owner | Milestones | Scope |
|---|---|---|
| **Eashan Singh** | M4 live test, M5 | Smoke-test the investigation agent against the real API. Build the Critic agent (the six checks, confidence computed in code), the report writer (Markdown + JSON, numeric check on the full text) and the orchestrator behind `sentinel run` (DQ → detect → investigate → critic → report, saved under `runs/<run_id>/`). |
| **Arpit** | M6 | Baselines: A (single agent + pandas, sandboxed code execution), No-Critic and stats-only (`Tools.find_causes`). Build `sentinel eval` → `results.csv` + `report.md` across all scenarios × 3 seeds, and settle the scoring rules for partial filter matches and two-cause scenarios (HANDOFF §7 items 12–15). |
| **Krish** | M7, M8 | Streamlit UI (run list, report view, click a claim to see its query and result, eval dashboard), a daily scheduler (cron) and an optional Slack webhook. Build a public-dataset adapter (Olist or GA4 sample) that writes the same per-day parquet layout as the simulator. |
| **Everyone** | M8 | Final write-up: results table, limitations. |

**Hand-offs:** Arpit's eval and Krish's UI both consume the `Report` model in
[sentinel/models/__init__.py](sentinel/models/__init__.py) and the `runs/<run_id>/` output from
`sentinel run`. Agree on that output format with Eashan early, and use stub reports until M5 lands.
Log design decisions in [DECISIONS.md](DECISIONS.md).

Design decisions and their reasoning are logged in [DECISIONS.md](DECISIONS.md).
