"""Publisher for agent.events (schema agent-event.v1)."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from aiokafka import AIOKafkaProducer

from .config import TESTBED, TOPIC_EVENTS, kafka_bootstrap
from .telemetry import meter, sim_clock_from_baggage, trace_ids

log = logging.getLogger("svc_sim.events")
_AGENT_EVENTS = None


def _agent_events_counter():
    """Per-agent activity metric: one count per agent-event (Prometheus: svc_sim_agent_events_total)."""
    global _AGENT_EVENTS
    if _AGENT_EVENTS is None:
        _AGENT_EVENTS = meter().create_counter("svc_sim.agent.events", description="Agent steps by agent, kind and tool")
    return _AGENT_EVENTS

EVENT_NAMESPACE = uuid.UUID("6f1c2b1e-2f0a-4c1e-9a55-5c5f7e5a0001")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def json_dumps(obj: Any) -> bytes:
    return json.dumps(obj, default=str, separators=(",", ":")).encode("utf-8")


class KafkaBus:
    """Single shared producer per process."""

    _producer: AIOKafkaProducer | None = None
    _lock = asyncio.Lock()

    @classmethod
    async def producer(cls) -> AIOKafkaProducer:
        async with cls._lock:
            if cls._producer is None:
                p = AIOKafkaProducer(bootstrap_servers=kafka_bootstrap(), value_serializer=json_dumps, linger_ms=5)
                for attempt in range(30):
                    try:
                        await p.start()
                        break
                    except Exception as exc:  # broker may still be starting
                        log.warning("kafka not ready (%s), retry %s", exc, attempt)
                        await asyncio.sleep(2)
                else:
                    raise RuntimeError("kafka unavailable")
                cls._producer = p
            return cls._producer

    @classmethod
    async def send(cls, topic: str, value: dict[str, Any], key: str | None = None) -> None:
        p = await cls.producer()
        await p.send_and_wait(topic, value, key=key.encode() if key else None)

    @classmethod
    async def stop(cls) -> None:
        if cls._producer is not None:
            await cls._producer.stop()
            cls._producer = None


class EventPublisher:
    """Builds and publishes agent-event.v1 records for one agent."""

    def __init__(self, agent_id: str, agent_type: str, name: str | None = None) -> None:
        self.agent = {"id": agent_id, "type": agent_type, "name": name or agent_id}
        self._seq: dict[str, int] = {}

    def reset(self, run_id: str) -> None:
        self._seq[run_id] = 0

    async def emit(
        self,
        run_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        sim_tick: int = 0,
        sim_time: str = "",
        location: str | None = None,
    ) -> dict[str, Any]:
        if sim_tick == 0 and not sim_time:
            sim_tick, sim_time = sim_clock_from_baggage()
        seq = self._seq.get(run_id, 0)
        self._seq[run_id] = seq + 1
        event = {
            "schema": "agent-event.v1",
            "event_id": str(uuid.uuid5(EVENT_NAMESPACE, f"{run_id}/{self.agent['id']}/{seq}")),
            "run_id": run_id,
            "seq": seq,
            "sim_tick": sim_tick,
            "sim_time": sim_time,
            "wall_time": now_iso(),
            "agent": self.agent,
            "kind": kind,
            "location": location,
            "payload": payload or {},
            "trace": trace_ids(),
            "attributes": {"atlas.testbed": TESTBED, "atlas.run_id": run_id},
        }
        p = payload or {}
        ok = p.get("ok")
        _agent_events_counter().add(1, {
            "svc_sim.agent.id": self.agent["id"],
            "gen_ai.agent.name": self.agent.get("name") or self.agent["id"],
            "svc_sim.agent.type": self.agent["type"],
            "svc_sim.event.kind": kind,
            "gen_ai.tool.name": str(p.get("tool") or ""),
            "svc_sim.outcome": "" if ok is None else ("ok" if ok else "failed"),
            "atlas.run_id": run_id,
        })
        try:
            await KafkaBus.send(TOPIC_EVENTS, event, key=self.agent["id"])
        except Exception:
            log.exception("failed to publish event %s", kind)
        return event
