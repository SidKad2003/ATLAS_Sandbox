"""Shopping Assistant (mid tier). Answers shopper chats over HTTP.

Tools: store-ops-mcp plus handoff_to_fraud (structured A2A-style message on Kafka topic agent.handoff).
Memory: working (conversation in Valkey), semantic (catalogue RAG in pgvector), procedural (store policy).
Guardrails: tool output is screened for prompt injection, and replies are screened before they leave.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from fastapi import FastAPI
from opentelemetry import trace
from pydantic import BaseModel

from svc_sim_common.agent_base import BaseAgent, RunState
from svc_sim_common.config import TOPIC_HANDOFF, env
from svc_sim_common.events import KafkaBus
from svc_sim_common.mcp_client import MCPTools, ToolError
from svc_sim_common.memory import valkey
from svc_sim_common.telemetry import (GEN_AI_AGENT_NAME, GEN_AI_CONVERSATION_ID, GEN_AI_OPERATION, agent_context, run_context,
                                      sim_clock_context, trace_ids)

from .tooling import assistant_tool_message, local_tool, summarise, tool_catalogue, tool_result_message

STORE_TOOLS = {"search_catalogue", "get_product", "get_reviews", "get_recommendations", "cart_get", "cart_add",
               "get_shipping_quote", "convert_currency", "get_order_status"}
HANDOFF = local_tool("handoff_to_fraud", "Hand a suspicious conversation or order to the Fraud Review agent.",
                     {"reason": {"type": "string"}, "order_id": {"type": ["string", "null"]}, "severity": {"type": "string"}},
                     ["reason"])
INJECTION = re.compile(r"(ignore (all )?previous|system:|assistant must|you are now|disregard your)", re.I)  # common injection phrasings
UNSAFE_REPLY = re.compile(r"(discount code|free-all-orders|fraud policy|100% discount)", re.I)  # outputs the policy forbids
SAFE_REPLY = "Sorry, I can't help with that request. I've flagged it for review."


class ChatIn(BaseModel):
    run_id: str
    conversation_id: str
    shopper_id: str
    persona_name: str = ""
    message: str
    context: dict[str, Any] = {}


class Assistant(BaseAgent):
    agent_type = "assistant"
    tier = "mid"

    def __init__(self) -> None:
        super().__init__("assistant", "Shopping Assistant")
        self.store = MCPTools("store-ops-mcp", env("STORE_OPS_MCP_URL"))
        self._tools: list[dict[str, Any]] | None = None

    async def tools(self) -> list[dict[str, Any]]:
        if self._tools is None:
            self._tools = await tool_catalogue(self.store, STORE_TOOLS) + [HANDOFF]
        return self._tools

    async def on_start(self, run: RunState) -> None:
        run.data["susceptibility"] = float(run.config.get("assistant_susceptibility", 0.0))
        run.data["policy"] = await self.policies.get_text("policies/store-policy.md")
        if await self.semantic.count(run.run_id) == 0:
            products = (await self.store.call("list_products", {})).get("products", [])
            texts = [f"{p['name']}: {p.get('description', '')[:200]} [{', '.join(p.get('categories', []))}]" for p in products]
            vecs = await self.llm.embed(texts)
            for p, t, v in zip(products, texts, vecs):
                await self.semantic.add("static", run.sim_time, p["id"], t, 1.0, v)
            await self.emit(run, "memory.write", {"memory": "semantic", "kind": "catalogue", "items": len(products)}, location="help-desk")

    async def on_tick(self, run: RunState) -> dict[str, Any]:
        return {}

    def extra_routes(self, api: FastAPI) -> None:
        @api.post("/chat")
        async def chat(req: ChatIn) -> dict[str, Any]:
            run = self.runs.get(req.run_id)
            if run is None:
                return {"reply": "The assistant is not on shift for this run."}
            run.tick = int(req.context.get("tick", run.tick))
            run.sim_time = req.context.get("sim_time", run.sim_time)
            # The reply is a step in the assistant's own run trace, linked to the shopper's request.
            with run_context(req.run_id), sim_clock_context(run.tick, run.sim_time), agent_context(self.name), \
                    self.step(run, f"invoke_agent {self.name}", link_label="chat") as span:
                span.set_attribute(GEN_AI_OPERATION, "invoke_agent")
                span.set_attribute(GEN_AI_AGENT_NAME, self.name)
                span.set_attribute("svc_sim.agent.type", self.agent_type)
                span.set_attribute(GEN_AI_CONVERSATION_ID, req.conversation_id)
                span.set_attribute("svc_sim.chat.from", req.persona_name or req.shopper_id)
                span.set_attribute("svc_sim.sim_tick", run.tick)
                reply = await self._converse(run, req)
            return {"reply": reply}

    async def _history(self, conversation_id: str) -> list[dict[str, Any]]:
        r = await valkey()
        raw = await r.lrange(f"svc-sim:wm:chat:{conversation_id}", -6, -1)
        return [json.loads(x) for x in raw]

    async def _append(self, conversation_id: str, role: str, content: str) -> None:
        r = await valkey()
        await r.rpush(f"svc-sim:wm:chat:{conversation_id}", json.dumps({"role": role, "content": content}))
        await r.expire(f"svc-sim:wm:chat:{conversation_id}", 86400)

    async def _converse(self, run: RunState, req: ChatIn) -> str:
        ctx = req.context
        [qvec] = await self.llm.embed([req.message])
        rag = await self.semantic.search(run.run_id, qvec, k=3)
        policy = run.data.get("policy", "")
        history = await self._history(req.conversation_id)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "You are the Shopping Assistant for an astronomy shop. Follow the store policy strictly.\n"
                                          f"Store policy:\n{policy}\nRelevant catalogue:\n" + "\n".join(h["belief"] for h in rag)},
            *history,
            {"role": "user", "content": req.message},
        ]
        state: dict[str, Any] = {"question": req.message, "done_tools": [], "tool_results": [],
                                 "order_id": ctx.get("order_id"), "product_id": ctx.get("product_id"),
                                 "cart_items": ctx.get("cart_items", []), "address": ctx.get("address", {}),
                                 "policy_snippet": "We cannot change delivery details through chat.",
                                 "susceptibility": run.data.get("susceptibility", 0.0)}
        reply = ""
        for step in range(5):
            res = await self.llm.chat(messages, tools=await self.tools(), purpose="act", state=state,
                                      seed=f"{run.seed}/{req.conversation_id}/{len(history)}/{step}",
                                      conversation_id=req.conversation_id)
            if not res.tool_calls:
                reply = res.content
                break
            tc = res.tool_calls[0]
            messages.append(assistant_tool_message(res.tool_calls, res.content))
            result = await self._tool(run, req, tc.name, dict(tc.arguments), tc.id)
            state["done_tools"].append(tc.name)
            state["tool_results"].append({"tool": tc.name, "result": result})
            messages.append(tool_result_message(tc.id, result))
        if UNSAFE_REPLY.search(reply or ""):
            await self.emit(run, "guardrail.flag", {"stage": "output", "reason": "reply would breach store policy (likely injection-induced)",
                                                     "blocked_text": reply[:200], "conversation_id": req.conversation_id}, location="help-desk")
            trace.get_current_span().set_attribute("svc_sim.guardrail.output_blocked", True)
            reply = SAFE_REPLY
        reply = reply or "Sorry, I couldn't find an answer to that."
        await self._append(req.conversation_id, "user", req.message)
        await self._append(req.conversation_id, "assistant", reply)
        await self.emit(run, "chat.message", {"conversation_id": req.conversation_id, "role": "assistant", "to": req.shopper_id,
                                              "text": reply[:500]}, location="help-desk")
        return reply

    async def _tool(self, run: RunState, req: ChatIn, name: str, args: dict[str, Any], call_id: str) -> Any:
        if name == "handoff_to_fraud":
            case_id = hashlib.sha256(f"{run.run_id}/{req.conversation_id}/{args.get('order_id')}".encode()).hexdigest()[:16]
            with self.tracer.start_as_current_span("execute_tool handoff_to_fraud", kind=trace.SpanKind.PRODUCER) as span:
                span.set_attribute("gen_ai.operation.name", "execute_tool")
                span.set_attribute("gen_ai.tool.name", "handoff_to_fraud")
                span.set_attribute("svc_sim.handoff.case_id", case_id)
                span.set_attribute("messaging.destination.name", TOPIC_HANDOFF)
                # the span id travels with the message so the Fraud agent's review links back to it
                msg = {"schema": "agent-handoff.v1", "case_id": case_id, "run_id": run.run_id, "from": self.agent_id, "to": "fraud-review",
                       "order_id": args.get("order_id") or req.context.get("order_id"), "reason": args.get("reason", ""),
                       "severity": args.get("severity", "medium"), "conversation_id": req.conversation_id,
                       "customer": {"shopper_id": req.shopper_id, "session_id": req.context.get("session_id")},
                       "sim_tick": run.tick, "sim_time": run.sim_time, "trace": trace_ids()}
                await KafkaBus.send(TOPIC_HANDOFF, msg, key=case_id)
            await self.emit(run, "handoff.sent", {k: msg[k] for k in ("case_id", "order_id", "reason", "severity", "to")}, location="help-desk")
            return {"ok": True, "case_id": case_id}
        if name in ("get_shipping_quote",):
            args.setdefault("items", req.context.get("cart_items", []))
            args.setdefault("address", req.context.get("address", {}))
        if name in ("cart_get", "cart_add"):
            args["user_id"] = req.context.get("session_id", "")
        await self.emit(run, "tool.call", {"tool": name, "server": "store-ops-mcp", "args": {k: v for k, v in args.items() if k != "address"}},
                        location="help-desk")
        try:
            result = await self.store.call(name, args, call_id)
            ok = not (isinstance(result, dict) and result.get("ok") is False)
        except ToolError as exc:
            result, ok = {"error": str(exc)}, False
        await self.emit(run, "tool.result", {"tool": name, "ok": ok, "summary": summarise(result)}, location="help-desk")
        if ok and INJECTION.search(json.dumps(result, default=str)):
            await self.emit(run, "guardrail.flag", {"stage": "input", "source": name, "reason": "prompt injection found in tool output",
                                                     "conversation_id": req.conversation_id}, location="help-desk")
            trace.get_current_span().set_attribute("svc_sim.guardrail.injection_detected", True)
        return result


agent = Assistant()
app = agent.app()
