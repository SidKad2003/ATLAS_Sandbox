"""LLM providers behind llm-gateway: mock (default), bedrock (disabled), openai_compatible (disabled)."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from .embeddings import hash_embedding
from .mock_policies import run_policy


@dataclass
class ProviderResult:
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    provider: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str = "stop"


def estimate_tokens(obj: Any) -> int:
    text = obj if isinstance(obj, str) else json.dumps(obj, default=str)
    return max(1, len(text) // 4)


class MockProvider:
    name = "mock"

    async def chat(self, req: dict[str, Any], n: int) -> ProviderResult:
        tools = {t["name"] for t in req.get("tools", [])}
        content, calls = run_policy(
            req["agent"]["type"], req["agent"]["id"], req.get("purpose", "act"), req.get("state", {}),
            tools, req.get("seed", ""), n,
        )
        return ProviderResult(
            content=content,
            tool_calls=calls,
            model=f"svc-sim-mock-{req['tier']}-v1",
            provider=self.name,
            input_tokens=estimate_tokens(req.get("messages", [])) + estimate_tokens(req.get("tools", [])),
            output_tokens=estimate_tokens(content) + estimate_tokens(calls),
            finish_reason="tool_calls" if calls else "stop",
        )

    async def embed(self, texts: list[str], dim: int) -> list[list[float]]:
        return [hash_embedding(t, dim) for t in texts]


class BedrockProvider:
    """bedrock-runtime Converse through an application inference profile. Uses the instance role only.

    Disabled until the operator confirms the role and profiles (BEDROCK_ENABLED=true).
    Tier mapping comes from BEDROCK_PROFILE_ARN_SMALL / BEDROCK_PROFILE_ARN_MID.
    """

    name = "aws.bedrock"

    def __init__(self) -> None:
        if os.environ.get("BEDROCK_ENABLED", "false").lower() != "true":
            raise RuntimeError("bedrock provider is disabled (BEDROCK_ENABLED is not true)")
        import boto3

        self.client = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "eu-west-2"))
        self.profiles = {
            "small": os.environ["BEDROCK_PROFILE_ARN_SMALL"],
            "mid": os.environ["BEDROCK_PROFILE_ARN_MID"],
        }

    @staticmethod
    def _messages(req: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        system, msgs = [], []
        for m in req.get("messages", []):
            if m["role"] == "system":
                system.append({"text": m["content"]})
            elif m["role"] == "tool":
                msgs.append({"role": "user", "content": [{"toolResult": {"toolUseId": m["tool_call_id"], "content": [{"text": m["content"]}]}}]})
            elif m["role"] == "assistant" and m.get("tool_calls"):
                content = [{"text": m["content"]}] if m.get("content") else []
                content += [{"toolUse": {"toolUseId": tc["id"], "name": tc["name"], "input": tc["arguments"]}} for tc in m["tool_calls"]]
                msgs.append({"role": "assistant", "content": content})
            else:
                msgs.append({"role": m["role"], "content": [{"text": m["content"]}]})
        return system, msgs

    async def chat(self, req: dict[str, Any], n: int) -> ProviderResult:
        system, msgs = self._messages(req)
        kwargs: dict[str, Any] = {
            "modelId": self.profiles[req["tier"]],
            "messages": msgs,
            "inferenceConfig": {"maxTokens": int(req.get("max_tokens", 512)), "temperature": 0.2},
        }
        if system:
            kwargs["system"] = system
        if req.get("tools"):
            kwargs["toolConfig"] = {"tools": [{"toolSpec": {"name": t["name"], "description": t.get("description", "")[:500],
                                                            "inputSchema": {"json": t.get("input_schema", {"type": "object"})}}}
                                              for t in req["tools"]]}
        resp = await asyncio.to_thread(self.client.converse, **kwargs)
        content, calls = "", []
        for block in resp["output"]["message"]["content"]:
            if "text" in block:
                content += block["text"]
            if "toolUse" in block:
                tu = block["toolUse"]
                calls.append({"id": tu["toolUseId"], "name": tu["name"], "arguments": tu.get("input", {})})
        usage = resp.get("usage", {})
        return ProviderResult(content=content, tool_calls=calls, model=kwargs["modelId"], provider=self.name,
                              input_tokens=usage.get("inputTokens", 0), output_tokens=usage.get("outputTokens", 0),
                              finish_reason=resp.get("stopReason", "stop"))

    async def embed(self, texts: list[str], dim: int) -> list[list[float]]:
        # Embeddings stay local and deterministic even with Bedrock chat, so memory retrieval is comparable across providers.
        return [hash_embedding(t, dim) for t in texts]


class OpenAICompatibleProvider:
    """For a local model later (e.g. vLLM or llama.cpp server). Disabled unless OPENAI_COMPATIBLE_URL is set."""

    name = "openai_compatible"

    def __init__(self) -> None:
        url = os.environ.get("OPENAI_COMPATIBLE_URL")
        if not url:
            raise RuntimeError("openai_compatible provider needs OPENAI_COMPATIBLE_URL")
        self.http = httpx.AsyncClient(base_url=url, timeout=120)
        self.models = {"small": os.environ.get("OPENAI_COMPATIBLE_MODEL_SMALL", "small"),
                       "mid": os.environ.get("OPENAI_COMPATIBLE_MODEL_MID", "mid")}

    async def chat(self, req: dict[str, Any], n: int) -> ProviderResult:
        msgs = []
        for m in req.get("messages", []):
            if m["role"] == "assistant" and m.get("tool_calls"):
                msgs.append({"role": "assistant", "content": m.get("content", ""), "tool_calls": [
                    {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}}
                    for tc in m["tool_calls"]]})
            else:
                msgs.append({k: v for k, v in m.items() if k in ("role", "content", "tool_call_id")})
        body: dict[str, Any] = {"model": self.models[req["tier"]], "messages": msgs, "max_tokens": req.get("max_tokens", 512)}
        if req.get("tools"):
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                                               "parameters": t.get("input_schema", {"type": "object"})}} for t in req["tools"]]
        r = await self.http.post("/v1/chat/completions", json=body)
        r.raise_for_status()
        d = r.json()
        msg = d["choices"][0]["message"]
        calls = [{"id": tc["id"], "name": tc["function"]["name"], "arguments": json.loads(tc["function"]["arguments"] or "{}")}
                 for tc in msg.get("tool_calls") or []]
        u = d.get("usage", {})
        return ProviderResult(content=msg.get("content") or "", tool_calls=calls, model=body["model"], provider=self.name,
                              input_tokens=u.get("prompt_tokens", 0), output_tokens=u.get("completion_tokens", 0),
                              finish_reason=d["choices"][0].get("finish_reason", "stop"))

    async def embed(self, texts: list[str], dim: int) -> list[list[float]]:
        return [hash_embedding(t, dim) for t in texts]


def build_provider(name: str):
    if name == "mock":
        return MockProvider()
    if name == "bedrock":
        return BedrockProvider()
    if name == "openai_compatible":
        return OpenAICompatibleProvider()
    raise RuntimeError(f"unknown provider {name}")
