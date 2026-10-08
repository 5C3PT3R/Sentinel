"""Synthetic e-commerce event generator. One parquet file per table per day."""
import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from sentinel.config import load_settings
from sentinel.simulator import injector as inj

DOW = [1.0, 0.97, 0.95, 0.97, 1.05, 1.15, 1.10]  # Mon..Sun
# traffic_source: (share, conversion multiplier)
SRC = {"organic": (0.35, 1.0), "paid": (0.30, 0.6), "email": (0.10, 1.5), "direct": (0.15, 1.2), "social": (0.10, 0.5)}
PLAT = {"web": 0.40, "android": 0.38, "ios": 0.22}
OS = {"web": (["windows", "macos"], [0.7, 0.3]), "android": (["android"], [1]), "ios": (["ios"], [1])}
VER = {"web": ["web"], "android": ["5.2.0", "5.3.0"], "ios": ["5.2.0", "5.3.0"]}
COUNTRY = {"IN": 0.5, "US": 0.2, "BR": 0.12, "UK": 0.1, "DE": 0.08}
# category: (share, mean price)
CAT = {"electronics": (0.2, 180), "fashion": (0.3, 45), "home": (0.2, 70), "beauty": (0.15, 25), "grocery": (0.15, 30)}
PAY = {"IN": {"upi": 0.5, "card": 0.25, "wallet": 0.1, "cod": 0.15}, "other": {"card": 0.75, "wallet": 0.25}}
BASE_FAIL = {"upi": 0.04, "card": 0.03, "wallet": 0.03, "cod": 0.0}
ERR = {"upi": ["UPI_TIMEOUT", "UPI_DECLINED"], "card": ["CARD_DECLINED", "3DS_FAIL"],
       "wallet": ["WALLET_LOW_BALANCE"], "cod": ["COD_REJECTED"]}


def _pick(rng, d: dict, n: int) -> np.ndarray:
    p = np.array([v if np.isscalar(v) else v[0] for v in d.values()], dtype=float)
    return rng.choice(list(d), n, p=p / p.sum())


def _sessions(n: int, day: date, rng) -> pd.DataFrame:
    s = pd.DataFrame({"platform": _pick(rng, PLAT, n), "country": _pick(rng, COUNTRY, n),
                      "traffic_source": _pick(rng, SRC, n), "category": _pick(rng, CAT, n)})
    s["ts"] = pd.Timestamp(day) + pd.to_timedelta(rng.integers(0, 86400, n), unit="s")
    s["new_vs_returning"] = np.where(rng.random(n) < 0.6, "new", "returning")
    s["os"], s["app_version"], s["payment_method"] = "", "", ""
    for p, (oses, w) in OS.items():
        m = (s.platform == p).to_numpy()
        s.loc[m, "os"] = rng.choice(oses, m.sum(), p=w)
        s.loc[m, "app_version"] = rng.choice(VER[p], m.sum())
    s["device_type"] = np.where(s.platform == "web", np.where(rng.random(n) < 0.6, "desktop", "mobile"), "mobile")
    for c in COUNTRY:
        m = (s.country == c).to_numpy()
        s.loc[m, "payment_method"] = _pick(rng, PAY["IN" if c == "IN" else "other"], m.sum())
    return s


def _funnel(s: pd.DataFrame, fx: list[dict], rng) -> dict[str, pd.DataFrame]:
    n = len(s)
    src = s.traffic_source.map({k: v[1] for k, v in SRC.items()}).to_numpy()
    nvr = np.where(s.new_vs_returning == "new", 0.8, 1.3)
    p_atc = np.clip(0.12 * src * nvr * inj.multiplier(s, fx, "atc_mult"), 0, 1)
    u = rng.random((4, n))
    atc = u[0] < p_atc
    cs = atc & (u[1] < 0.55 * inj.multiplier(s, fx, "checkout_mult"))
    pa = cs & (u[2] < 0.9 * inj.multiplier(s, fx, "attempt_mult"))
    forced, spike_code = inj.failure_override(s, fx)
    fail_rate = np.where(np.isnan(forced), s.payment_method.map(BASE_FAIL).to_numpy(), forced)
    ps = pa & (u[3] >= fail_rate)
    pf = pa & ~ps
    code = np.array([rng.choice(ERR[m]) for m in s.payment_method[pf]], dtype=object)
    use = (spike_code[pf] != "") & (rng.random(len(code)) < 0.8)  # spike code on 80% of spiked failures
    code[use] = spike_code[pf][use]

    def ev(m, typ, off, err=None):
        d = s.loc[m, ["session_id", "ts", "category"]].copy()
        d["ts"] += pd.to_timedelta(off, unit="s")
        d["event_type"], d["error_code"] = typ, err
        d["payment_method"] = s.loc[m, "payment_method"] if typ.startswith("payment") else None
        return d

    every = np.ones(n, bool)
    events = pd.concat([ev(every, "view", 0), ev(atc, "add_to_cart", 30), ev(cs, "checkout_start", 90),
                        ev(pa, "payment_attempt", 120), ev(ps, "payment_success", 125),
                        ev(pf, "payment_failure", 125, code)])
    events = events.sort_values("ts", kind="stable").reset_index(drop=True)
    events.insert(0, "event_id", s.session_id.iloc[0][:8] + "-e" + events.index.astype(str))
    price = s.category.map({k: v[1] for k, v in CAT.items()}).to_numpy()
    items = 1 + rng.poisson(0.5, n)
    amount = price * rng.lognormal(0, 0.4, n) * items * inj.multiplier(s, fx, "amount_mult")
    o = s.loc[ps, ["session_id", "ts", "payment_method"]].copy()
    o["ts"] += pd.to_timedelta(130, unit="s")
    o["amount"], o["items"] = amount[ps].round(2), items[ps]
    o.insert(0, "order_id", o.session_id.str.replace("-", "-o", n=1))
    return {"events": events, "orders": o.reset_index(drop=True)}


