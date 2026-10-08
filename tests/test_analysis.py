import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from sentinel.analysis.cube import baseline_cube, clean_baseline_days, day_cube, window_days
from sentinel.analysis.drilldown import find_causes, segments
from sentinel.analysis.metric_tree import METRICS, decompose
from sentinel.analysis.tools import Tools
from sentinel.detection import baseline_z

DATA = Path("data/sim")
needs_sim = pytest.mark.skipif(not DATA.exists(), reason="run `sentinel simulate --out data/sim` first")


def _cube(rows):
    cols = ["platform", "os", "app_version", "country", "traffic_source", "device_type",
            "new_vs_returning", "category", "payment_method"]
    return pd.DataFrame([{**dict.fromkeys(cols, "x"), **r} for r in rows])


def test_lmdi_and_segments_reconcile():
    base = _cube([{"os": "a", "S": 1000, "A": 300, "C": 150, "P": 120, "O": 100, "R": 10000},
                  {"os": "b", "S": 1000, "A": 300, "C": 150, "P": 120, "O": 100, "R": 10000}])
    now = _cube([{"os": "a", "S": 1000, "A": 300, "C": 150, "P": 120, "O": 40, "R": 4000},
                 {"os": "b", "S": 1000, "A": 300, "C": 150, "P": 120, "O": 100, "R": 10000}])
    for m in ("revenue", "orders", "conversion_rate"):
        dec = decompose(now, base, m)
        assert abs(sum(c["share"] for c in dec["children"]) - 1) < 1e-9
    for m in METRICS:  # additive and ratio metrics alike
        seg = segments(now, base, m, "os")
        assert abs(seg["share"].sum() - 1) < 1e-9 or seg["share"].abs().sum() == 0
    assert segments(now, base, "payment_success_rate", "os").iloc[0].segment == "a"


def test_flat_parent_is_skipped():
    c = _cube([{"S": 10, "A": 5, "C": 4, "P": 3, "O": 2, "R": 20}])
    assert decompose(c, c, "revenue")["children"] == []


@needs_sim
def test_baseline_c_ranks_true_cause_in_top3():
    truth = [t for t in json.loads((DATA / "ground_truth.json").read_text())
             if t["category"] == "business" and len(t["expected_filters"]) == 1]

    def norm(f):  # platform android/ios is the same population as os android/ios
        return frozenset(("os", v) if k == "platform" and v in ("ios", "android") else (k, v)
                         for k, v in f.items())

    hits = 0
    for t in truth:
        d = date.fromisoformat(t["date"])
        base = baseline_cube(DATA, clean_baseline_days(DATA, window_days(d, "weekday")))
        z = {m: baseline_z(DATA, d, m)[0] for m in METRICS}
        top = [norm(c["filters"]) for c in find_causes(day_cube(DATA, d), base, "revenue", z)[:3]]
        hits += norm(t["expected_filters"][0]) in top
    assert hits / len(truth) >= 0.7, (hits, len(truth))


@needs_sim
def test_tools_store_evidence(tmp_path):
    t = Tools(DATA)
    r = t.find_causes("2026-04-01")
    assert r["result"]["causes"][0]["filters"] == {"category": "electronics"}
    assert t.check_traffic("2026-04-11")["result"]["within_normal"]
    t.get_events("payment_failure", "2026-02-10", group_by="error_code")
    t.compare_distributions("os", "2026-02-10")
    t.correlate("sessions", "orders", "2026-04-01")
    assert t.get_metric("revenue", "2026-04-01", "2026-04-03")["result"]["mean"] > 0
    assert t.drill_down("revenue", "category", "2026-04-01")["result"]["segments"]
    t.store.save(tmp_path / "evidence.json")
    assert r["evidence_id"] == "E001" and len(t.store.items) == 7
