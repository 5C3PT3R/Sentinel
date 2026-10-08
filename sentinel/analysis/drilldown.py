"""Adtributor-style drill-down: ratio-aware segment contributions, plus the automatic cause search."""
import pandas as pd

from sentinel.analysis.cube import COUNTS, DIMS, filt
from sentinel.analysis.metric_tree import METRICS, NO_PM, TREE, decompose

MIN_VOLUME = 0.01  # prune segments under 1% of the parent's baseline volume
MIN_SHARE = 0.20  # a segment must explain this much of the parent change to count
DOMINANT = 0.70  # ...and this much to replace the parent as the better (more specific) description
MIN_COUNT = 30  # baseline events behind a segment; smaller slices are all noise
MIN_REL = 0.05  # a segment (or leaf) must itself move at least this much, else it is noise
MIN_LIFT = 1.5  # share / volume share; below this the change is spread evenly, not localized
Z_MIN = 2.0
MIN_ZSTAT = 3.0  # sampling-noise gate for a segment's own move (NaN = no test, passes)
MAX_DEPTH = 4
TOP_K = 3


def dims_for(metric: str, exclude=frozenset()) -> list[str]:
    return [d for d in DIMS if d not in exclude and not (d == "payment_method" and metric in NO_PM)]


def segments(now: pd.DataFrame, base: pd.DataFrame, metric: str, dim: str,
             filters: dict | None = None) -> pd.DataFrame:
    """Per-segment value/delta/share. Ratio metrics split into rate_effect + mix_effect (Simpson-safe)."""
    n, d = METRICS[metric]
    g1 = filt(now, filters).groupby(dim)[COUNTS].sum()
    g0 = filt(base, filters).groupby(dim)[COUNTS].sum()
    idx = g1.index.union(g0.index)
    g1, g0 = g1.reindex(idx, fill_value=0.0), g0.reindex(idx, fill_value=0.0)
    out = pd.DataFrame(index=idx)
    nan = float("nan")
    if d is None:
        out["baseline"], out["value"] = g0[n], g1[n]
        out["delta"] = out["value"] - out["baseline"]
        out["rate_effect"], out["mix_effect"] = out["delta"], 0.0
        total = out["delta"].sum()
        vol = out["baseline"] / max(out["baseline"].sum(), 1e-12)
        rel = out["value"] / out["baseline"].replace(0, nan) - 1
        out["zstat"] = out["delta"] / out["baseline"].clip(lower=1).pow(0.5) if n != "R" else nan  # Poisson
        parent_rel = out["value"].sum() / max(out["baseline"].sum(), 1e-12) - 1
    else:
        D1, D0 = g1[d], g0[d]
        r1s, r0s = g1[n] / D1.replace(0, nan), g0[n] / D0.replace(0, nan)
        r1s, r0s = r1s.fillna(r0s).fillna(0.0), r0s.fillna(r1s).fillna(0.0)  # missing side: no rate effect
        w1, w0 = D1 / max(D1.sum(), 1e-12), D0 / max(D0.sum(), 1e-12)
        R1, R0 = g1[n].sum() / max(D1.sum(), 1e-12), g0[n].sum() / max(D0.sum(), 1e-12)
        out["baseline"], out["value"] = r0s, r1s
        out["rate_effect"] = w0 * (r1s - r0s)
        out["mix_effect"] = (w1 - w0) * (r1s - R0)
        out["delta"] = out["rate_effect"] + out["mix_effect"]
        total, vol = R1 - R0, w0
        rel = r1s / r0s.replace(0, nan) - 1
        if n == "R":  # AOV: no simple sampling model, rely on the other filters
            out["zstat"] = nan
        else:  # binomial: is the segment's rate move bigger than sampling noise at today's volume?
            out["zstat"] = (r1s - r0s) / (r0s * (1 - r0s)).clip(lower=1e-4).div(D1.clip(lower=1)).pow(0.5)
        parent_rel = R1 / R0 - 1 if R0 else 0.0
    out["count"] = g0[d if d else n]  # baseline volume behind the segment
    out["share"] = out["delta"] / total if abs(total) > 1e-12 else 0.0
    out["volume"] = vol
    out["rel"] = rel.fillna(0.0)
    out["surprise"] = out["rel"] - parent_rel
    out["lift"] = out["share"] / vol.replace(0, nan)
    out.index.name = "segment"
    return out.reset_index().sort_values("share", ascending=False).reset_index(drop=True)


