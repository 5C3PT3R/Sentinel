from datetime import date
from pathlib import Path

import pytest

from sentinel.agents.investigation import investigate
from sentinel.analysis.tools import Tools
from sentinel.detection import detect
from sentinel.evidence import EvidenceStore
from sentinel.evidence.verifier import unsupported_numbers
from sentinel.llm import LLMClient

DATA = Path("data/sim")
DAY = date(2026, 4, 1)  # S11: electronics stockout
needs_sim = pytest.mark.skipif(not DATA.exists(), reason="run `sentinel simulate --out data/sim` first")


class Script:
    """Stands in for the LLM: each step is a function (tools) -> list of content blocks."""

    def __init__(self, tools, steps):
        self.tools, self.steps, self.n = tools, steps, 0
        self.seen_tools = []

    def create(self, system, messages, tools, tool_choice=None, max_tokens=4096):
        self.seen_tools.append([t["name"] for t in tools])
        step = self.steps[min(self.n, len(self.steps) - 1)]
        self.n += 1
        return {"content": step(self.tools, messages), "stop_reason": "tool_use", "usage": {}}


def use(name, **kw):
    return lambda t, m: [{"type": "tool_use", "id": f"t{len(m)}", "name": name, "input": kw}]


def submit(statement_fn, filters=None, ids=lambda t: list(t.store.items)[-1:]):
    def step(t, m):
        h = {"statement": statement_fn(t), "segment_filters": filters if filters is not None
             else {"category": "electronics"}, "chain": ids(t), "evidence_ids": ids(t)}
        return [{"type": "tool_use", "id": f"s{len(m)}", "name": "submit_hypotheses",
                 "input": {"hypotheses": [h]}}]
    return step


def atc_pct(t):  # electronics add-to-cart rate today, taken from the drill_down evidence
    ev = next(e for e in t.store.items.values() if e.tool == "drill_down")
    row = next(r for r in ev.result["segments"] if r["segment"] == "electronics")
    return f"{row['value'] * 100:.1f}%"


def setup():
    tools = Tools(DATA)
    return tools, detect(DATA, DAY, tools.store, metrics=["revenue"])


STEPS = [use("decompose_metric", metric="revenue", date=str(DAY)),
         use("drill_down", metric="atc_rate", dimension="category", date=str(DAY))]


@needs_sim
def test_happy_path_hypothesis_is_cited_and_contribution_computed():
    tools, an = setup()
    fake = Script(tools, STEPS + [submit(lambda t: f"Electronics add-to-cart rate fell to {atc_pct(t)}.")])
    res = investigate(tools, fake, DAY, an)
    assert len(res.hypotheses) == 1 and not res.dropped
    h = res.hypotheses[0]
    assert all(i in tools.store.items for i in h.evidence_ids)
    assert 0 < h.contribution_pct <= 100 and abs(res.unexplained_pct + h.contribution_pct - 100) < 1e-9
    assert res.tool_calls == 2


@needs_sim
def test_invented_number_is_bounced_then_dropped_if_not_fixed():
    tools, an = setup()
    bad = submit(lambda t: "Electronics add-to-cart rate fell to 73.9%.")
    fake = Script(tools, STEPS + [bad])  # model keeps resubmitting the same bad number
    res = investigate(tools, fake, DAY, an)
    assert res.hypotheses == [] and "73.9%" in res.dropped[0]
    tools, an = setup()
    fake = Script(tools, STEPS + [bad, submit(lambda t: f"Electronics add-to-cart rate fell to {atc_pct(t)}.")])
    assert len(investigate(tools, fake, DAY, an).hypotheses) == 1  # corrected on retry


@needs_sim
def test_uncited_hypothesis_dropped():
    tools, an = setup()
    fake = Script(tools, STEPS + [submit(lambda t: "Electronics broke.", ids=lambda t: ["E999"])])
    res = investigate(tools, fake, DAY, an)
    assert res.hypotheses == [] and "no valid evidence" in res.dropped[0]


@needs_sim
def test_budget_forces_submission():
    tools, an = setup()
    fake = Script(tools, [use("check_traffic", date=str(DAY)), submit(lambda t: "Electronics broke.")])
    res = investigate(tools, fake, DAY, an, budget=1)
    assert res.budget_exhausted and res.tool_calls == 1
    assert fake.seen_tools[-1] == ["submit_hypotheses"]  # only submit offered once spent


def test_verifier_rules():
    st = EvidenceStore()
    e = st.add("t", {"date": "2026-04-01"}, None, {"share": 0.2413, "n": 1234.0})
    ok = "On 2026-04-01 share was 24% (0.24) with 1,234 orders on app 5.3.0 per E001."
    assert unsupported_numbers(ok, st, [e.evidence_id]) == []
    assert unsupported_numbers("share was 31%", st, [e.evidence_id]) == ["31"]


def test_llm_cache_hits_skip_api(tmp_path):
    class Msg:
        def model_dump(self, **kw):
            return {"type": "text", "text": "hi"}

    class Api:
        calls = 0

        class messages:
            @staticmethod
            def create(**kw):
                Api.calls += 1
                u = type("U", (), {"input_tokens": 3, "output_tokens": 2})
                return type("R", (), {"content": [Msg()], "stop_reason": "end_turn", "usage": u})

    c = LLMClient(tmp_path, api=Api)
    a = c.create("sys", [{"role": "user", "content": "x"}], [])
    b = c.create("sys", [{"role": "user", "content": "x"}], [])
    assert a == b and Api.calls == 1 and c.cache_hits == 1
