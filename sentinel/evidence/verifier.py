"""Numeric check (invariant 1): every number in LLM prose must be traceable to cited evidence."""
import re

from sentinel.evidence import EvidenceStore

_STRIP = re.compile(r"\d{4}-\d{2}-\d{2}|\d+(?:\.\d+){2,}|E\d{3}|\b[A-Za-z_]+\d+[A-Za-z_\d]*\b")  # dates, versions, ids
_NUM = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")


def _leaves(x, out):
    if isinstance(x, bool):
        return
    if isinstance(x, (int, float)):
        out.append(float(x))
    elif isinstance(x, dict):
        for v in x.values():
            _leaves(v, out)
    elif isinstance(x, (list, tuple)):
        for v in x:
            _leaves(v, out)


def _matches(num: float, decimals: int, vals: list[float]) -> bool:
    tol = 0.5 * 10 ** -decimals  # the text was rounded to `decimals` places
    for v in vals:
        for cand in (v, v * 100, abs(v), abs(v) * 100, v / 1000, v / 1e6):
            if abs(abs(num) - abs(cand)) <= tol + 1e-9 or (cand and abs(abs(num) - abs(cand)) / abs(cand) < 0.005):
                return True
    return False


def unsupported_numbers(text: str, store: EvidenceStore, evidence_ids: list[str]) -> list[str]:
    """Numbers in `text` that match no value (rounded, x100 for percentages) in the cited evidence."""
    vals: list[float] = []
    for i in evidence_ids:
        ev = store.items.get(i)
        if ev:
            _leaves([ev.result, ev.params], vals)
    bad = []
    for m in _NUM.findall(_STRIP.sub(" ", text)):
        raw = m.replace(",", "")
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        if not _matches(float(raw), decimals, vals):
            bad.append(m)
    return bad
