"""Fraud Review agent (mid tier).

Consumes the shop's orders topic (own consumer group) and agent.handoff. Work is buffered and handled
inside Conductor ticks in a stable order. Holds and releases above the threshold go to human approval
first; store-ops-mcp also refuses them without an approved approval id (fail closed).
Memory: working, episodic (case log), procedural (fraud policy).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

import httpx
from aiokafka import AIOKafkaConsumer

from svc_sim_common.agent_base import BaseAgent, RunState
from svc_sim_common.config import ORDERS_TOPIC, TOPIC_HANDOFF, env, env_float, kafka_bootstrap
from svc_sim_common.mcp_client import MCPTools, ToolError
from svc_sim_common.memory import db

from .tooling import assistant_tool_message, local_tool, summarise, tool_catalogue, tool_result_message

log = logging.getLogger("svc_sim.fraud")
FRAUD_TOOLS = {"get_order", "get_order_history", "get_payment_signals", "get_fraud_verdict", "hold_order", "release_order"}
ESCALATE = local_tool("escalate_to_human", "Ask a human to approve a hold or release, or to review a case.",
                      {"action": {"type": "string", "enum": ["hold_order", "release_order", "escalation"]},
                       "order_id": {"type": "string"}, "rationale": {"type": "string"}}, ["action", "order_id", "rationale"])
MAX_CASES_PER_TICK = 3
MAX_DEFERS = 8


class FraudReview(BaseAgent):
    agent_type = "fraud"
    tier = "mid"

    def __init__(self) -> None:
        super().__init__("fraud-review", "Fraud Review")
        self.store = MCPTools("store-ops-mcp", env("STORE_OPS_MCP_URL"))
        self.approvals = httpx.AsyncClient(base_url=env("APPROVAL_SERVICE_URL", "http://approval-service:8000"), timeout=30)
        self.threshold = env_float("FRAUD_APPROVAL_THRESHOLD_USD", 100.0)
        self.inbox: list[dict[str, Any]] = []
        self.active_run: str | None = None
        self._tools: list[dict[str, Any]] | None = None
        self._consumers_started = False

    async def tools(self) -> list[dict[str, Any]]:
        if self._tools is None:
            self._tools = await tool_catalogue(self.store, FRAUD_TOOLS) + [ESCALATE]
        return self._tools

    # ---------------- consumers ----------------
    async def _consume(self, topic: str, group: str, decode) -> None:
        while True:
            consumer = AIOKafkaConsumer(topic, bootstrap_servers=kafka_bootstrap(), group_id=group, auto_offset_reset="latest")
            try:
                await consumer.start()
                async for msg in consumer:
                    if self.active_run is None:
                        continue
                    item = decode(msg.value)
                    if item:
                        self.inbox.append(item)
            except Exception as exc:
                log.warning("consumer %s error: %s", topic, exc)
                await asyncio.sleep(3)
            finally:
                await consumer.stop()

    @staticmethod
    def _decode_order(value: bytes) -> dict[str, Any] | None:
        import demo_pb2

        try:
            o = demo_pb2.OrderResult()
            o.ParseFromString(value)
        except Exception:
            return None
        a = o.shipping_address
        return {"source": "orders", "order_id": o.order_id, "street_address": a.street_address, "zip_code": a.zip_code,
                "items": sorted((i.item.product_id, i.item.quantity) for i in o.items)}

    def _decode_handoff(self, value: bytes) -> dict[str, Any] | None:
        try:
            m = json.loads(value)
        except Exception:
            return None
        if m.get("run_id") != self.active_run:
            return None
        return {"source": "handoff", "order_id": m.get("order_id"), "case_id": m.get("case_id"), "reason": m.get("reason", ""),
                "severity": m.get("severity", "medium")}

    async def on_start(self, run: RunState) -> None:
        self.active_run = run.run_id
        self.inbox.clear()
        run.data.update(defers={}, reviewed=set(), applied=set(), policy=await self.policies.get_text("policies/fraud-policy.md"))
        if not self._consumers_started:
            asyncio.create_task(self._consume(ORDERS_TOPIC, "svc-sim-fraud-review", self._decode_order))
            asyncio.create_task(self._consume(TOPIC_HANDOFF, "svc-sim-fraud-review-handoff", self._decode_handoff))
            self._consumers_started = True

    async def on_finish(self, run: RunState) -> dict[str, Any]:
        self.active_run = None
        return {"cases": len(run.data.get("reviewed", set()))}

    # ---------------- tick ----------------
    async def on_tick(self, run: RunState) -> dict[str, Any]:
        await self._apply_decisions(run)
        for h in run.directives:
            if h.get("type") == "handoff":
                self.inbox.append(h)
        # stable order: handoffs first, then orders by address and items (order ids are random)
        pending, self.inbox = self.inbox, []
        pending.sort(key=lambda c: (c["source"] != "handoff", c.get("case_id") or "", c.get("street_address") or "", str(c.get("items"))))
        handled = 0
        for case in pending:
            if handled >= MAX_CASES_PER_TICK:
                self.inbox.append(case)
                continue
            key = f"{case['source']}:{case.get('order_id')}:{case.get('case_id')}"
            if key in run.data["reviewed"]:
                continue
            if case["source"] == "handoff":
                await self.emit(run, "handoff.received", {"case_id": case.get("case_id"), "order_id": case.get("order_id"),
                                                          "reason": case.get("reason")}, location="fraud-office")
            try:
                outcome = await self._triage(run, case)
            except Exception as exc:
                log.exception("case failed")
                await self.emit(run, "error", {"error": type(exc).__name__, "detail": str(exc)[:300], "order_id": case.get("order_id")},
                                location="fraud-office")
                outcome = "defer"
            handled += 1
            if outcome == "defer":
                n = run.data["defers"].get(key, 0) + 1
                run.data["defers"][key] = n
                if n <= MAX_DEFERS:
                    self.inbox.append(case)
                continue
            run.data["reviewed"].add(key)
        return {"handled": handled, "queued": len(self.inbox)}

    async def _apply_decisions(self, run: RunState) -> None:
        """Act on approvals a human has decided since the last tick."""
        r = await self.approvals.get("/api/approvals", params={"run_id": run.run_id})
        r.raise_for_status()
        for a in sorted(r.json(), key=lambda x: x["id"]):
            if a["requested_by"] != self.agent_id or a["id"] in run.data["applied"]:
                continue
            if a["status"] == "approved" and a["kind"] in ("hold_order", "release_order"):
                args = {"order_id": a["subject"], "reason": f"Approved by {a['decided_by']}: {a['rationale'][:120]}",
                        "requested_by": self.agent_id, "run_id": run.run_id, "approval_id": a["id"]}
                await self.emit(run, "tool.call", {"tool": a["kind"], "server": "store-ops-mcp", "args": {"order_id": a["subject"], "approval_id": a["id"]}},
                                location="fraud-office")
                try:
                    res = await self.store.call(a["kind"], args)
                    ok = not (isinstance(res, dict) and res.get("ok") is False)
                except ToolError as exc:
                    res, ok = {"error": str(exc)}, False
                await self.emit(run, "tool.result", {"tool": a["kind"], "ok": ok, "summary": summarise(res)}, location="fraud-office")
                run.data["applied"].add(a["id"])
            elif a["status"] == "rejected":
                await self.remember(run, f"Human rejected {a['kind']} for {a['subject']}: {a.get('decision_note') or ''}", importance=6, kind="case")
                run.data["applied"].add(a["id"])

    async def _triage(self, run: RunState, case: dict[str, Any]) -> str:
        state: dict[str, Any] = {"case": case, "done_tools": [], "results": {}, "threshold": self.threshold, "value_usd": 0.0}
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "You are the Fraud Review agent. Follow the fraud policy.\n" + run.data["policy"]},
            {"role": "user", "content": json.dumps({"case": case})},
        ]
        outcome = ""
        for step in range(7):
            res = await self.llm.chat(messages, tools=await self.tools(), purpose="triage", state=state,
                                      seed=f"{run.seed}/{case.get('case_id') or case.get('street_address')}/{step}")
            if not res.tool_calls:
                outcome = res.content
                break
            tc = res.tool_calls[0]
            messages.append(assistant_tool_message(res.tool_calls, res.content))
            result = await self._tool(run, tc.name, dict(tc.arguments), tc.id, state)
            state["done_tools"].append(tc.name)
            state["results"][tc.name] = result
            if tc.name == "get_order" and isinstance(result, dict):
                state["value_usd"] = float(result.get("items_total_usd") or 0)
            messages.append(tool_result_message(tc.id, result))
        if outcome == "defer":
            return "defer"
        verdict = state["results"].get("get_fraud_verdict") or {}
        pool = await db()
        await pool.execute(
            """INSERT INTO case_log (agent_id, run_id, sim_time, order_id, source, verdict, score, details)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb)""",
            self.agent_id, run.run_id, datetime.fromisoformat(run.sim_time), case.get("order_id"), case["source"], outcome or verdict.get("verdict", "unknown"),
            float(verdict.get("score", 0) or 0), json.dumps({"tools": state["done_tools"], "reasons": verdict.get("reasons", [])}, default=str),
        )
        await self.remember(run, f"Case {case.get('order_id') or case.get('case_id')} ({case['source']}): {outcome or 'reviewed'}, "
                                 f"score {verdict.get('score', 'n/a')}.", importance=5 if outcome == "approve" else 8, kind="case")
        return outcome or "reviewed"

    async def _tool(self, run: RunState, name: str, args: dict[str, Any], call_id: str, state: dict[str, Any]) -> Any:
        if name == "escalate_to_human":
            body = {"run_id": run.run_id, "kind": args.get("action", "hold_order"), "requested_by": self.agent_id,
                    "subject": args["order_id"], "rationale": args.get("rationale", ""),
                    "request": {"order_id": args["order_id"], "action": args.get("action"), "value_usd": state.get("value_usd"),
                                "threshold_usd": self.threshold}}
            await self.emit(run, "tool.call", {"tool": name, "args": {k: v for k, v in body.items() if k != "request"}}, location="fraud-office")
            r = await self.approvals.post("/api/approvals", json=body)
            r.raise_for_status()
            result = r.json()
            await self.emit(run, "tool.result", {"tool": name, "ok": True, "summary": f"approval {result['id']} {result['status']}"},
                            location="approval-desk")
            return {"approval_id": result["id"], "status": result["status"]}
        if name in ("hold_order", "release_order"):
            args.update(requested_by=self.agent_id, run_id=run.run_id)
        await self.emit(run, "tool.call", {"tool": name, "server": "store-ops-mcp", "args": args}, location="fraud-office")
        try:
            result = await self.store.call(name, args, call_id)
            ok = not (isinstance(result, dict) and result.get("ok") is False)
        except ToolError as exc:
            result, ok = {"error": str(exc)}, False
        await self.emit(run, "tool.result", {"tool": name, "ok": ok, "summary": summarise(result)}, location="fraud-office")
        return result


agent = FraudReview()
app = agent.app()
