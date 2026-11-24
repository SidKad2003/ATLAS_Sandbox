"""CLI for the Conductor: start a run, show status, and check determinism.

    python -m agents.conductor_cli start --seed 42 --shoppers 5 --ticks 60
    python -m agents.conductor_cli status
    python -m agents.conductor_cli determinism --seed 42
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import defaultdict
from typing import Any

import httpx
from aiokafka import AIOKafkaConsumer, TopicPartition

from svc_sim_common.config import TOPIC_EVENTS, kafka_bootstrap

BASE = "http://localhost:8000"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)  # UUIDs (shop order ids, tracking ids)
HEX_RE = re.compile(r"\b[0-9a-f]{12,32}\b", re.I)  # trace, span and call ids
VOLATILE_KEYS = {"order_id", "shipping_tracking_id", "trace_id", "span_id", "approval_id", "incident_id", "case_id",
                 "conversation_id", "id", "run_id", "summary", "subject", "decided_by", "decided_at", "created_at"}
COSMETIC_TOOLS = {"get_recommendations", "get_ads"}
ASYNC_AGENT_TYPES = {"fraud", "mcp-server", "human", "approval-service"}


def _norm(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in sorted(v.items()) if k not in VOLATILE_KEYS}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    if isinstance(v, str):
        return HEX_RE.sub("<id>", UUID_RE.sub("<uuid>", v))
    return v


def fingerprint(events: list[dict[str, Any]]) -> dict[str, list[str]]:
    per_agent: dict[str, list[str]] = defaultdict(list)
    for e in sorted(events, key=lambda e: (e["agent"]["id"], e["seq"])):
        # outcomes of upstream-random calls (recommendations, ads) are observed but not decision inputs
        if e["kind"] == "tool.result" and e.get("payload", {}).get("tool") in COSMETIC_TOOLS:
            continue
        tick = None if e["agent"]["type"] in ASYNC_AGENT_TYPES else e["sim_tick"]
        per_agent[e["agent"]["id"]].append(json.dumps([tick, e["kind"], e.get("location"), _norm(e.get("payload", {}))], sort_keys=True))
    return per_agent


async def read_events(run_ids: set[str]) -> dict[str, list[dict[str, Any]]]:
    consumer = AIOKafkaConsumer(bootstrap_servers=kafka_bootstrap(), enable_auto_commit=False, group_id=None)
    await consumer.start()
    try:
        parts = [TopicPartition(TOPIC_EVENTS, p) for p in (consumer.partitions_for_topic(TOPIC_EVENTS) or {0})]
        consumer.assign(parts)
        await consumer.seek_to_beginning(*parts)
        end = await consumer.end_offsets(parts)
        out: dict[str, list[dict[str, Any]]] = {r: [] for r in run_ids}
        while True:
            batch = await consumer.getmany(timeout_ms=2000, max_records=2000)
            for _, msgs in batch.items():
                for m in msgs:
                    e = json.loads(m.value)
                    if e.get("run_id") in out:
                        out[e["run_id"]].append(e)
            positions = {p: await consumer.position(p) for p in parts}
            if all(positions[p] >= end[p] for p in parts):
                return out
    finally:
        await consumer.stop()


def start(args: argparse.Namespace, extra: dict[str, Any] | None = None) -> str:
    body = {"seed": str(args.seed), "shoppers": args.shoppers, "ticks": args.ticks,
            "wall_seconds_per_tick": args.wall_seconds, **(extra or {})}
    r = httpx.post(f"{BASE}/runs", json=body, timeout=60)
    if r.status_code != 200:
        print(f"could not start run: {r.status_code} {r.text}")
        sys.exit(1)
    run = r.json()
    print(f"run {run['run_id']} started (seed {args.seed}, {args.shoppers} shoppers, {args.ticks} ticks)")
    print("schedule:", json.dumps(run["schedule"], indent=2))
    return run["run_id"]


def wait(run_id: str) -> dict[str, Any]:
    import time

    while True:
        d = httpx.get(f"{BASE}/runs/{run_id}", timeout=30).json()
        if d["status"] != "running":
            return d
        time.sleep(5)


def cmd_start(args: argparse.Namespace) -> None:
    run_id = start(args)
    if args.no_wait:
        return
    d = wait(run_id)
    print(f"run {run_id} {d['status']}")
    print(json.dumps(d.get("summary"), indent=2, default=str))


def cmd_status(_: argparse.Namespace) -> None:
    for r in httpx.get(f"{BASE}/runs", timeout=30).json():
        s = r.get("summary") or {}
        print(f"{r['run_id']}  seed={r['seed']:<6} {r['status']:<9} orders={s.get('orders', '-')} chats={s.get('chats', '-')} "
              f"incidents={s.get('incidents', '-')} approvals={sum(a.get('n', 0) for a in s.get('approvals', []))}")


def cmd_determinism(args: argparse.Namespace) -> None:
    extra = {"sre_alert_rules": False}
    ids = []
    for i in (1, 2):
        print(f"--- run {i} of 2")
        rid = start(args, extra)
        d = wait(rid)
        if d["status"] != "finished":
            print(f"run {rid} {d['status']}")
            sys.exit(1)
        ids.append(rid)
    import time

    time.sleep(5)
    print(f"comparing {ids[0]} and {ids[1]}", flush=True)
    evs = asyncio.run(read_events(set(ids)))
    report(evs[ids[0]], evs[ids[1]])


def compare(args: argparse.Namespace) -> None:
    """Compare two finished runs without starting new ones."""
    evs = asyncio.run(read_events({args.run_a, args.run_b}))
    report(evs[args.run_a], evs[args.run_b])


def report(ev_a: list[dict[str, Any]], ev_b: list[dict[str, Any]]) -> None:
    a, b = fingerprint(ev_a), fingerprint(ev_b)
    mismatched = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    print(f"events: run1={len(ev_a)} run2={len(ev_b)}", flush=True)
    if not mismatched:
        print("DETERMINISTIC: identical event sequences for every agent (volatile ids and wall times excluded)", flush=True)
        return
    for k in mismatched:
        x, y = a.get(k, []), b.get(k, [])
        first = next((i for i in range(max(len(x), len(y))) if (x[i:i + 1] != y[i:i + 1])), None)
        print(f"MISMATCH {k}: {len(x)} vs {len(y)} events, first difference at #{first}")
        if first is not None:
            print("  run1:", x[first] if first < len(x) else "<none>")
            print("  run2:", y[first] if first < len(y) else "<none>")
    sys.stdout.flush()
    sys.exit(2)


def main() -> None:
    p = argparse.ArgumentParser(prog="conductor_cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("start", "determinism"):
        s = sub.add_parser(name)
        s.add_argument("--seed", default="42")
        s.add_argument("--shoppers", type=int, default=5)
        s.add_argument("--ticks", type=int, default=60)
        s.add_argument("--wall-seconds", type=float, default=2.0)
        if name == "start":
            s.add_argument("--no-wait", action="store_true")
    sub.add_parser("status")
    c = sub.add_parser("compare")
    c.add_argument("run_a")
    c.add_argument("run_b")
    args = p.parse_args()
    {"start": cmd_start, "status": cmd_status, "determinism": cmd_determinism, "compare": compare}[args.cmd](args)


if __name__ == "__main__":
    main()
