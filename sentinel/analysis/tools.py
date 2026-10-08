"""The deterministic tool layer (PRD §6.2). Each call is stored as Evidence and returns
{evidence_id, query, result, computed_at}; the LLM only ever sees these."""
from datetime import date, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from scipy.stats import chi2_contingency

from sentinel.analysis.cube import (
    baseline_cube,
    clean_baseline_days,
    day_cube,
    filt,
    has_day,
    window_days,
)
from sentinel.analysis.drilldown import dims_for, find_causes, segments
from sentinel.analysis.metric_tree import METRICS, decompose, metric_value
from sentinel.detection import baseline_z, robust_z
from sentinel.evidence import EvidenceStore
from sentinel.ingestion import day_file


def _d(x) -> date:
    return x if isinstance(x, date) else date.fromisoformat(x)


class Tools:
    def __init__(self, data_dir: Path, store: EvidenceStore | None = None):
        self.data_dir = Path(data_dir)
        self.store = store or EvidenceStore()

    def _base(self, day: date, baseline: str) -> pd.DataFrame:
        return baseline_cube(self.data_dir, clean_baseline_days(self.data_dir, window_days(day, baseline)))

    def _out(self, tool: str, params: dict, result: dict, query: str | None = None) -> dict:
        ev = self.store.add(tool, params, query, result)
        return {"evidence_id": ev.evidence_id, "query": query or f"{tool}({params})",
                "result": result, "computed_at": ev.computed_at.isoformat()}

    def get_metric(self, metric: str, start: str, end: str, filters: dict | None = None) -> dict:
        s, e = _d(start), _d(end)
        days = [s + timedelta(days=i) for i in range((e - s).days + 1)]
        vals = {str(d): metric_value(filt(day_cube(self.data_dir, d), filters), metric)
                for d in days if has_day(self.data_dir, d)}
        return self._out("get_metric", {"metric": metric, "start": start, "end": end, "filters": filters},
                         {"daily": vals, "mean": float(np.mean(list(vals.values()))) if vals else None})

    def decompose_metric(self, metric: str, date: str, baseline: str = "weekday") -> dict:
        d = _d(date)
        res = decompose(day_cube(self.data_dir, d), self._base(d, baseline), metric)
        return self._out("decompose_metric", {"metric": metric, "date": date, "baseline": baseline}, res)

    def drill_down(self, metric: str, dimension: str, date: str, baseline: str = "weekday",
                   filters: dict | None = None) -> dict:
        d = _d(date)
        if dimension not in dims_for(metric):
            return self._out("drill_down", {"metric": metric, "dimension": dimension},
                             {"error": f"{dimension} is not defined for {metric}"})
        seg = segments(day_cube(self.data_dir, d), self._base(d, baseline), metric, dimension, filters)
        seg = seg[seg.volume >= 0.01]
        return self._out("drill_down", {"metric": metric, "dimension": dimension, "date": date,
                                        "baseline": baseline, "filters": filters},
                         {"segments": seg.round(6).to_dict("records")})

    def find_causes(self, date: str, baseline: str = "weekday", metric: str = "revenue") -> dict:
        """Automatic tree walk + drill-down (what Baseline C runs). Returns ranked candidate causes."""
        d = _d(date)
        z = {m: baseline_z(self.data_dir, d, m)[0] for m in METRICS}
        causes = find_causes(day_cube(self.data_dir, d), self._base(d, baseline), metric, z)
        return self._out("find_causes", {"date": date, "baseline": baseline, "metric": metric},
                         {"causes": causes, "metric_z": z})

    def segment_contribution(self, filters: dict, date: str, baseline: str = "weekday") -> dict:
        """Share of the total revenue change that happened inside the segment (additive, so sets of
        disjoint segments sum to <= 100%; the remainder is unexplained)."""
        d = _d(date)
        now, base = day_cube(self.data_dir, d), self._base(d, baseline)
        seg = filt(now, filters)["R"].sum() - filt(base, filters)["R"].sum()
        total = now["R"].sum() - base["R"].sum()
        return self._out("segment_contribution", {"filters": filters, "date": date, "baseline": baseline},
                         {"revenue_delta_in_segment": float(seg), "revenue_delta_total": float(total),
                          "share": float(seg / total) if total else 0.0})

    def compare_distributions(self, column: str, date: str, baseline: str = "weekday",
                              segment: dict | None = None) -> dict:
        """Chi-square shift of a session column's mix inside `segment`, today vs baseline."""
        d = _d(date)
        now = filt(day_cube(self.data_dir, d), segment).groupby(column)["S"].sum()
        base = filt(self._base(d, baseline), segment).groupby(column)["S"].sum()
        idx = now.index.union(base.index)
        now, base = now.reindex(idx, fill_value=0), base.reindex(idx, fill_value=0)
        p = float(chi2_contingency(np.array([now, base.round()]))[1]) if len(idx) > 1 else 1.0
        shares = pd.DataFrame({"today": now / now.sum(), "baseline": base / base.sum()}).round(4)
        return self._out("compare_distributions", {"column": column, "date": date, "segment": segment},
                         {"p_value": p, "shares": shares.to_dict("index")})

    def check_traffic(self, date: str, baseline: str = "weekday", filters: dict | None = None) -> dict:
        d = _d(date)
        days = clean_baseline_days(self.data_dir, window_days(d, baseline))
        hist = [float(filt(day_cube(self.data_dir, x), filters)["S"].sum()) for x in days]
        v = float(filt(day_cube(self.data_dir, d), filters)["S"].sum())
        z, med = robust_z(v, hist)
        return self._out("check_traffic", {"date": date, "baseline": baseline, "filters": filters},
                         {"sessions": v, "baseline": med, "pct_change": v / med - 1 if med else None,
                          "z": z, "within_normal": abs(z) < 2})

    def get_events(self, event_type: str, date: str, filters: dict | None = None,
                   group_by: str = "error_code") -> dict:
        if group_by not in ("error_code", "payment_method", "category"):
            raise ValueError(group_by)
        d = _d(date)
        where = "".join(f" and s.{k} = ?" for k in (filters or {}))
        sql = (f"select coalesce(e.{group_by}, 'none') g, count(*) n from read_parquet(?) e join "
               f"read_parquet(?) s using(session_id) where e.event_type = ?{where} group by 1 order by 2 desc")
        args = [day_file(self.data_dir, "events", d).as_posix(),
                day_file(self.data_dir, "sessions", d).as_posix(), event_type, *(filters or {}).values()]
        rows = duckdb.connect().execute(sql, args).fetchall()
        return self._out("get_events", {"event_type": event_type, "date": date, "filters": filters,
                                        "group_by": group_by}, {"counts": dict(rows)}, query=sql)

    def correlate(self, metric_a: str, metric_b: str, end: str, window: int = 28) -> dict:
        e = _d(end)
        days = [e - timedelta(days=i) for i in range(window)][::-1]
        days = [x for x in days if has_day(self.data_dir, x)]
        a = [metric_value(day_cube(self.data_dir, x), metric_a) for x in days]
        b = [metric_value(day_cube(self.data_dir, x), metric_b) for x in days]
        r = float(np.corrcoef(a, b)[0, 1]) if len(days) > 2 else None
        return self._out("correlate", {"metric_a": metric_a, "metric_b": metric_b, "end": end,
                                       "window": window}, {"pearson_r": r, "n_days": len(days)})
