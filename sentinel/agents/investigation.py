"""Investigation Agent: an LLM tool-use loop over the deterministic tools (PRD §6.1, M4).

The LLM chooses what to explore and writes the statements. Code owns everything else: tool results,
evidence ids, contribution_pct, budgets, and rejection of anything uncited or with untraceable numbers.
"""
import json
from datetime import date
from pathlib import Path

from pydantic import BaseModel

from sentinel.analysis.cube import DIMS
from sentinel.analysis.metric_tree import METRICS
from sentinel.analysis.tools import Tools
from sentinel.config import load_settings
from sentinel.evidence.verifier import unsupported_numbers
from sentinel.models import Anomaly, Hypothesis

PROMPT = (Path(__file__).parent / "prompts" / "investigation.md").read_text(encoding="utf-8")
MAX_SUBMIT_RETRIES = 2

_S = {"type": "string"}
_FILTERS = {"type": "object", "additionalProperties": _S,
            "description": f"dimension -> value; dimensions: {', '.join(DIMS)}"}
_M = {"type": "string", "enum": list(METRICS)}
_BASE = {"type": "string", "enum": ["weekday", "trailing7"], "default": "weekday"}


def _spec(name, desc, props, required):
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": props, "required": required}}


TOOL_SPECS = [
    _spec("get_metric", "Daily values of a metric over a date range, optionally filtered to a segment.",
          {"metric": _M, "start": _S, "end": _S, "filters": _FILTERS}, ["metric", "start", "end"]),
    _spec("decompose_metric", "Split a metric's change vs baseline into its children in the metric tree "
          "(shares sum to 1).", {"metric": _M, "date": _S, "baseline": _BASE}, ["metric", "date"]),
    _spec("drill_down", "Per-segment values, deltas and share of the metric's change for one dimension, "
          "ranked. Optionally restrict to a segment with filters.",
          {"metric": _M, "dimension": {"type": "string", "enum": DIMS}, "date": _S, "baseline": _BASE,
           "filters": _FILTERS}, ["metric", "dimension", "date"]),
    _spec("compare_distributions", "Chi-square test of whether a session column's mix shifted today vs "
          "baseline, optionally within a segment.",
          {"column": {"type": "string", "enum": DIMS[:-1]}, "date": _S, "baseline": _BASE,
           "segment": _FILTERS}, ["column", "date"]),
    _spec("check_traffic", "Sessions vs baseline (is traffic within its normal range?).",
          {"date": _S, "baseline": _BASE, "filters": _FILTERS}, ["date"]),
    _spec("get_events", "Count events of a type, grouped by error_code, payment_method or category.",
          {"event_type": {"type": "string", "enum": ["view", "add_to_cart", "checkout_start",
                                                      "payment_attempt", "payment_success",
                                                      "payment_failure"]},
           "date": _S, "filters": _FILTERS,
           "group_by": {"type": "string", "enum": ["error_code", "payment_method", "category"]}},
          ["event_type", "date"]),
    _spec("correlate", "Pearson correlation of two metrics' daily values over a trailing window.",
          {"metric_a": _M, "metric_b": _M, "end": _S, "window": {"type": "integer"}},
          ["metric_a", "metric_b", "end"]),
    _spec("segment_contribution", "Share of the total revenue change that happened inside a segment. "
          "Call for each hypothesis.", {"filters": _FILTERS, "date": _S, "baseline": _BASE},
          ["filters", "date"]),
]
_EV = {"type": "array", "items": _S}
SUBMIT = _spec("submit_hypotheses", "Final answer. Call once, when done.", {
    "hypotheses": {"type": "array", "items": {"type": "object", "properties": {
        "statement": _S, "segment_filters": _FILTERS, "chain": _EV, "evidence_ids": _EV},
        "required": ["statement", "segment_filters", "chain", "evidence_ids"]}},
    "rejected_alternatives": {"type": "array", "items": {"type": "object", "properties": {
        "statement": _S, "evidence_ids": _EV}, "required": ["statement", "evidence_ids"]}}},
    ["hypotheses"])


TOOL_NAMES = {t["name"] for t in TOOL_SPECS}


class InvestigationResult(BaseModel):
    hypotheses: list[Hypothesis] = []
    rejected_alternatives: list[dict] = []
    unexplained_pct: float = 100.0
    tool_calls: int = 0
    budget_exhausted: bool = False
    dropped: list[str] = []  # reasons, for the audit trail


def _view(out: dict) -> str:
    """What the LLM sees: the evidence record, with long segment lists trimmed."""
    r = out["result"]
    if isinstance(r.get("segments"), list) and len(r["segments"]) > 10:
        r = {**r, "segments": r["segments"][:8] + r["segments"][-2:]}
    text = json.dumps({"evidence_id": out["evidence_id"], "result": r}, default=str)
    return text if len(text) <= 9000 else text[:9000] + "...[truncated]"


