"""Daily session-level cube: every dimension x funnel counts. All analysis reads from this."""
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import duckdb
import pandas as pd

from sentinel.ingestion import day_file

DIMS = ["platform", "os", "app_version", "country", "traffic_source", "device_type",
        "new_vs_returning", "category", "payment_method"]
# payment_method is the session's payment_attempt method ('none' if it never attempted),
# so it is only meaningful for metrics whose numerator and denominator both need an attempt.
COUNTS = ["S", "A", "C", "P", "O", "R"]  # sessions, atc, checkout, attempt sessions, orders, revenue

_SQL = """
with ev as (
  select session_id, max((event_type='add_to_cart')::int) a, max((event_type='checkout_start')::int) c,
         max((event_type='payment_attempt')::int) p,
         any_value(payment_method) filter (where event_type='payment_attempt') pm
  from read_parquet('{ev}') group by 1),
o as (select session_id, count(*) n, sum(amount) r from read_parquet('{od}') group by 1)
select s.platform, s.os, s.app_version, s.country, s.traffic_source, s.device_type, s.new_vs_returning,
       s.category, coalesce(ev.pm, 'none') payment_method, count(*) S, coalesce(sum(ev.a),0) A,
       coalesce(sum(ev.c),0) C, coalesce(sum(ev.p),0) P, coalesce(sum(o.n),0) O, coalesce(sum(o.r),0.0) R
from read_parquet('{se}') s left join ev using(session_id) left join o using(session_id)
group by all
"""


@lru_cache(maxsize=512)
def _cube(data_dir: Path, day: date) -> pd.DataFrame:
    f = {k: day_file(data_dir, t, day).as_posix() for k, t in
         (("se", "sessions"), ("ev", "events"), ("od", "orders"))}
    return duckdb.connect().execute(_SQL.format(**f)).df()


def day_cube(data_dir: Path, day: date) -> pd.DataFrame:
    return _cube(Path(data_dir), day)


def has_day(data_dir: Path, day: date) -> bool:
    return day_file(data_dir, "orders", day).exists()


def window_days(day: date, kind: str, weeks: int = 4) -> list[date]:
    if kind == "weekday":
        return [day - timedelta(days=7 * k) for k in range(1, weeks + 1)]
    return [day - timedelta(days=k) for k in range(1, 8)]  # trailing7


def _totals(cube: pd.DataFrame) -> pd.Series:
    return cube[COUNTS].sum()


def clean_baseline_days(data_dir: Path, days: list[date]) -> list[date]:
    """Existing days minus outliers in total revenue/sessions (earlier incidents, late data).

    Baselines are means of daily cubes (so segments stay additive); without this filter a
    contaminated day, e.g. a prior scenario, would bias them. Keeps all days if < 2 survive.
    """
    days = [d for d in days if has_day(data_dir, d)]
    if len(days) < 3:
        return days
    tot = pd.DataFrame([_totals(day_cube(data_dir, d)) for d in days], index=days)
    keep = pd.Series(True, index=days)
    for col in ("R", "S"):
        med = tot[col].median()
        sigma = max(1.4826 * (tot[col] - med).abs().median(), 0.03 * med)
        keep &= (tot[col] - med).abs() <= 3 * sigma
    return [d for d in days if keep[d]] if keep.sum() >= 2 else days


def baseline_cube(data_dir: Path, days: list[date]) -> pd.DataFrame:
    """Mean daily cube over `days` (per-segment mean daily counts)."""
    cat = pd.concat([day_cube(data_dir, d) for d in days])
    return cat.groupby(DIMS, as_index=False)[COUNTS].sum().assign(
        **{c: lambda x, c=c: x[c] / len(days) for c in COUNTS})


def filt(cube: pd.DataFrame, filters: dict | None) -> pd.DataFrame:
    for k, v in (filters or {}).items():
        cube = cube[cube[k] == v]
    return cube
