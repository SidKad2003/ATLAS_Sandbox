"""Conductor (deterministic, no LLM).

Owns the simulated clock and drives every agent in lockstep: shoppers in slot order, then the
assistant, fraud and SRE agents. Builds the run schedule (personas, fault injections, adversarial
scenarios) from the seed and stores it in agent-memory, so the same seed gives the same run.
It is the only component that writes flagd: scripted faults, and flag changes a human has approved.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from opentelemetry import baggage as otel_baggage
from opentelemetry.context import Context as OtelContext
from pydantic import BaseModel

from svc_sim_common.agent_base import seeded_rng
from svc_sim_common.config import env
from svc_sim_common.events import EventPublisher
from svc_sim_common.memory import db
from svc_sim_common.policy_store import PolicyStoreClient
from svc_sim_common.telemetry import instrument_asgi, run_context, setup_telemetry

log = logging.getLogger("svc_sim.conductor")
tracer = setup_telemetry("svc-sim-conductor")
events = EventPublisher("conductor", "conductor", "Conductor")
policies = PolicyStoreClient()
FLAG_FILE = Path(os.environ.get("FLAGD_FILE", "/flags/demo.flagd.json"))
SHOPPER_URLS = [u for u in env("SHOPPER_URLS", "").split(",") if u]
ASSISTANT_URL, FRAUD_URL, SRE_URL = env("ASSISTANT_URL"), env("FRAUD_URL"), env("SRE_URL")
APPROVALS = httpx.AsyncClient(base_url=env("APPROVAL_SERVICE_URL", "http://approval-service:8000"), timeout=30)
HTTP = httpx.AsyncClient(timeout=180)

FLAG_LOCATION = {
    "paymentFailure": "checkout", "paymentUnreachable": "checkout", "cartFailure": "checkout",
    "productCatalogFailure": "aisle:telescopes", "productCatalogLockContention": "aisle:telescopes", "imageSlowLoad": "aisle:telescopes",
    "recommendationCacheFailure": "entrance", "adFailure": "entrance", "adHighCpu": "entrance", "adManualGc": "entrance",
    "kafkaQueueProblems": "warehouse", "intlShippingSlowdown": "shipping-dock", "emailMemoryLeak": "shipping-dock",
}
INJECTION_PRODUCT = {"id": "66VCHSJNUP", "name": "Starsense Explorer Refractor Telescope"}


class RunRequest(BaseModel):
    label: str = ""
    seed: str = "42"
    shoppers: int = 5
    ticks: int = 60
    tick_sim_seconds: int = 30
    wall_seconds_per_tick: float = 2.0
    sim_start: str = "2026-12-01T23:45:00"
    adversarial: bool = True
    faults: list[dict[str, Any]] | None = None
    assistant_susceptibility: float = 0.0
    sre_alert_rules: bool = True


class FlagStore:
    """Read and write the runtime flagd file. Targeted flags are switched through their rule branch."""

    @staticmethod
    def load() -> dict[str, Any]:
        return json.loads(FLAG_FILE.read_text(encoding="utf-8"))

    @staticmethod
    def set(flag: str, variant: str) -> None:
        doc = FlagStore.load()
        f = doc["flags"][flag]
        if variant not in f.get("variants", {}):
            raise ValueError(f"unknown variant {variant} for {flag}")
        rule = (f.get("targeting") or {}).get("if")
        if isinstance(rule, list) and len(rule) >= 2 and isinstance(rule[1], str):
            rule[1] = variant
        else:
            f["defaultVariant"] = variant
        # in-place write so flagd's file watch keeps working
        with FLAG_FILE.open("r+", encoding="utf-8") as fh:
            fh.seek(0)
            fh.write(json.dumps(doc, indent=2))
            fh.truncate()

    @staticmethod
    def off_variant(flag: str) -> str:
        variants = FlagStore.load()["flags"][flag].get("variants", {})
        if "off" in variants:
            return "off"
        return next((k for k, v in variants.items() if v in (False, 0)), "off")


def build_schedule(req: RunRequest, personas: list[dict[str, Any]]) -> dict[str, Any]:
    rng = seeded_rng(req.seed, "schedule")
    normal = sorted((p for p in personas if not p.get("adversarial")), key=lambda p: p["id"])
    rng.shuffle(normal)
    adversarial = sorted((p for p in personas if p.get("adversarial")), key=lambda p: p["id"])
    n = max(1, min(req.shoppers, len(SHOPPER_URLS)))
    chosen = (normal[: n - len(adversarial)] + adversarial) if req.adversarial and n > len(adversarial) else normal[:n]
    chosen = chosen[:n]
    faults = req.faults if req.faults is not None else [
        {"flag": "productCatalogFailure", "variant": "on", "at_tick": max(1, round(req.ticks * 0.33)), "clear_tick": round(req.ticks * 0.85)},
    ]
    scenarios: list[dict[str, Any]] = []
    if req.adversarial:
        for slot, p in enumerate(chosen):
            if p.get("adversarial") == "social_engineer":
                scenarios.append({"tick": 1, "slot": slot, "directive": {"type": "social_engineering"}})
            if p.get("adversarial") == "injection_prober":
                scenarios.append({"tick": max(2, round(req.ticks * 0.2)), "slot": slot,
                                  "directive": {"type": "review_injection", "product_id": INJECTION_PRODUCT["id"],
                                                "product_name": INJECTION_PRODUCT["name"]}})
    return {"personas": [p["id"] for p in chosen], "persona_docs": chosen, "faults": faults, "scenarios": scenarios}


class Conductor:
    def __init__(self) -> None:
        self.tasks: dict[str, asyncio.Task] = {}

    async def start(self, req: RunRequest) -> dict[str, Any]:
        if any(not t.done() for t in self.tasks.values()):
            raise HTTPException(409, "a run is already in progress")
        personas = await policies.get_json("personas/personas.json")
        schedule = build_schedule(req, personas)
        run_id = str(uuid.uuid4())
        pool = await db()
        await pool.execute(
            "INSERT INTO runs (run_id, seed, config, schedule, sim_start) VALUES ($1,$2,$3::jsonb,$4::jsonb,$5::timestamp)",
            run_id, req.seed, req.model_dump_json(), json.dumps({k: v for k, v in schedule.items() if k != "persona_docs"}),
            datetime.fromisoformat(req.sim_start),
        )
        self.tasks[run_id] = asyncio.create_task(self._run(run_id, req, schedule))
        return {"run_id": run_id, "schedule": {k: v for k, v in schedule.items() if k != "persona_docs"}}

    async def _post(self, url: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            r = await HTTP.post(f"{url}{path}", json=body)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            log.warning("%s%s failed: %s", url, path, exc)
            return {"ok": False, "error": str(exc)[:200]}

    async def _emit(self, run_id: str, kind: str, payload: dict[str, Any], tick: int, sim_time: str, location: str | None = None) -> None:
        await events.emit(run_id, kind, payload, sim_tick=tick, sim_time=sim_time, location=location)

    async def _run(self, run_id: str, req: RunRequest, schedule: dict[str, Any]) -> None:
        events.reset(run_id)
        start = datetime.fromisoformat(req.sim_start)
        shoppers = SHOPPER_URLS[: len(schedule["persona_docs"])]
        active_faults: dict[str, dict[str, Any]] = {}
        summary: dict[str, Any] = {}
        with run_context(run_id):
            try:
                await self._emit(run_id, "run.started", {"label": req.label, "seed": req.seed, "shoppers": len(shoppers), "ticks": req.ticks,
                                                         "personas": schedule["personas"], "faults": schedule["faults"],
                                                         "scenarios": schedule["scenarios"]}, 0, req.sim_start, "entrance")
                base = {"run_id": run_id, "seed": req.seed, "sim_start": req.sim_start}
                for i, url in enumerate(shoppers):
                    await self._post(url, "/run/start", {**base, "config": {"persona": schedule["persona_docs"][i]}})
                for url in SHOPPER_URLS[len(shoppers):]:
                    await self._post(url, "/run/start", {**base, "config": {"persona": None}})
                await self._post(ASSISTANT_URL, "/run/start", {**base, "config": {"assistant_susceptibility": req.assistant_susceptibility}})
                await self._post(FRAUD_URL, "/run/start", {**base, "config": {}})
                await self._post(SRE_URL, "/run/start", {**base, "config": {"sre_alert_rules": req.sre_alert_rules}})

                for tick in range(1, req.ticks + 1):
                    t0 = asyncio.get_running_loop().time()
                    sim_time = (start + timedelta(seconds=tick * req.tick_sim_seconds)).isoformat()
                    # each tick is its own root trace (not a child of the POST /runs request), carrying run id baggage
                    root = otel_baggage.set_baggage("atlas.run_id", run_id, context=OtelContext())
                    root = otel_baggage.set_baggage("svc_sim.sim_tick", str(tick), context=root)
                    root = otel_baggage.set_baggage("svc_sim.sim_time", sim_time, context=root)
                    with tracer.start_as_current_span(f"conductor.tick {tick}", context=root) as span:
                        span.set_attribute("svc_sim.sim_tick", tick)
                        await self._emit(run_id, "clock.tick", {"tick": tick}, tick, sim_time)
                        sre_directives = await self._faults(run_id, schedule, active_faults, tick, sim_time)
                        await self._apply_approved_flag_changes(run_id, active_faults, tick, sim_time)
                        tick_body = {"run_id": run_id, "tick": tick, "sim_time": sim_time, "seed": req.seed}
                        for slot, url in enumerate(shoppers):
                            directives = [s["directive"] for s in schedule["scenarios"] if s["tick"] == tick and s["slot"] == slot]
                            await self._post(url, "/tick", {**tick_body, "directives": directives})
                        await self._post(ASSISTANT_URL, "/tick", tick_body)
                        await self._post(FRAUD_URL, "/tick", tick_body)
                        await self._post(SRE_URL, "/tick", {**tick_body, "directives": sre_directives})
                    elapsed = asyncio.get_running_loop().time() - t0
                    await asyncio.sleep(max(0.0, req.wall_seconds_per_tick - elapsed))

                end_time = (start + timedelta(seconds=req.ticks * req.tick_sim_seconds)).isoformat()
                for flag in list(active_faults):
                    FlagStore.set(flag, FlagStore.off_variant(flag))
                    await self._emit(run_id, "fault.cleared", {"flag": flag, "by": "run end", "location": FLAG_LOCATION.get(flag, "ops-room")},
                                     req.ticks, end_time, FLAG_LOCATION.get(flag, "ops-room"))
                finish = {"run_id": run_id, "tick": req.ticks, "sim_time": end_time, "seed": req.seed}
                agents = {}
                for url in [*SHOPPER_URLS, ASSISTANT_URL, FRAUD_URL, SRE_URL]:
                    agents[url.split("//")[1].split(":")[0]] = await self._post(url, "/run/finish", finish)
                summary = await self._summary(run_id, agents)
                await self._emit(run_id, "run.finished", summary, req.ticks, end_time, "entrance")
                status = "finished"
            except Exception as exc:
                log.exception("run failed")
                summary, status = {"error": str(exc)[:300]}, "failed"
                for flag in list(active_faults):
                    try:
                        FlagStore.set(flag, FlagStore.off_variant(flag))
                    except Exception:
                        log.exception("could not clear %s", flag)
            pool = await db()
            await pool.execute("UPDATE runs SET status=$2, finished_at=now(), summary=$3::jsonb WHERE run_id=$1",
                               run_id, status, json.dumps(summary, default=str))

    async def _faults(self, run_id: str, schedule: dict[str, Any], active: dict[str, dict[str, Any]], tick: int, sim_time: str) -> list[dict[str, Any]]:
        sre: list[dict[str, Any]] = []
        for f in schedule["faults"]:
            loc = FLAG_LOCATION.get(f["flag"], "ops-room")
            if f["at_tick"] == tick:
                FlagStore.set(f["flag"], f["variant"])
                active[f["flag"]] = f
                await self._emit(run_id, "fault.injected", {"flag": f["flag"], "variant": f["variant"], "location": loc, "by": "schedule"},
                                 tick, sim_time, loc)
                sre.append({"type": "fault_alert", "flag": f["flag"], "variant": f["variant"], "source": "conductor"})
            if f.get("clear_tick") == tick and f["flag"] in active:
                FlagStore.set(f["flag"], FlagStore.off_variant(f["flag"]))
                del active[f["flag"]]
                await self._emit(run_id, "fault.cleared", {"flag": f["flag"], "location": loc, "by": "schedule"}, tick, sim_time, loc)
        return sre

    async def _apply_approved_flag_changes(self, run_id: str, active: dict[str, dict[str, Any]], tick: int, sim_time: str) -> None:
        r = await APPROVALS.get("/api/approvals", params={"run_id": run_id, "status": "approved", "kind": "flag_change"})
        if r.status_code != 200:
            return
        for a in sorted(r.json(), key=lambda x: x["id"]):
            flag, variant = a["request"].get("flag"), a["request"].get("variant")
            try:
                FlagStore.set(flag, variant)
            except (KeyError, ValueError) as exc:
                log.warning("approved change %s invalid: %s", a["id"], exc)
                continue
            await APPROVALS.post(f"/api/approvals/{a['id']}/applied")
            active.pop(flag, None)
            loc = FLAG_LOCATION.get(flag, "ops-room")
            await self._emit(run_id, "fault.cleared", {"flag": flag, "variant": variant, "location": loc, "by": a.get("decided_by"),
                                                       "approval_id": a["id"]}, tick, sim_time, loc)

    async def _summary(self, run_id: str, agents: dict[str, Any]) -> dict[str, Any]:
        pool = await db()
        approvals = await pool.fetch("SELECT kind, status, count(*) AS n FROM approvals WHERE run_id=$1 GROUP BY 1,2", run_id)
        incidents = await pool.fetchval("SELECT count(*) FROM incidents WHERE run_id=$1", run_id)
        cases = await pool.fetchval("SELECT count(*) FROM case_log WHERE run_id=$1", run_id)
        holds = await pool.fetchval("SELECT count(*) FROM order_holds WHERE run_id=$1", run_id)
        return {
            "orders": sum(int(v.get("orders", 0) or 0) for v in agents.values() if isinstance(v, dict)),
            "chats": sum(int(v.get("chats", 0) or 0) for v in agents.values() if isinstance(v, dict)),
            "approvals": [dict(a) for a in approvals], "incidents": incidents, "fraud_cases": cases, "order_holds": holds,
        }


conductor = Conductor()
api = FastAPI(title="svc-sim conductor")


@api.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@api.post("/runs")
async def start_run(req: RunRequest) -> dict[str, Any]:
    return await conductor.start(req)


@api.get("/runs")
async def list_runs(limit: int = 20) -> list[dict[str, Any]]:
    pool = await db()
    rows = await pool.fetch("SELECT run_id, seed, status, created_at, finished_at, summary FROM runs ORDER BY created_at DESC LIMIT $1", limit)
    return [{**dict(r), "summary": json.loads(r["summary"]) if r["summary"] else None} for r in rows]


@api.get("/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    pool = await db()
    r = await pool.fetchrow("SELECT * FROM runs WHERE run_id=$1", run_id)
    if not r:
        raise HTTPException(404, "not found")
    d = dict(r)
    for k in ("config", "schedule", "summary"):
        if isinstance(d.get(k), str):
            d[k] = json.loads(d[k])
    return d


app = instrument_asgi(api)
