"""Agent memory: working (Valkey), episodic and semantic (Postgres + pgvector), procedural (policy-store).

Every store is scoped to one agent id. Agents never read another agent's memory.
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any

import asyncpg
import redis.asyncio as aioredis
from pgvector.asyncpg import register_vector

from .config import agent_memory_dsn, working_memory_url

_pool: asyncpg.Pool | None = None
_redis: aioredis.Redis | None = None


async def _init_conn(conn: asyncpg.Connection) -> None:
    await register_vector(conn)


async def db() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(agent_memory_dsn(), min_size=1, max_size=4, init=_init_conn)
    return _pool


async def valkey() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(working_memory_url(), decode_responses=True)
    return _redis


class WorkingMemory:
    """Short-lived per-agent state in Valkey, namespaced by run."""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id

    def _key(self, run_id: str) -> str:
        return f"svc-sim:wm:{run_id}:{self.agent_id}"

    async def get(self, run_id: str) -> dict[str, Any]:
        r = await valkey()
        raw = await r.get(self._key(run_id))
        return json.loads(raw) if raw else {}

    async def put(self, run_id: str, state: dict[str, Any]) -> None:
        r = await valkey()
        await r.set(self._key(run_id), json.dumps(state, default=str), ex=86400)

    async def clear(self, run_id: str) -> None:
        r = await valkey()
        await r.delete(self._key(run_id))


def _parse_ts(sim_time: str) -> datetime:
    return datetime.fromisoformat(sim_time)


class EpisodicMemory:
    """Episodes with recency, importance and relevance scoring."""

    def __init__(self, agent_id: str, decay_per_hour: float = 0.995) -> None:
        self.agent_id = agent_id
        self.decay = decay_per_hour

    async def add(self, run_id: str, sim_time: str, content: str, importance: int, embedding: list[float], kind: str = "observation") -> int:
        pool = await db()
        return await pool.fetchval(
            """INSERT INTO episodic_memory (agent_id, run_id, sim_time, kind, content, importance, embedding)
               VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id""",
            self.agent_id, run_id, _parse_ts(sim_time), kind, content, importance, embedding,
        )

    async def retrieve(self, run_id: str, now_sim: str, query_embedding: list[float], k: int = 5) -> list[dict[str, Any]]:
        pool = await db()
        rows = await pool.fetch(
            """SELECT id, sim_time, kind, content, importance, 1 - (embedding <=> $3) AS relevance
               FROM episodic_memory WHERE agent_id=$1 AND run_id=$2
               ORDER BY sim_time DESC, id DESC LIMIT 200""",
            self.agent_id, run_id, query_embedding,
        )
        now = _parse_ts(now_sim)
        scored = []
        for r in rows:
            hours = max(0.0, (now - r["sim_time"]).total_seconds() / 3600)
            recency = math.pow(self.decay, hours * 60)
            score = recency + r["importance"] / 10 + float(r["relevance"] or 0)
            scored.append((score, r["id"], dict(r)))
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [dict(item, score=round(s, 4)) for s, _, item in scored[:k]]

    async def since(self, run_id: str, after_id: int = 0) -> list[dict[str, Any]]:
        pool = await db()
        rows = await pool.fetch(
            "SELECT id, sim_time, kind, content, importance FROM episodic_memory WHERE agent_id=$1 AND run_id=$2 AND id>$3 ORDER BY id",
            self.agent_id, run_id, after_id,
        )
        return [dict(r) for r in rows]


class SemanticMemory:
    """Beliefs distilled by reflection, or catalogue facts for RAG."""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id

    async def add(self, run_id: str, sim_time: str, subject: str, belief: str, confidence: float, embedding: list[float]) -> None:
        pool = await db()
        await pool.execute(
            """INSERT INTO semantic_memory (agent_id, run_id, sim_time, subject, belief, confidence, embedding)
               VALUES ($1,$2,$3,$4,$5,$6,$7)""",
            self.agent_id, run_id, _parse_ts(sim_time), subject, belief, confidence, embedding,
        )

    async def search(self, run_id: str, query_embedding: list[float], k: int = 5) -> list[dict[str, Any]]:
        pool = await db()
        rows = await pool.fetch(
            """SELECT subject, belief, confidence, 1 - (embedding <=> $3) AS relevance FROM semantic_memory
               WHERE agent_id=$1 AND (run_id=$2 OR run_id='static') ORDER BY embedding <=> $3, subject LIMIT $4""",
            self.agent_id, run_id, query_embedding, k,
        )
        return [dict(r) for r in rows]

    async def count(self, run_id: str) -> int:
        pool = await db()
        return await pool.fetchval(
            "SELECT count(*) FROM semantic_memory WHERE agent_id=$1 AND (run_id=$2 OR run_id='static')", self.agent_id, run_id
        )
