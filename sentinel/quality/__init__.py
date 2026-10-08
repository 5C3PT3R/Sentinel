"""Deterministic data-quality checks. Status: any block issue -> BLOCK, else any warn -> WARN."""
from datetime import date
from functools import lru_cache
from pathlib import Path
from statistics import median

import duckdb
import yaml

from sentinel.config import ROOT
from sentinel.ingestion import TABLES, history_days, load_day
from sentinel.models import DQIssue, DQReport

SCHEMA = yaml.safe_load((ROOT / "config" / "schema.yaml").read_text())
CATEGORICAL = {
    "sessions": ["platform", "os", "app_version", "country", "traffic_source", "device_type",
                 "new_vs_returning", "category"],
    "events": ["event_type", "payment_method", "category"],  # error_code excluded: new codes are signal
    "orders": ["payment_method"],
}
ROW_BLOCK_RATIO = 0.7  # today's volume / trailing median below this -> BLOCK
NULL_WARN_DELTA = 0.10  # absolute rise in null rate vs trailing median
DUP_WARN_RATE = 0.01


def _stats(con, prefix: str, cols: dict[str, list[str]]) -> dict:
    """Per-table row count, null rate and distinct values for one loaded day."""
    out = {}
    for t, cs in cols.items():
        row = con.execute(f"select count(*) from {prefix}{t}").fetchone()[0]
        nulls = {c: (con.execute(f"select avg(({c} is null)::int) from {prefix}{t}").fetchone()[0]
                     or 0.0) for c in cs}
        vals = {c: {r[0] for r in con.execute(f"select distinct {c} from {prefix}{t}").fetchall()}
                for c in CATEGORICAL[t] if c in cs}
        out[t] = {"rows": row, "nulls": nulls, "vals": vals}
    return out


@lru_cache(maxsize=256)
def _hist_stats(data_dir: Path, day: date, cols: tuple) -> dict:
    con = duckdb.connect()
    load_day(con, data_dir, day)
    return _stats(con, "", {t: list(c) for t, c in cols})


def check_day(data_dir: Path, day: date) -> DQReport:
    issues: list[DQIssue] = []
    add = lambda check, sev, detail: issues.append(DQIssue(check=check, severity=sev, detail=detail))
    con = duckdb.connect()
    try:
        load_day(con, data_dir, day)
    except duckdb.IOException as e:
        return DQReport(status="BLOCK", issues=[DQIssue(check="freshness", severity="block",
                                                        detail=f"missing file(s): {e}")])

    # schema diff (extra/missing columns); tables with missing columns skip column-level checks
    actual = {t: [r[0] for r in con.execute(f"describe {t}").fetchall()] for t in TABLES}
    ok_cols = {}
    for t in TABLES:
        missing, extra = set(SCHEMA[t]) - set(actual[t]), set(actual[t]) - set(SCHEMA[t])
        if missing or extra:
            add("schema_diff", "block", f"{t}: missing {sorted(missing)}, unexpected {sorted(extra)}")
        ok_cols[t] = [c for c in actual[t] if c in SCHEMA[t]]
    cols = {t: ok_cols[t] for t in TABLES if not set(SCHEMA[t]) - set(actual[t])}

    today = _stats(con, "", cols)
    hist = []
    for h in history_days(data_dir, day):
        try:
            hist.append(_hist_stats(Path(data_dir), h, tuple((t, tuple(c)) for t, c in cols.items())))
        except duckdb.Error:
            pass  # historical day with its own schema break; ignore as a baseline

    for t in cols:
        hs = [h[t] for h in hist if t in h]
        if hs:
            # sessions drop legitimately in business scenarios (traffic), orders too: only block on
            # gross loss of sessions, and on events-per-session (a funnel change can't move it much)
            if t == "sessions":
                ratio = today[t]["rows"] / median(x["rows"] for x in hs)
            elif t == "events" and "sessions" in today:
                ratio = (today[t]["rows"] / today["sessions"]["rows"]) / median(
                    x[t]["rows"] / x["sessions"]["rows"] for x in hist if t in x and "sessions" in x)
            else:
                ratio = 1.0
            if ratio < ROW_BLOCK_RATIO:
                add("row_count", "block", f"{t}: {ratio:.0%} of trailing median volume")
            for c, r in today[t]["nulls"].items():
                base = median(x["nulls"].get(c, 0.0) for x in hs)
                if r - base > NULL_WARN_DELTA:
                    add("null_rate", "warn", f"{t}.{c}: null rate {r:.0%} vs {base:.0%} baseline")
            for c, v in today[t]["vals"].items():
                seen = set().union(*(x["vals"].get(c, set()) for x in hs))
                if new := v - seen - {None}:
                    add("category_drift", "warn", f"{t}.{c}: new values {sorted(map(str, new))}")

    if "events" in cols:
        d = con.execute("select 1-count(distinct event_id)/count(*) from events").fetchone()[0] or 0
        if d > DUP_WARN_RATE:
            add("duplicates", "warn", f"events: {d:.0%} duplicate event_id")
    if "orders" in cols:
        d = con.execute("select 1-count(distinct order_id)/count(*) from orders").fetchone()[0] or 0
        if d > DUP_WARN_RATE:
            add("duplicates", "warn", f"orders: {d:.0%} duplicate order_id")
        bad = con.execute("select count(*) from orders where amount <= 0 or items < 1").fetchone()[0]
        if bad:
            add("range", "warn", f"orders: {bad} rows with amount<=0 or items<1")
    for t in cols:
        off = con.execute(f"select avg((ts::date != date '{day}')::int) from {t}").fetchone()[0] or 0
        if off > 0.01:  # sessions spilling past midnight are normal
            add("freshness", "warn", f"{t}: {off:.0%} of rows have ts outside {day}")

    # tracking invariants (history-free): funnel order and orders == payment_success
    if {"events", "orders"} <= cols.keys():
        ps, n_ord = con.execute(
            "select (select count(*) from events where event_type='payment_success'),"
            " (select count(*) from orders)").fetchone()
        if n_ord and abs(n_ord - ps) / n_ord > 0.02:
            add("event_order_mismatch", "warn", f"orders={n_ord} vs payment_success events={ps}")
        orphan, cs = con.execute(
            "select count(distinct session_id), (select count(distinct session_id) from events"
            " where event_type='checkout_start') from events where event_type='checkout_start' and"
            " session_id not in (select session_id from events where event_type='add_to_cart')"
        ).fetchone()
        if cs and orphan / cs > 0.05:
            add("funnel_order", "warn", f"{orphan}/{cs} checkout sessions have no add_to_cart")

    status = "BLOCK" if any(i.severity == "block" for i in issues) else "WARN" if issues else "PASS"
    return DQReport(status=status, issues=issues)

