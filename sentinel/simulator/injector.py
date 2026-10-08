"""Scenario loading and effect application.

An effect is {type, filters, params}. Business effects are generic: the type is only a label,
the behaviour comes from params (traffic_factor, atc_mult, checkout_mult, attempt_mult,
amount_mult, failure_rate_to). Data-issue effects (tracking_bug, schema_change, late_data,
duplicate_events, null_spike) mutate the finished tables instead.
"""
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

DATA_ISSUES = {"tracking_bug", "schema_change", "late_data", "duplicate_events", "null_spike"}


def load_scenarios(path: str | Path) -> list[dict]:
    out = []
    for f in sorted(Path(path).glob("*.yaml")):
        d = yaml.safe_load(f.read_text())
        out += d if isinstance(d, list) else [d]
    return out


def effects_of(scenario: dict) -> list[dict]:
    return scenario.get("effects") or [scenario]


def mask(df: pd.DataFrame, filters: dict | None) -> np.ndarray:
    m = np.ones(len(df), dtype=bool)
    for k, v in (filters or {}).items():
        m &= (df[k] == v).to_numpy()
    return m


def _business(effects: list[dict]):
    return (e for e in effects if e["type"] not in DATA_ISSUES)


def apply_traffic(s: pd.DataFrame, effects: list[dict], rng) -> pd.DataFrame:
    """Thin (factor<1) or duplicate (factor>1) the sessions an effect targets."""
    for e in _business(effects):
        f = e.get("params", {}).get("traffic_factor")
        if f is None:
            continue
        idx = np.flatnonzero(mask(s, e.get("filters")))
        if f < 1:
            s = s.drop(s.index[idx[rng.random(len(idx)) > f]])
        else:
            extra = rng.choice(idx, round((f - 1) * len(idx)), replace=True)
            s = pd.concat([s, s.iloc[extra]])
        s = s.reset_index(drop=True)
    return s


def multiplier(s: pd.DataFrame, effects: list[dict], key: str) -> np.ndarray:
    out = np.ones(len(s))
    for e in _business(effects):
        if key in e.get("params", {}):
            out[mask(s, e.get("filters"))] *= e["params"][key]
    return out


def failure_override(s: pd.DataFrame, effects: list[dict]):
    """Per-session forced failure rate (NaN = method's base rate) and spike error code ('' = none)."""
    rate = np.full(len(s), np.nan)
    code = np.full(len(s), "", dtype=object)
    for e in _business(effects):
        p = e.get("params", {})
        if "failure_rate_to" in p:
            m = mask(s, e.get("filters"))
            rate[m] = p["failure_rate_to"]
            code[m] = p.get("error_code", "GATEWAY_TIMEOUT")
    return rate, code


def apply_data_issues(t: dict[str, pd.DataFrame], effects: list[dict], rng) -> dict:
    for e in effects:
        p, ty = e.get("params", {}), e["type"]
        if ty == "tracking_bug":
            ev = t["events"].merge(t["sessions"][["session_id", "os", "platform"]], on="session_id")
            hit = mask(ev, e.get("filters")) & (ev.event_type == p["event_type"])
            hit &= rng.random(len(ev)) < p.get("drop_rate", 1.0)
            t["events"] = ev[~hit].drop(columns=["os", "platform"]).reset_index(drop=True)
        elif ty == "schema_change":
            t[p["table"]] = t[p["table"]].rename(columns=p["rename"])
        elif ty == "late_data":
            for name in p["tables"]:
                t[name] = t[name].sample(frac=p["keep_fraction"], random_state=int(rng.integers(1 << 31)))
        elif ty == "duplicate_events":
            dup = t["events"].sample(frac=p["dup_rate"], random_state=int(rng.integers(1 << 31)))
            t["events"] = pd.concat([t["events"], dup])
        elif ty == "null_spike":
            df = t[p["table"]].copy()
            df[p["column"]] = df[p["column"]].mask(rng.random(len(df)) < p["null_rate"])
            t[p["table"]] = df
    return t
