"""Metric definitions (numerator/denominator over cube counts) and LMDI decomposition (DECISIONS D1, D3)."""
import math

import pandas as pd
import yaml

from sentinel.analysis.cube import COUNTS
from sentinel.config import ROOT

# name: (numerator, denominator or None). Every product in the tree telescopes exactly.
METRICS = {
    "revenue": ("R", None), "orders": ("O", None), "sessions": ("S", None),
    "aov": ("R", "O"), "conversion_rate": ("O", "S"), "atc_rate": ("A", "S"),
    "checkout_start_rate": ("C", "A"), "payment_attempt_rate": ("P", "C"),
    "payment_success_rate": ("O", "P"),
}
TREE = yaml.safe_load((ROOT / "config" / "metric_tree.yaml").read_text())
# payment_method only exists once a session attempts payment, so these can't be split by it
NO_PM = {"sessions", "conversion_rate", "atc_rate", "checkout_start_rate", "payment_attempt_rate"}


def totals(cube: pd.DataFrame) -> pd.Series:
    return cube[COUNTS].sum()


def value(t: pd.Series, metric: str) -> float:
    n, d = METRICS[metric]
    if d is None:
        return float(t[n])
    return float(t[n] / t[d]) if t[d] else 0.0


def metric_value(cube: pd.DataFrame, metric: str) -> float:
    return value(totals(cube), metric)


def _logmean(a: float, b: float) -> float:
    return a if abs(a - b) < 1e-12 * max(abs(a), 1.0) else (a - b) / (math.log(a) - math.log(b))


def decompose(now: pd.DataFrame, base: pd.DataFrame, metric: str, min_rel_change: float = 0.0) -> dict:
    """One level of the tree. Child shares sum to 1 (LMDI); no children if leaf or flat parent (D1)."""
    kids = TREE.get(metric, {}).get("children", [])
    tn, tb = totals(now), totals(base)
    p1, p0 = value(tn, metric), value(tb, metric)
    out = {"metric": metric, "value": p1, "baseline": p0, "delta": p1 - p0, "children": []}
    if not kids:
        return out
    out["material"] = p0 > 0 and p1 > 0 and abs(p1 / p0 - 1) > max(min_rel_change, 1e-9)
    if not out["material"]:
        return out
    L = _logmean(p1, p0)
    for k in kids:
        x1, x0 = max(value(tn, k), 1e-12), max(value(tb, k), 1e-12)
        c = L * math.log(x1 / x0)
        out["children"].append({"metric": k, "value": value(tn, k), "baseline": value(tb, k),
                                "contribution": c, "share": c / (p1 - p0)})
    return out
