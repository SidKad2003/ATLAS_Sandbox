"""llm-gateway: the only path from agents to a model.

Emits one gen_ai.* span per call, enforces a per-run token budget, a per-run call cap and a
per-agent rate limit. Fails closed: if the budget store is unavailable, calls are refused.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from opentelemetry import trace
from pydantic import BaseModel

from svc_sim_common.config import env_int
from svc_sim_common.memory import valkey
from svc_sim_common.telemetry import (
    GEN_AI_AGENT_ID,
    GEN_AI_AGENT_NAME,
    GEN_AI_CONVERSATION_ID,
    GEN_AI_FINISH_REASONS,
    GEN_AI_INPUT_TOKENS,
    GEN_AI_OPERATION,
    GEN_AI_OUTPUT_TOKENS,
    GEN_AI_PROVIDER,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_RESPONSE_MODEL,
    instrument_asgi,
    meter,
    run_context,
    setup_telemetry,
)

from .providers import build_provider

log = logging.getLogger("svc_sim.gateway")
tracer = setup_telemetry("svc-sim-llm-gateway")

PROVIDER_NAME = os.environ.get("LLM_PROVIDER", "mock")
provider = build_provider(PROVIDER_NAME)
TOKEN_BUDGET = env_int("LLM_RUN_TOKEN_BUDGET", 400_000)
CALL_CAP = env_int("LLM_RUN_CALL_CAP", 5_000)
RATE_PER_MIN = env_int("LLM_RATE_PER_AGENT_PER_MIN", 240)
EMBED_DIM = env_int("EMBED_DIM", 64)


class Agent(BaseModel):
    id: str
    type: str
    name: str | None = None


# Per-agent LLM metrics (Prometheus: svc_sim_llm_tokens_total, gen_ai_client_token_usage_*, gen_ai_client_operation_duration_seconds_*)
_meter = meter("svc_sim.llm_gateway")
TOKENS = _meter.create_counter("svc_sim.llm.tokens", description="LLM tokens by agent, model and token type")
TOKEN_USAGE = _meter.create_histogram("gen_ai.client.token.usage", unit="{token}", description="GenAI token usage per call")
DURATION = _meter.create_histogram("gen_ai.client.operation.duration", unit="s", description="GenAI call duration")


def _metric_attrs(req: "ChatRequest", model: str) -> dict[str, str]:
    return {"gen_ai.agent.name": req.agent.name or req.agent.id, "svc_sim.agent.id": req.agent.id,
            "svc_sim.agent.type": req.agent.type, "gen_ai.operation.name": "chat", "gen_ai.provider.name": provider.name,
            "gen_ai.request.model": model, "svc_sim.llm.purpose": req.purpose, "atlas.run_id": req.run_id}


class ChatRequest(BaseModel):
    tier: str = "small"
    agent: Agent
    run_id: str
    conversation_id: str | None = None
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = []
    purpose: str = "act"
    state: dict[str, Any] = {}
    seed: str = ""
    max_tokens: int = 512


class EmbedRequest(BaseModel):
    agent: Agent
    run_id: str
    input: list[str]


async def _admit(run_id: str, agent_id: str, est_tokens: int) -> int:
    """Check caps before the call; returns the per-agent call index used to seed the mock."""
    try:
        r = await valkey()
        calls = await r.incr(f"svc-sim:llm:{run_id}:calls")
        used = int(await r.get(f"svc-sim:llm:{run_id}:tokens") or 0)
        minute = int(time.time() // 60)
        rate_key = f"svc-sim:llm:rate:{agent_id}:{minute}"
        rate = await r.incr(rate_key)
        await r.expire(rate_key, 120)
        n = await r.incr(f"svc-sim:llm:{run_id}:n:{agent_id}")
        await r.expire(f"svc-sim:llm:{run_id}:calls", 7 * 86400)
    except Exception as exc:
        log.error("budget store unavailable: %s", exc)
        raise HTTPException(status_code=429, detail="budget store unavailable; failing closed") from exc
    if calls > CALL_CAP:
        raise HTTPException(status_code=402, detail=f"run call cap {CALL_CAP} reached")
    if used + est_tokens > TOKEN_BUDGET:
        raise HTTPException(status_code=402, detail=f"run token budget {TOKEN_BUDGET} exhausted")
    if rate > RATE_PER_MIN:
        raise HTTPException(status_code=429, detail=f"rate limit {RATE_PER_MIN}/min for {agent_id}")
    return n


async def _charge(run_id: str, tokens: int) -> None:
    r = await valkey()
    await r.incrby(f"svc-sim:llm:{run_id}:tokens", tokens)
    await r.expire(f"svc-sim:llm:{run_id}:tokens", 7 * 86400)


api = FastAPI(title="svc-sim llm-gateway")


@api.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "provider": PROVIDER_NAME}


@api.get("/v1/usage/{run_id}")
async def usage(run_id: str) -> dict[str, Any]:
    r = await valkey()
    return {
        "run_id": run_id,
        "calls": int(await r.get(f"svc-sim:llm:{run_id}:calls") or 0),
        "tokens": int(await r.get(f"svc-sim:llm:{run_id}:tokens") or 0),
        "token_budget": TOKEN_BUDGET,
        "call_cap": CALL_CAP,
    }


@api.post("/v1/chat")
async def chat(req: ChatRequest) -> dict[str, Any]:
    body = req.model_dump()
    est = max(1, len(str(req.messages)) // 4) + req.max_tokens
    with run_context(req.run_id):
        n = await _admit(req.run_id, req.agent.id, est)
        requested_model = f"{PROVIDER_NAME}:{req.tier}"
        with tracer.start_as_current_span(f"chat {requested_model}", kind=trace.SpanKind.CLIENT) as span:
            span.set_attribute(GEN_AI_OPERATION, "chat")
            span.set_attribute(GEN_AI_PROVIDER, provider.name)
            span.set_attribute(GEN_AI_REQUEST_MODEL, requested_model)
            span.set_attribute(GEN_AI_AGENT_NAME, req.agent.name or req.agent.id)
            span.set_attribute(GEN_AI_AGENT_ID, req.agent.id)
            span.set_attribute("svc_sim.agent.type", req.agent.type)
            span.set_attribute("svc_sim.llm.tier", req.tier)
            span.set_attribute("svc_sim.llm.purpose", req.purpose)
            if req.conversation_id:
                span.set_attribute(GEN_AI_CONVERSATION_ID, req.conversation_id)
            t0 = time.perf_counter()
            try:
                result = await provider.chat(body, n)
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(trace.Status(trace.StatusCode.ERROR, type(exc).__name__))
                raise HTTPException(status_code=502, detail=f"provider error: {type(exc).__name__}") from exc
            span.set_attribute(GEN_AI_RESPONSE_MODEL, result.model)
            span.set_attribute(GEN_AI_INPUT_TOKENS, result.input_tokens)
            span.set_attribute(GEN_AI_OUTPUT_TOKENS, result.output_tokens)
            span.set_attribute(GEN_AI_FINISH_REASONS, [result.finish_reason])
            span.set_attribute("svc_sim.llm.tool_calls", len(result.tool_calls))
        attrs = _metric_attrs(req, result.model)
        DURATION.record(time.perf_counter() - t0, attrs)
        for token_type, value in (("input", result.input_tokens), ("output", result.output_tokens)):
            TOKENS.add(value, {**attrs, "gen_ai.token.type": token_type})
            TOKEN_USAGE.record(value, {**attrs, "gen_ai.token.type": token_type})
        await _charge(req.run_id, result.input_tokens + result.output_tokens)
    return {
        "content": result.content,
        "tool_calls": result.tool_calls,
        "model": result.model,
        "provider": result.provider,
        "usage": {"input_tokens": result.input_tokens, "output_tokens": result.output_tokens},
    }


@api.post("/v1/embeddings")
async def embeddings(req: EmbedRequest) -> dict[str, Any]:
    with run_context(req.run_id):
        await _admit(req.run_id, req.agent.id, sum(len(t) // 4 for t in req.input))
        with tracer.start_as_current_span(f"embeddings {PROVIDER_NAME}", kind=trace.SpanKind.CLIENT) as span:
            span.set_attribute(GEN_AI_OPERATION, "embeddings")
            span.set_attribute(GEN_AI_PROVIDER, provider.name)
            span.set_attribute(GEN_AI_REQUEST_MODEL, f"svc-sim-hash-embed-{EMBED_DIM}")
            span.set_attribute(GEN_AI_AGENT_ID, req.agent.id)
            tokens = sum(max(1, len(t) // 4) for t in req.input)
            span.set_attribute(GEN_AI_INPUT_TOKENS, tokens)
            vectors = await provider.embed(req.input, EMBED_DIM)
        await _charge(req.run_id, tokens)
    return {"data": vectors, "model": f"svc-sim-hash-embed-{EMBED_DIM}"}


app = instrument_asgi(api)
