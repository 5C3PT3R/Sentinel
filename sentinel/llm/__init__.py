"""Thin LLM client: deterministic response cache (D9) and token/cost log.

`create` returns a plain dict {content: [text|tool_use blocks], stop_reason, usage}. Anything with the same
`create` signature (e.g. a scripted fake in tests) can stand in for it.
"""
import hashlib
import json
from pathlib import Path

from sentinel.config import load_settings


class LLMClient:
    def __init__(self, cache_dir: Path = Path("runs/llm_cache"), model: str | None = None,
                 temperature: float | None = None, api=None):
        cfg = load_settings()
        self.model = model or cfg["model"]
        self.temperature = cfg["temperature"] if temperature is None else temperature
        self.pricing = cfg.get("pricing") or {}  # {input_per_mtok, output_per_mtok}; unset -> cost None
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._api = api
        self.calls = self.cache_hits = 0
        self.input_tokens = self.output_tokens = 0

    def key(self, system: str, messages: list, tools: list, tool_choice: dict | None) -> str:
        blob = json.dumps([self.model, system, messages, tools, self.temperature, tool_choice],
                          sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def create(self, system: str, messages: list, tools: list, tool_choice: dict | None = None,
               max_tokens: int = 4096) -> dict:
        self.calls += 1
        f = self.cache_dir / f"{self.key(system, messages, tools, tool_choice)}.json"
        if f.exists():
            self.cache_hits += 1
            return json.loads(f.read_text())
        if self._api is None:
            import anthropic  # reads ANTHROPIC_API_KEY from the environment

            self._api = anthropic.Anthropic()
        kw = {"tool_choice": tool_choice} if tool_choice else {}
        r = self._api.messages.create(model=self.model, system=system, messages=messages, tools=tools,
                                      temperature=self.temperature, max_tokens=max_tokens, **kw)
        out = {"content": [b.model_dump(exclude_none=True) for b in r.content],
               "stop_reason": r.stop_reason,
               "usage": {"input_tokens": r.usage.input_tokens, "output_tokens": r.usage.output_tokens}}
        f.write_text(json.dumps(out))
        self.input_tokens += out["usage"]["input_tokens"]
        self.output_tokens += out["usage"]["output_tokens"]
        return out

    @property
    def cost_usd(self) -> float | None:
        if not self.pricing:
            return None
        return (self.input_tokens * self.pricing["input_per_mtok"]
                + self.output_tokens * self.pricing["output_per_mtok"]) / 1e6
