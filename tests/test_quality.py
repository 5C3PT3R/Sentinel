import json
from datetime import date
from pathlib import Path

import pytest

from sentinel.ingestion import available_days
from sentinel.quality import check_day

DATA = Path("data/sim")
pytestmark = pytest.mark.skipif(not DATA.exists(), reason="run `sentinel simulate --out data/sim` first")


def test_dq_matches_ground_truth():
    truth = {date.fromisoformat(t["date"]): t["expected_dq_status"]
             for t in json.loads((DATA / "ground_truth.json").read_text())
             if t["expected_dq_status"] != "PASS"}
    wrong = []
    for d in available_days(DATA)[1:]:  # day 1 has no history
        got = check_day(DATA, d)
        if got.status != truth.get(d, "PASS"):
            wrong.append((str(d), got.status, truth.get(d, "PASS"), [i.detail for i in got.issues]))
    assert not wrong, wrong
