"""Base class for tick-driven agents.

The Conductor drives every agent in lockstep: POST /run/start, then POST /tick once per
sim-clock tick (agents are called in a fixed order), then POST /run/finish. Agents act only
inside a tick, which is what makes a seeded run with the mock provider reproducible.
"""
from __future__ import annotations

import hashlib
import logging
import random
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from opentelemetry import trace
from pydantic import BaseModel

from .events import EventPublisher
from .llm import BudgetExceeded, LLMClient
from .memory import EpisodicMemory, SemanticMemory, WorkingMemory
from .policy_store import PolicyStoreClient
from .telemetry import (
    GEN_AI_AGENT_ID,
    GEN_AI_AGENT_NAME,
    GEN_AI_CONVERSATION_ID,
    GEN_AI_OPERATION,
    instrument_asgi,
    run_context,
    setup_telemetry,
    sim_clock_context,
    agent_context,
)

log = logging.getLogger("svc_sim.agent")


class RunStart(BaseModel):
    run_id: str
    seed: str
    sim_start: str
    config: dict[str, Any] = {}


class Tick(BaseModel):
    run_id: str
    tick: int
    sim_time: str
    seed: str
    directives: list[dict[str, Any]] = []


@dataclass
class RunState:
    run_id: str
    seed: str
    config: dict[str, Any]
    tick: int = 0
    sim_time: str = ""
    stopped: bool = False
    directives: list[dict[str, Any]] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)


def seeded_rng(*parts: Any) -> random.Random:
    digest = hashlib.sha256("/".join(str(p) for p in parts).encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


class BaseAgent:
    agent_type = "agent"
    tier = "mid"

    def __init__(self, agent_id: str, name: str | None = None) -> None:
        self.agent_id = agent_id
        self.name = name or agent_id
        self.tracer = setup_telemetry(f"svc-sim-{self.agent_type}")
        self.events = EventPublisher(agent_id, self.agent_type, self.name)
        self.llm = LLMClient(agent_id, self.agent_type, self.tier)
        self.llm.agent_name = self.name
        self.working = WorkingMemory(agent_id)
        self.episodic = EpisodicMemory(agent_id)
        self.semantic = SemanticMemory(agent_id)
        self.policies = PolicyStoreClient()
        self.runs: dict[str, RunState] = {}

    # ---- hooks for subclasses ----
    async def on_start(self, run: RunState) -> None:  # pragma: no cover - overridden
        return None

    async def on_tick(self, run: RunState) -> dict[str, Any]:  # pragma: no cover - overridden
        return {}

    async def on_finish(self, run: RunState) -> dict[str, Any]:
        return {}

    # ---- helpers ----
    async def emit(self, run: RunState, kind: str, payload: dict[str, Any] | None = None, location: str | None = None) -> dict[str, Any]:
        return await self.events.emit(run.run_id, kind, payload, sim_tick=run.tick, sim_time=run.sim_time, location=location)

    def rng(self, run: RunState, *extra: Any) -> random.Random:
        return seeded_rng(run.seed, self.agent_id, run.tick, *extra)

    async def remember(self, run: RunState, content: str, importance: int, kind: str = "observation") -> None:
        [vec] = await self.llm.embed([content])
        await self.episodic.add(run.run_id, run.sim_time, content, importance, vec, kind)
        await self.emit(run, "memory.write", {"memory": "episodic", "kind": kind, "importance": importance, "content": content[:200]})

    # ---- FastAPI wiring ----
    def app(self) -> Any:
        api = FastAPI(title=f"svc-sim {self.agent_type} {self.agent_id}")
        agent = self

        @api.get("/healthz")
        async def healthz() -> dict[str, str]:
            return {"status": "ok", "agent": agent.agent_id}

        @api.post("/run/start")
        async def start(req: RunStart) -> dict[str, Any]:
            run = RunState(run_id=req.run_id, seed=req.seed, config=req.config, sim_time=req.sim_start)
            agent.runs[req.run_id] = run
            agent.events.reset(req.run_id)
            with run_context(req.run_id):
                await agent.working.clear(req.run_id)
                await agent.on_start(run)
            return {"ok": True}

        @api.post("/tick")
        async def tick(req: Tick) -> dict[str, Any]:
            run = agent.runs.get(req.run_id)
            if run is None:
                return {"ok": False, "error": "unknown run"}
            run.tick, run.sim_time, run.directives = req.tick, req.sim_time, req.directives
            if run.stopped:
                return {"ok": True, "stopped": True}
            with run_context(req.run_id), sim_clock_context(req.tick, req.sim_time), agent_context(agent.events.agent["name"]), \
                    agent.tracer.start_as_current_span(f"invoke_agent {agent.events.agent['name']}") as span:
                span.set_attribute(GEN_AI_OPERATION, "invoke_agent")
                span.set_attribute(GEN_AI_AGENT_NAME, agent.events.agent["name"])
                span.set_attribute("svc_sim.agent.type", agent.agent_type)
                span.set_attribute(GEN_AI_AGENT_ID, agent.agent_id)
                span.set_attribute(GEN_AI_CONVERSATION_ID, f"{req.run_id}:{agent.agent_id}")
                span.set_attribute("svc_sim.sim_tick", req.tick)
                try:
                    result = await agent.on_tick(run)
                except BudgetExceeded as exc:
                    run.stopped = True
                    span.set_status(trace.Status(trace.StatusCode.ERROR, "budget"))
                    await agent.emit(run, "error", {"error": "llm budget exhausted, agent stopped", "detail": str(exc)})
                    return {"ok": False, "stopped": True}
                except Exception as exc:
                    log.exception("tick failed")
                    span.record_exception(exc)
                    span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)[:200]))
                    await agent.emit(run, "error", {"error": type(exc).__name__, "detail": str(exc)[:300]})
                    return {"ok": False, "error": str(exc)[:300]}
            return {"ok": True, **(result or {})}

        @api.post("/run/finish")
        async def finish(req: Tick) -> dict[str, Any]:
            run = agent.runs.get(req.run_id)
            if run is None:
                return {"ok": False}
            run.tick, run.sim_time = req.tick, req.sim_time
            with run_context(req.run_id):
                summary = await agent.on_finish(run)
            return {"ok": True, **(summary or {})}

        agent.extra_routes(api)
        return instrument_asgi(api)

    def extra_routes(self, api: FastAPI) -> None:
        return None