def gen_day(day: date, cfg: dict, base_fx: list[dict], scen_fx: list[dict], seed: int):
    """Returns (tables, true_revenue). Business effects shape the data; data-issue effects corrupt it after."""
    rng = np.random.default_rng([seed, day.toordinal()])
    fx_rng = np.random.default_rng([seed, day.toordinal(), 1])
    fx = base_fx + scen_fx
    d = (day - cfg["start"]).days
    n = int(cfg["sessions"] * DOW[day.weekday()] * (1 + 0.0005 * d) * rng.lognormal(0, 0.03))
    s = inj.apply_traffic(_sessions(n, day, rng), fx, fx_rng)
    s["session_id"] = day.strftime("%Y%m%d") + "-" + s.index.astype(str)
    t = _funnel(s, fx, rng)
    revenue = float(t["orders"].amount.sum())
    t["sessions"] = s.drop(columns="payment_method")
    return inj.apply_data_issues(t, scen_fx, fx_rng), revenue


TRUTH_KEYS = ("id", "type", "category", "expected_root_cause", "expected_filters", "expected_dq_status",
              "expected_detect")


def simulate(scenarios_dir: str, out: str | None = None, days: int | None = None,
             sessions: int | None = None, seed: int | None = None, plot: bool = False) -> dict:
    st = load_settings()
    ex = st["simulator_extra"]
    cfg = {"start": ex["start_date"], "sessions": sessions or st["simulator"]["sessions_per_day"]}
    days, seed = days or st["simulator"]["days"], seed if seed is not None else st["seed"]
    out = Path(out or ex["out_dir"])
    sale_fx = [{"type": "sale", "filters": {}, "params": {"traffic_factor": 1.5, "atc_mult": 1.2}}]
    sale = {d: sale_fx for d in ex["sale_days"]}
    scen = {s["date"]: s for s in inj.load_scenarios(scenarios_dir)}
    for name in ("sessions", "events", "orders"):
        (out / name).mkdir(parents=True, exist_ok=True)
    daily, truth = [], []
    for i in range(days):
        day = cfg["start"] + timedelta(days=i)
        sc = scen.get(day)
        t, rev = gen_day(day, cfg, sale.get(day, []), inj.effects_of(sc) if sc else [], seed)
        for name, df in t.items():
            df.to_parquet(out / name / f"{day}.parquet", index=False)
        daily.append((day, rev))
        if sc:  # counterfactual: same day without the scenario gives the true effect size
            _, cf = gen_day(day, cfg, sale.get(day, []), [], seed)
            truth.append({k: sc.get(k) for k in TRUTH_KEYS} | {
                "date": str(day), "true_revenue": round(rev, 2), "counterfactual_revenue": round(cf, 2),
                "effect_revenue_delta": round(rev - cf, 2)})
    (out / "ground_truth.json").write_text(json.dumps(truth, indent=2))
    if plot:
        _plot(daily, truth, out / "revenue.png")
    return {"days": days, "scenarios": len(truth), "out": str(out)}


def _plot(daily, truth, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(14, 4))
    ax.plot(*zip(*daily), lw=1)
    for t in truth:
        ax.axvline(date.fromisoformat(t["date"]), color="r" if t["category"] != "none" else "g", alpha=0.4, lw=0.8)
    ax.set_title("Daily revenue (red = injected anomaly, green = no-anomaly scenario)")
    fig.savefig(path, dpi=100, bbox_inches="tight")