def _context(day: date, anomalies: list[Anomaly]) -> str:
    rows = [a.model_dump(mode="json") for a in anomalies]
    return (f"Date under analysis: {day}. Data quality: PASS. Detected moves vs baseline "
            f"(cite their evidence_id as the start of your chain):\n{json.dumps(rows, indent=1)}")


def _check(h: dict, tools: Tools) -> list[str]:
    """Problems with one submitted statement (unknown evidence, untraceable numbers)."""
    ids = [i for i in h.get("evidence_ids", []) if i in tools.store.items]
    errs = []
    if not ids:
        errs.append("cites no valid evidence_id")
    for bad in unsupported_numbers(h["statement"], tools.store, ids):
        errs.append(f"number {bad!r} does not appear in the cited evidence")
    return errs


def _finalize(sub: dict, tools: Tools, day: date, baseline: str) -> InvestigationResult:
    res, hyps = InvestigationResult(), []
    for i, h in enumerate(sub.get("hypotheses", []), 1):
        if errs := _check(h, tools):
            res.dropped.append(f"{h['statement'][:60]!r}: {'; '.join(errs)}")
            continue
        ids = [e for e in h["evidence_ids"] if e in tools.store.items]
        # contribution is always recomputed in code; the LLM's own figure is never used
        try:
            c = tools.segment_contribution(h["segment_filters"], str(day), baseline)
        except Exception as e:  # noqa: BLE001 - a bad filter drops one hypothesis, not the run
            res.dropped.append(f"{h['statement'][:60]!r}: bad segment_filters ({e})")
            continue
        hyps.append(Hypothesis(
            id=f"H{i}", statement=h["statement"], segment_filters=h["segment_filters"],
            chain=[e for e in h.get("chain", []) if e in tools.store.items],
            contribution_pct=100 * min(max(c["result"]["share"], 0.0), 1.0),
            evidence_ids=ids + [c["evidence_id"]]))
    if (total := sum(h.contribution_pct for h in hyps)) > 100:  # overlapping segments: scale to 100%
        for h in hyps:
            h.contribution_pct *= 100 / total
    res.hypotheses, res.unexplained_pct = hyps, max(0.0, 100 - sum(h.contribution_pct for h in hyps))
    for r in sub.get("rejected_alternatives", []):
        if _check(r, tools):
            res.dropped.append(f"rejected alternative {r['statement'][:50]!r}: untraceable")
        else:
            res.rejected_alternatives.append(r)
    return res


def investigate(tools: Tools, client, day: date, anomalies: list[Anomaly], baseline: str = "weekday",
                budget: int | None = None) -> InvestigationResult:
    budget = budget or load_settings()["budgets"]["investigation_tool_calls"]
    messages = [{"role": "user", "content": _context(day, anomalies)}]
    calls, retries, nudges, sub = 0, 0, 0, None
    for _ in range(budget + 2 * MAX_SUBMIT_RETRIES + 6):
        spent = calls >= budget
        resp = client.create(PROMPT, messages, [SUBMIT] if spent else TOOL_SPECS + [SUBMIT],
                             {"type": "tool", "name": "submit_hypotheses"} if spent else None)
        messages.append({"role": "assistant", "content": resp["content"]})
        uses = [b for b in resp["content"] if b["type"] == "tool_use"]
        if not uses:
            if nudges >= 2:
                break
            nudges += 1
            messages.append({"role": "user", "content": "Call submit_hypotheses now."})
            continue
        results = []
        for u in uses:
            if u["name"] == "submit_hypotheses":
                sub = u["input"]
                problems = [f"{h['statement'][:50]!r}: {e}" for h in sub.get("hypotheses", [])
                            for e in _check(h, tools)]
                if problems and retries < MAX_SUBMIT_RETRIES:
                    retries += 1
                    results.append({"type": "tool_result", "tool_use_id": u["id"], "is_error": True,
                                    "content": "Fix and resubmit (only use numbers from cited evidence): "
                                               + " | ".join(problems)})
                    sub = None
                else:
                    break
                continue
            if calls >= budget:
                out = "Tool budget exhausted. Call submit_hypotheses."
                err = True
            else:
                calls += 1
                try:
                    if u["name"] not in TOOL_NAMES:
                        raise ValueError("unknown tool")
                    out, err = _view(getattr(tools, u["name"])(**u["input"])), False
                except Exception as e:  # noqa: BLE001 - bad model arguments are data, not crashes
                    out, err = f"{type(e).__name__}: {e}", True
            results.append({"type": "tool_result", "tool_use_id": u["id"], "content": out,
                            **({"is_error": True} if err else {})})
        if sub is not None:
            break
        messages.append({"role": "user", "content": results})
    res = _finalize(sub or {}, tools, day, baseline)
    res.tool_calls, res.budget_exhausted = calls, calls >= budget
    if sub is None:
        res.dropped.append("agent never submitted hypotheses")
    return res
