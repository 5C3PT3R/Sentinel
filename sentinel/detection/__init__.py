"""Robust-z change detection against two baselines (same weekday x4, trailing 7); smaller |z| wins."""
from datetime import date
from pathlib import Path

import numpy as np

from sentinel.analysis.cube import clean_baseline_days, day_cube, window_days
from sentinel.analysis.metric_tree import METRICS, metric_value
from sentinel.config import load_settings
from sentinel.models import Anomaly

SIGMA_FLOOR = 0.03  # of |median|; D2


def robust_z(x: float, hist: list[float]) -> tuple[float, float]:
    med = float(np.median(hist))
    sigma = max(1.4826 * float(np.median(np.abs(np.array(hist) - med))), SIGMA_FLOOR * abs(med))
    return (x - med) / sigma if sigma else 0.0, med


def baseline_z(data_dir: Path, day: date, metric: str, today=None) -> tuple[float, float, str] | None:
    """(z, baseline median, baseline kind) against the more conservative of the two baselines."""
    cfg = load_settings()["detection"]
    v = metric_value(today if today is not None else day_cube(data_dir, day), metric)
    best = None
    for kind in ("weekday", "trailing7"):
        days = clean_baseline_days(data_dir, window_days(day, kind, cfg["baseline_weeks"]))
        hist = [metric_value(day_cube(data_dir, d), metric) for d in days]
        if len(hist) < 2:
            continue
        z, med = robust_z(v, hist)
        if best is None or abs(z) < abs(best[0]):
            best = (z, med, kind)
    return best


def detect(data_dir: Path, day: date, store=None, metrics: list[str] | None = None,
           min_rel_change: float | None = None, z_min: float = 2.0) -> list[Anomaly]:
    cfg = load_settings()["detection"]
    min_rel = min_rel_change if min_rel_change is not None else cfg["min_rel_change"]
    if day in load_settings()["simulator_extra"]["sale_days"]:
        return []  # known calendar event, not an anomaly (D20)
    today = day_cube(data_dir, day)
    out = []
    for m in metrics or list(METRICS):
        best = baseline_z(data_dir, day, m, today)
        if best is None or not best[1]:
            continue
        z, med, kind = best
        v = metric_value(today, m)
        pct = (v - med) / med
        if abs(pct) < min_rel or abs(z) < z_min:
            continue
        sev = "high" if abs(z) >= 5 or abs(pct) >= 0.15 else "medium" if abs(z) >= 3 else "low"
        ev = store.add("detect", {"metric": m, "date": str(day), "baseline": kind}, None,
                       {"value": v, "baseline": med, "z": z, "pct_change": pct}).evidence_id if store else ""
        out.append(Anomaly(metric=m, date=day, value=v, baseline=med, pct_change=pct,
                           z_score=z, severity=sev, evidence_id=ev))
    return out
