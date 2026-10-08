from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel


class Evidence(BaseModel):
    evidence_id: str
    tool: str
    params: dict
    query: str | None = None
    result: dict
    computed_at: datetime


class DQIssue(BaseModel):
    check: str
    severity: Literal["warn", "block"]
    detail: str


class DQReport(BaseModel):
    status: Literal["PASS", "WARN", "BLOCK"]
    issues: list[DQIssue] = []


class Anomaly(BaseModel):
    metric: str
    date: date
    value: float
    baseline: float
    pct_change: float
    z_score: float
    severity: Literal["low", "medium", "high"]
    evidence_id: str


class Hypothesis(BaseModel):
    id: str
    statement: str
    segment_filters: dict[str, str]
    chain: list[str]  # ordered evidence_ids, KPI down to cause
    contribution_pct: float  # computed in code
    evidence_ids: list[str]


class CriticVerdict(BaseModel):
    hypothesis_id: str
    verdict: Literal["accept", "revise", "reject"]
    reasons: list[str]
    checks_run: list[str]
    confidence: float = 0.0  # computed in code


class RejectedHypothesis(BaseModel):
    hypothesis: Hypothesis
    reasons: list[str]


class Report(BaseModel):
    run_id: str
    date: date
    status: Literal["normal", "anomaly", "data_issue"]
    headline: str
    primary_cause: Hypothesis | None = None
    secondary_causes: list[Hypothesis] = []
    rejected: list[RejectedHypothesis] = []
    unexplained_pct: float = 100.0
    confidence: float = 0.0
    recommended_action: str = ""
    escalate: bool = False
    markdown: str = ""