def _candidates(now, base, metric, filters):
    rows = []
    for dim in dims_for(metric, frozenset(filters)):
        s = segments(now, base, metric, dim, filters)
        s = s[(s.volume >= MIN_VOLUME) & (s['count'] >= MIN_COUNT) & (s.volume < 0.9) & (s.share >= MIN_SHARE) & (s.lift >= MIN_LIFT)
               & (s.rel.abs() >= MIN_REL) & ~(s.zstat.abs() < MIN_ZSTAT)]
        rows += [(dim, r.segment, r.share, r.lift, abs(r.mix_effect) > abs(r.rate_effect))
                 for r in s.itertuples()]
    return sorted(rows, key=lambda r: -r[3])


def _expand(now, base, metric, filters, prob, depth, path):
    """Terminal descriptions of a node's change: descend into a dominant child, else spread over top-k."""
    kids = _candidates(now, base, metric, filters) if depth < MAX_DEPTH else []
    if not kids:
        return [{"filters": filters, "contribution": prob, "path": path}] if filters else []
    dom = [k for k in kids if k[2] >= DOMINANT]
    out = []
    for dim, seg, share, _, mix in (dom[:1] or kids[:TOP_K]):
        f, p = {**filters, dim: seg}, path + [f"{dim}={seg}"]
        if mix:  # mix shift: the segment's own rates are unchanged, so don't hunt for a cause inside it
            out.append({"filters": f, "contribution": prob * share, "path": p, "mix_shift": True})
        else:
            out += _expand(now, base, metric, f, prob * share, depth + 1, p)
    return out


def find_causes(now: pd.DataFrame, base: pd.DataFrame, metric: str = "revenue",
                z: dict | None = None) -> list[dict]:
    """Walk the metric tree to its leaves, then drill each moved leaf. Ranked by contribution.

    `z` maps metric -> robust z vs its own history; nodes below Z_MIN are noise (heavy-tailed AOV can
    swing 10% by chance and would otherwise soak up the revenue change) and are not followed.
    """
    cands = []

    def sig(k):  # a leaf must have really moved, and by more than its own day-to-day noise
        if abs(k["value"] / k["baseline"] - 1) < 0.03:
            return False
        return abs(z[k["metric"]]) >= Z_MIN if z else k["share"] >= MIN_SHARE

    def add(node, chain, path):  # localize a leaf by segments, else it's a global change
        found = _expand(now, base, node, {}, chain, 0, []) or [
            {"filters": {}, "contribution": chain, "path": []}]
        cands.extend({**c, "leaf": node, "tree_path": path + [node],
                      "contribution": min(c["contribution"], 1.0)} for c in found)

    def walk(node, chain, path):
        for k in decompose(now, base, node)["children"]:
            if k["share"] <= 0:
                continue  # moved the other way: offsets the change, doesn't cause it
            if k["metric"] in TREE:
                walk(k["metric"], chain * k["share"], path + [node])
            elif sig(k):
                add(k["metric"], chain * k["share"], path + [node])

    walk(metric, 1.0, [])
    if not cands:  # nothing in the tree stands out from noise: drill the headline metric directly
        add(metric, 1.0, [])
    merged = {}  # same segment reached via several leaves: tree shares sum to 1, so contributions add
    for c in cands:
        m = merged.setdefault(frozenset(c["filters"].items()), {**c, "contribution": 0.0, "leaves": []})
        m["contribution"] = min(m["contribution"] + c["contribution"], 1.0)
        m["leaves"].append(c["leaf"])
    return sorted(merged.values(), key=lambda c: -c["contribution"])
