import json
from datetime import UTC, datetime
from pathlib import Path

from sentinel.models import Evidence


class EvidenceStore:
    """Append-only log of tool results; the LLM refers to them only by evidence_id."""

    def __init__(self):
        self.items: dict[str, Evidence] = {}

    def add(self, tool: str, params: dict, query: str | None, result: dict) -> Evidence:
        ev = Evidence(evidence_id=f"E{len(self.items) + 1:03d}", tool=tool, params=params, query=query,
                      result=result, computed_at=datetime.now(UTC))
        self.items[ev.evidence_id] = ev
        return ev

    def get(self, evidence_id: str) -> Evidence:
        return self.items[evidence_id]

    def save(self, path: Path) -> None:
        Path(path).write_text(json.dumps([e.model_dump(mode="json") for e in self.items.values()],
                                         indent=1, default=str))
