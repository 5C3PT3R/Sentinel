import json

import pandas as pd

from sentinel.simulator.generator import simulate
from sentinel.simulator.injector import load_scenarios


def test_at_least_25_scenarios_with_unique_dates():
    sc = load_scenarios("scenarios")
    assert len(sc) >= 25 and len({s["date"] for s in sc}) == len(sc)


def test_simulate_writes_data_and_ground_truth(tmp_path):
    r = simulate("scenarios", str(tmp_path), days=50, sessions=400, seed=1)
    assert r["scenarios"] == 2  # S01 (Feb 10), S02 (Feb 15) fall in the first 50 days
    truth = json.loads((tmp_path / "ground_truth.json").read_text())
    assert truth[0]["id"] == "S01" and truth[0]["effect_revenue_delta"] < 0
    assert len(list((tmp_path / "events").glob("*.parquet"))) == 50


def test_schema_change_renames_column(tmp_path):
    simulate("scenarios", str(tmp_path), days=112, sessions=300, seed=1)  # S15 is 2026-04-21 (day 110)
    cols = pd.read_parquet(tmp_path / "events" / "2026-04-21.parquet").columns
    assert "err_code" in cols and "error_code" not in cols
