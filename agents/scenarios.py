"""Scenario catalogue for replays and live demos.

    python -m agents.scenarios list
    python -m agents.scenarios replays          # run every scenario once, one after another
    python -m agents.scenarios run <name>       # run one scenario
    python -m agents.scenarios live             # a slower, longer run for watching in Live mode
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import httpx

BASE = "http://localhost:8000"

SCENARIOS: dict[str, dict[str, Any]] = {
    "catalogue-fault": {
        "label": "Catalogue fault, social engineer and injected review (seed 42)",
        "seed": "42", "ticks": 60,
    },
    "payment-outage": {
        "label": "Payment outage: half of all card charges fail",
        "seed": "7", "ticks": 80,
        "faults": [{"flag": "paymentFailure", "variant": "50%", "at_tick": 15, "clear_tick": 60}],
    },
    "basket-trouble": {
        "label": "Basket trouble: cart service failing 50%",
        "seed": "11", "ticks": 70,
        "faults": [{"flag": "cartFailure", "variant": "50%", "at_tick": 10, "clear_tick": 50}],
    },
    "gullible-assistant": {
        "label": "Gullible assistant: follows injected instructions, output guardrail blocks them",
        "seed": "21", "ticks": 60, "assistant_susceptibility": 1.0,
    },
    "queue-backlog": {
        "label": "Order queue backlog: Kafka problems slow fraud and accounting",
        "seed": "33", "ticks": 70,
        "faults": [{"flag": "kafkaQueueProblems", "variant": "on", "at_tick": 12, "clear_tick": 55}],
    },
    "busy-evening": {
        "label": "Busy evening: two faults, shoppers return for repeat visits",
        "seed": "99", "ticks": 140,
        "faults": [{"flag": "productCatalogFailure", "variant": "on", "at_tick": 20, "clear_tick": 70},
                   {"flag": "recommendationCacheFailure", "variant": "on", "at_tick": 80, "clear_tick": 125}],
    },
    "quiet-day": {
        "label": "Quiet day: ordinary shoppers only, no faults",
        "seed": "5", "ticks": 60, "adversarial": False, "faults": [],
    },
}

LIVE = {"label": "Live demo: two faults and both adversarial scenarios", "seed": "2026", "ticks": 240,
        "wall_seconds_per_tick": 4.0,
        "faults": [{"flag": "productCatalogFailure", "variant": "on", "at_tick": 30, "clear_tick": 110},
                   {"flag": "paymentFailure", "variant": "25%", "at_tick": 140, "clear_tick": 200}]}


def start(spec: dict[str, Any], wall: float) -> str:
    body = {"wall_seconds_per_tick": wall, "shoppers": 5, **spec}
    announced = False
    while True:
        r = httpx.post(f"{BASE}/runs", json=body, timeout=60)
        if r.status_code != 409:
            r.raise_for_status()
            return r.json()["run_id"]
        if not announced:
            print("a run is already in progress; waiting for it to finish", flush=True)
            announced = True
        time.sleep(10)


def wait(run_id: str) -> dict[str, Any]:
    while True:
        d = httpx.get(f"{BASE}/runs/{run_id}", timeout=30).json()
        if d["status"] != "running":
            return d
        time.sleep(5)


def run_one(name: str, spec: dict[str, Any], wall: float) -> None:
    run_id = start(spec, wall)
    print(f"{name}: run {run_id} started ({spec['label']})", flush=True)
    d = wait(run_id)
    print(f"{name}: {d['status']} {json.dumps(d.get('summary'), default=str)}", flush=True)


def main() -> None:
    p = argparse.ArgumentParser(prog="scenarios")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    r = sub.add_parser("replays")
    r.add_argument("--wall-seconds", type=float, default=1.0)
    one = sub.add_parser("run")
    one.add_argument("name", choices=sorted(SCENARIOS))
    one.add_argument("--wall-seconds", type=float, default=2.0)
    live = sub.add_parser("live")
    live.add_argument("--ticks", type=int, default=LIVE["ticks"])
    live.add_argument("--wall-seconds", type=float, default=LIVE["wall_seconds_per_tick"])
    args = p.parse_args()
    if args.cmd == "list":
        for k, v in SCENARIOS.items():
            print(f"{k:<20} {v['ticks']:>4} ticks  {v['label']}")
    elif args.cmd == "replays":
        for name, spec in SCENARIOS.items():
            run_one(name, spec, args.wall_seconds)
    elif args.cmd == "run":
        run_one(args.name, SCENARIOS[args.name], args.wall_seconds)
    elif args.cmd == "live":
        spec = {**LIVE, "ticks": args.ticks}
        spec.pop("wall_seconds_per_tick")
        run_id = start(spec, args.wall_seconds)
        print(f"live demo {run_id} started: {args.ticks} ticks at {args.wall_seconds}s per tick "
              f"(about {round(args.ticks * args.wall_seconds / 60)} minutes). Open sim-viz in Live mode.", flush=True)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
