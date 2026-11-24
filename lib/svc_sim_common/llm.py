"""Client for llm-gateway. Agents never call a model provider directly."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import env
from .telemetry import current_run_id


class BudgetExceeded(RuntimeError):
    """The gateway refused the call (budget, rate limit or hard cap). Agents must stop, not retry."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatResult:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    provider: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


class LLMClient:
    def __init__(self, agent_id: str, agent_type: str, tier: str) -> None:
        self.agent_id = agent_id
        self.agent_name = agent_id
        self.agent_type = agent_type
        self.tier = tier
        self.base = env("LLM_GATEWAY_URL", "http://llm-gateway:8000")
        self._http = httpx.AsyncClient(base_url=self.base, timeout=120)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        purpose: str = "act",
        state: dict[str, Any] | None = None,
        seed: str = "",
        conversation_id: str | None = None,
        max_tokens: int = 512,
    ) -> ChatResult:
        """purpose and state are hints used only by the mock provider's scripted policies."""
        body = {
            "tier": self.tier,
            "agent": {"id": self.agent_id, "type": self.agent_type, "name": self.agent_name},
            "run_id": current_run_id() or "no-run",
            "conversation_id": conversation_id,
            "messages": messages,
            "tools": tools or [],
            "purpose": purpose,
            "state": state or {},
            "seed": seed,
            "max_tokens": max_tokens,
        }
        r = await self._http.post("/v1/chat", json=body)
        if r.status_code in (402, 429):
            raise BudgetExceeded(r.json().get("detail", "refused"))
        r.raise_for_status()
        d = r.json()
        return ChatResult(
            content=d.get("content", ""),
            tool_calls=[ToolCall(**tc) for tc in d.get("tool_calls", [])],
            model=d.get("model", ""),
            provider=d.get("provider", ""),
            input_tokens=d.get("usage", {}).get("input_tokens", 0),
            output_tokens=d.get("usage", {}).get("output_tokens", 0),
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        r = await self._http.post(
            "/v1/embeddings",
            json={"agent": {"id": self.agent_id, "type": self.agent_type, "name": self.agent_name}, "run_id": current_run_id() or "no-run", "input": texts},
        )
        if r.status_code in (402, 429):
            raise BudgetExceeded(r.json().get("detail", "refused"))
        r.raise_for_status()
        return r.json()["data"]
