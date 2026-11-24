"""sim-viz-bridge: consumes agent.events, stores them for replay, serves REST and a live WebSocket.

The simulation never depends on this service. It degrades gracefully: /healthz stays 200 when
Kafka or agent-memory are unavailable, and the consumer retries in the background.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect

from svc_sim_common.memory import db
from svc_sim_common.telemetry import instrument_asgi, setup_telemetry

from .mapping import PLACES, map_event, map_trace

tracer = setup_telemetry("svc-sim-sim-viz-bridge")
log = logging.getLogger("svc_sim.viz_bridge")

TOPIC = "agent.events"
GROUP_ID = "svc-sim-viz-bridge"
JAEGER_URL = os.environ.get("JAEGER_URL", "http://jaeger:16686/jaeger/ui").rstrip("/")
TRACE_DELAY_S = float(os.environ.get("VIZ_TRACE_DELAY_SECONDS", "3"))

state: dict[str, Any] = {"kafka": "starting", "db": "starting", "consumed": 0}


class Hub:
    """Live WebSocket clients, each with an optional run filter."""

    def __init__(self) -> None:
        self.clients: dict[WebSocket, str | None] = {}

    async def broadcast(self, action: dict[str, Any]) -> None:
        dead = []
        for ws, run_filter in list(self.clients.items()):
            if run_filter and action.get("run_id") != run_filter:
                continue
            try:
                await ws.send_text(json.dumps(action, default=str))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.pop(ws, None)


hub = Hub()


async def _pool():
    try:
        pool = await db()
        state["db"] = "ok"
        return pool
    except Exception as exc:
        state["db"] = f"unavailable: {type(exc).__name__}"
        return None


async def store_row(event_id: str, run_id: str, sim_tick: int, seq: int, agent_id: str, kind: str, body: dict[str, Any]) -> None:
    pool = await _pool()
    if pool is None:
        return
    try:
        await pool.execute(
            """INSERT INTO viz_events (event_id, run_id, sim_tick, seq, agent_id, kind, body)
               VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb) ON CONFLICT (event_id) DO NOTHING""",
            event_id, run_id, sim_tick, seq, agent_id, kind, json.dumps(body, default=str),
        )
    except Exception as exc:
        log.warning("store failed: %s", exc)


async def derive_spans(event: dict[str, Any]) -> None:
    trace_id = (event.get("trace") or {}).get("trace_id")
    if not trace_id:
        return
    await asyncio.sleep(TRACE_DELAY_S)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{JAEGER_URL}/api/traces/{trace_id}")
            if r.status_code != 200:
                return
            actions = map_trace(event, r.json())
    except Exception as exc:
        log.info("jaeger lookup skipped: %s", exc)
        return
    for action in actions:
        await store_row(action["event_id"], action["run_id"] or "", action["sim_tick"], action["seq"], "shop-services", "span", action)
        await hub.broadcast(action)


async def handle_event(event: dict[str, Any]) -> None:
    if event.get("schema") != "agent-event.v1" or not event.get("event_id"):
        return
    agent = event.get("agent") or {}
    await store_row(event["event_id"], event.get("run_id", ""), int(event.get("sim_tick", 0)), int(event.get("seq", 0)),
                    agent.get("id", "unknown"), event.get("kind", ""), event)
    await hub.broadcast(map_event(event))
    if event.get("kind") == "order.placed":
        asyncio.create_task(derive_spans(event))


async def consume_forever() -> None:
    from aiokafka import AIOKafkaConsumer

    while True:
        consumer = AIOKafkaConsumer(
            TOPIC,
            bootstrap_servers=os.environ.get("KAFKA_ADDR", "kafka:9092"),
            group_id=GROUP_ID,
            auto_offset_reset="earliest",
            enable_auto_commit=True,
            value_deserializer=lambda b: json.loads(b.decode("utf-8")),
        )
        try:
            await consumer.start()
            state["kafka"] = "ok"
            async for msg in consumer:
                state["consumed"] += 1
                try:
                    await handle_event(msg.value)
                except Exception as exc:
                    log.warning("event handling failed: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state["kafka"] = f"unavailable: {type(exc).__name__}"
            log.warning("kafka consumer error, retrying: %s", exc)
        finally:
            with contextlib.suppress(Exception):
                await consumer.stop()
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(consume_forever())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task


api = FastAPI(title="svc-sim sim-viz-bridge", lifespan=lifespan)


@api.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", **state}


@api.get("/api/places")
async def places() -> dict[str, Any]:
    return {"places": PLACES}


@api.get("/api/runs")
async def runs() -> dict[str, Any]:
    pool = await _pool()
    if pool is None:
        return {"runs": [], "error": "event store unavailable"}
    rows = await pool.fetch(
        """SELECT v.run_id, min(v.sim_tick) AS first_tick, max(v.sim_tick) AS last_tick, count(*) AS events,
                  min(v.received_at) AS first_seen, max(v.received_at) AS last_seen,
                  r.seed, r.status, r.config->>'label' AS label, r.summary
           FROM viz_events v LEFT JOIN runs r ON r.run_id = v.run_id
           WHERE v.run_id <> '' GROUP BY v.run_id, r.seed, r.status, r.config, r.summary
           ORDER BY min(v.received_at) DESC LIMIT 100"""
    )
    out = []
    for r in rows:
        d = dict(r)
        d["first_seen"], d["last_seen"] = r["first_seen"].isoformat(), r["last_seen"].isoformat()
        d["summary"] = json.loads(r["summary"]) if isinstance(r["summary"], str) else r["summary"]
        out.append(d)
    return {"runs": out}


async def _actions(run_id: str, from_tick: int | None = None, to_tick: int | None = None) -> list[dict[str, Any]]:
    pool = await _pool()
    if pool is None:
        return []
    rows = await pool.fetch(
        """SELECT kind, body FROM viz_events
           WHERE run_id=$1 AND ($2::int IS NULL OR sim_tick >= $2) AND ($3::int IS NULL OR sim_tick <= $3)
           ORDER BY sim_tick, (kind = 'span'), received_at, seq""",
        run_id, from_tick, to_tick,
    )
    actions = []
    for r in rows:
        body = r["body"] if isinstance(r["body"], dict) else json.loads(r["body"])
        actions.append(body if r["kind"] == "span" else map_event(body))
    return actions


@api.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, from_tick: int | None = Query(None), to_tick: int | None = Query(None)) -> dict[str, Any]:
    pool = await _pool()
    if pool is None:
        return {"run_id": run_id, "actions": [], "error": "event store unavailable"}
    return {"run_id": run_id, "actions": await _actions(run_id, from_tick, to_tick)}


async def _current_run() -> dict[str, Any] | None:
    pool = await _pool()
    if pool is None:
        return None
    row = await pool.fetchrow(
        "SELECT run_id, status, seed, config->>'label' AS label FROM runs ORDER BY created_at DESC LIMIT 1")
    return dict(row) if row else None


@api.websocket("/ws/live")
async def live(ws: WebSocket) -> None:
    await ws.accept()
    hub.clients[ws] = None
    # Join in progress: send what has happened so far in the running run, or say we are idle.
    try:
        current = await _current_run()
        if current and current["status"] == "running":
            await ws.send_text(json.dumps({"type": "backlog", "run": current, "actions": await _actions(current["run_id"])}, default=str))
        else:
            await ws.send_text(json.dumps({"type": "idle", "last_run": current}, default=str))
    except Exception as exc:
        log.warning("live backlog failed: %s", exc)
    try:
        while True:
            msg = await ws.receive_text()
            try:
                hub.clients[ws] = (json.loads(msg) or {}).get("run_id") or None
            except (json.JSONDecodeError, AttributeError):
                continue
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.pop(ws, None)


app = instrument_asgi(api)
