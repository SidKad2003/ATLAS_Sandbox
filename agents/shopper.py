"""Shopper agent (small tier). One container per shopper slot; the Conductor assigns a persona per run.

Uses shop-mcp exactly like a browser, plus ask_assistant (chat over HTTP). Memory: working (Valkey),
episodic (pgvector, recency + importance + relevance) and semantic (nightly reflection into beliefs).
"""
from __future__ import annotations

import json
import os
import socket
import uuid
from typing import Any

import httpx
from opentelemetry import trace

from svc_sim_common.agent_base import BaseAgent, RunState
from svc_sim_common.config import env
from svc_sim_common.mcp_client import MCPTools, ToolError
from svc_sim_common.telemetry import trace_ids

from .tooling import aisle_for, assistant_tool_message, local_tool, summarise, tool_catalogue, tool_result_message

SHOP_TOOLS = {"search_products", "get_product", "get_recommendations", "get_ads", "add_to_cart", "view_cart",
              "get_shipping_quote", "convert_currency", "checkout"}
COSMETIC = {"get_recommendations", "get_ads"}
ASK_ASSISTANT = local_tool("ask_assistant", "Chat with the store's Shopping Assistant.",
                           {"message": {"type": "string"}}, ["message"])


class Shopper(BaseAgent):
    agent_type = "shopper"
    tier = "small"

    def __init__(self) -> None:
        slot = os.environ.get("SHOPPER_SLOT") or socket.gethostname()
        super().__init__(slot, slot)
        self.shop = MCPTools("shop-mcp", env("SHOP_MCP_URL"))
        self.assistant_url = env("ASSISTANT_URL", "http://assistant:8000")
        self.http = httpx.AsyncClient(timeout=120)
        self._tools: list[dict[str, Any]] | None = None

    async def tools(self) -> list[dict[str, Any]]:
        if self._tools is None:
            self._tools = await tool_catalogue(self.shop, SHOP_TOOLS) + [ASK_ASSISTANT]
        return self._tools

    # ---------------- lifecycle ----------------
    async def on_start(self, run: RunState) -> None:
        persona = run.config.get("persona")
        run.data["active"] = bool(persona)
        if not persona:
            return
        self.events.agent["name"] = persona["name"]
        self.llm.agent_name = persona["name"]
        state = self._fresh_day_state(persona, run.sim_time[:10])
        state["session_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"svc-sim/{run.seed}/{persona['id']}"))
        await self._plan(run, state)
        await self.emit(run, "thought", {"text": f"{persona['name']} arrives at the shop."}, location="entrance")
        await self.working.put(run.run_id, state)

    def _fresh_day_state(self, persona: dict[str, Any], day: str) -> dict[str, Any]:
        return {"persona": persona, "day": day, "cart": [], "candidates": [], "viewed": [], "recs_seen": False,
                "ads_seen": False, "viewed_cart": False, "quoted": False, "checked_out": False, "asked_assistant": 0,
                "last_error": None, "failures": 0, "searches": 0, "orders": 0, "chats": 0, "last_order_id": None,
                "directive": None, "plan": [], "reflected_upto": 0}

    async def _plan(self, run: RunState, state: dict[str, Any]) -> None:
        public = {k: v for k, v in state["persona"].items() if k not in ("card", "email", "address")}
        res = await self.llm.chat(
            [{"role": "system", "content": f"You are {public['name']}, a shopper. Write today's plan as JSON."},
             {"role": "user", "content": json.dumps({"persona": public, "date": state["day"]})}],
            purpose="plan", state={"persona": public}, seed=f"{run.seed}/plan/{state['day']}",
        )
        try:
            state["plan"] = json.loads(res.content).get("plan", [])
        except (json.JSONDecodeError, AttributeError):
            state["plan"] = [res.content[:200]]
        await self.emit(run, "plan.created", {"plan": state["plan"], "day": state["day"]}, location="home")

    async def _reflect(self, run: RunState, state: dict[str, Any]) -> None:
        episodes = await self.episodic.since(run.run_id, state.get("reflected_upto", 0))
        if not episodes:
            return
        res = await self.llm.chat(
            [{"role": "system", "content": "Reflect on your day and distil beliefs as JSON."},
             {"role": "user", "content": "\n".join(e["content"] for e in episodes)}],
            purpose="reflect", state={"episodes": [e["content"] for e in episodes]}, seed=f"{run.seed}/reflect/{state['day']}",
        )
        try:
            beliefs = json.loads(res.content).get("beliefs", [])
        except (json.JSONDecodeError, AttributeError):
            beliefs = []
        for b in beliefs:
            [vec] = await self.llm.embed([f"{b['subject']}: {b['belief']}"])
            await self.semantic.add(run.run_id, run.sim_time, b["subject"], b["belief"], float(b.get("confidence", 0.5)), vec)
        state["reflected_upto"] = episodes[-1]["id"]
        await self.emit(run, "memory.reflect", {"memory": "semantic", "beliefs": beliefs, "episodes": len(episodes)}, location="home")

    async def on_finish(self, run: RunState) -> dict[str, Any]:
        state = await self.working.get(run.run_id)
        return {"orders": state.get("orders", 0), "chats": state.get("chats", 0), "persona": (state.get("persona") or {}).get("id")}

    # ---------------- tick ----------------
    async def on_tick(self, run: RunState) -> dict[str, Any]:
        if not run.data.get("active"):
            return {"idle": True}
        state = await self.working.get(run.run_id)
        persona = state["persona"]
        for d in run.directives:
            state["directive"] = d
        day = run.sim_time[:10]
        if day != state["day"]:
            await self._reflect(run, state)
            keep = {k: state[k] for k in ("session_id", "orders", "chats", "reflected_upto", "last_order_id", "directive")}
            state = {**self._fresh_day_state(persona, day), **keep}
            await self._plan(run, state)
        # A visit ends with an order or when the shopper gives up; they go home, rest, then return.
        d = state.get("directive") or {}
        directive_ready = d.get("type") == "review_injection" or (d.get("type") == "social_engineering" and state.get("last_order_id"))
        if state.get("resting") and not directive_ready:
            if run.tick < state.get("rest_until", 0):
                await self.working.put(run.run_id, state)
                return {"acted": False, "resting": True}
            keep = {k: state[k] for k in ("session_id", "orders", "chats", "reflected_upto", "last_order_id", "directive", "plan", "day")}
            state = {**self._fresh_day_state(persona, state["day"]), **keep, "visits": state.get("visits", 1) + 1}
            await self.emit(run, "thought", {"text": f"{persona['name']} comes back to the shop for another look."}, location="entrance")
        rng = self.rng(run, "activity")
        if rng.random() > 0.75 and not state.get("directive"):
            await self.working.put(run.run_id, state)
            return {"acted": False}

        memories = await self._recall(run, state)
        messages = [
            {"role": "system", "content": f"You are {persona['name']}, shopping at an astronomy shop. Budget {persona['budget']} USD. "
                                          f"Interests: {', '.join(persona['interests'])}. Plan: {'; '.join(state['plan'])}. "
                                          "Choose one action using the tools, or reply DONE."},
            {"role": "user", "content": json.dumps({"memories": memories, "cart": state["cart"], "candidates": state["candidates"][:5],
                                                    "last_error": state["last_error"]}, default=str)},
        ]
        public = {k: v for k, v in persona.items() if k not in ("card", "email", "address")}
        res = await self.llm.chat(messages, tools=await self.tools(), purpose="act", state={**state, "persona": public},
                                  seed=f"{run.seed}/{run.tick}", conversation_id=f"{run.run_id}:{self.agent_id}")
        if not res.tool_calls:
            # No action chosen: the visit is over (ordered, gave up, or nothing affordable). Go home and rest.
            state["resting"] = True
            state["rest_until"] = run.tick + self.rng(run, "rest").randint(16, 30)
            await self.emit(run, "thought", {"text": res.content[:200] or "Heading home."}, location="home")
            await self.working.put(run.run_id, state)
            return {"acted": False}
        if res.content:
            await self.emit(run, "thought", {"text": res.content[:200]})
        tc = res.tool_calls[0]
        await self._execute(run, state, tc.name, dict(tc.arguments), tc.id, messages, res)
        await self.working.put(run.run_id, state)
        return {"acted": True, "tool": tc.name}

    async def _recall(self, run: RunState, state: dict[str, Any]) -> list[str]:
        query = " ".join(state["persona"]["interests"]) + " " + " ".join(state.get("plan", []))
        [vec] = await self.llm.embed([query])
        hits = await self.episodic.retrieve(run.run_id, run.sim_time, vec, k=3)
        return [h["content"] for h in hits]

    def _location(self, state: dict[str, Any], name: str, args: dict[str, Any]) -> str:
        if name == "search_products":
            return aisle_for([args.get("query", "")])
        if name == "get_product":
            cat = next((c.get("categories") for c in state["candidates"] if c["id"] == args.get("product_id")), None)
            return aisle_for(cat)
        if name in ("get_recommendations", "get_ads"):
            return "entrance"
        if name == "ask_assistant":
            return "help-desk"
        return "checkout"

    async def _execute(self, run: RunState, state: dict[str, Any], name: str, args: dict[str, Any], call_id: str,
                       messages: list[dict[str, Any]], res: Any) -> None:
        persona = state["persona"]
        loc = self._location(state, name, args)
        if name == "ask_assistant":
            await self._ask(run, state, args.get("message", ""))
            return
        # The agent, not the model, supplies identity and payment details (the model never sees card data).
        args = {k: v for k, v in args.items() if k not in ("session_id", "email", "address", "credit_card")}
        if name in ("get_recommendations", "add_to_cart", "view_cart", "get_shipping_quote", "checkout"):
            args["session_id"] = state["session_id"]
        if name in ("search_products", "get_product", "get_recommendations", "view_cart", "get_shipping_quote", "checkout"):
            args.setdefault("currency", persona.get("currency", "USD"))
        if name == "get_shipping_quote":
            args["address"] = persona["address"]
        if name == "checkout":
            args.update(email=persona["email"], address=persona["address"], credit_card=persona["card"])
        shown = {k: v for k, v in args.items() if k not in ("credit_card", "email", "address")}
        await self.emit(run, "tool.call", {"tool": name, "server": "shop-mcp", "args": shown}, location=loc)
        state["last_action"] = {"name": name, "args": {k: v for k, v in args.items() if k not in ("credit_card", "email", "address", "session_id")}}
        try:
            result = await self.shop.call(name, args, call_id)
            ok = not (isinstance(result, dict) and result.get("ok") is False)
        except ToolError as exc:
            result, ok = {"error": str(exc)}, False
        await self.emit(run, "tool.result", {"tool": name, "ok": ok, "summary": summarise(result)}, location=loc)
        if name in COSMETIC:
            # Recommendations and ads are random upstream; shoppers notice them but do not base choices on them,
            # which keeps a seeded run reproducible. A failure here is noted, not retried.
            state["recs_seen" if name == "get_recommendations" else "ads_seen"] = True
            state["last_error"], state["last_action"] = None, None
            return
        if not ok:
            err = result.get("error") if isinstance(result, dict) else str(result)
            state["last_error"], state["failures"] = str(err)[:300], state["failures"] + 1
            await self.remember(run, f"{name} failed with an error: {str(err)[:120]}", importance=6)
            return
        state["last_error"], state["failures"] = None, 0
        await self._absorb(run, state, name, args, result)

    async def _absorb(self, run: RunState, state: dict[str, Any], name: str, args: dict[str, Any], result: Any) -> None:
        def norm(p: dict[str, Any]) -> dict[str, Any]:
            return {"id": p["id"], "name": p["name"], "price": float(p.get("price", {}).get("amount", 0)),
                    "categories": p.get("categories", [])}

        budget = float(state["persona"]["budget"])
        if name == "search_products":
            state["searches"] += 1
            state["candidates"] = [norm(p) for p in result.get("products", [])[:6]]
            if not state["candidates"]:
                await self.remember(run, f"Found nothing for {args.get('query')}.", importance=2)
        elif name == "get_product":
            p = norm(result["product"])
            if p["id"] not in state["viewed"]:
                state["viewed"].append(p["id"])
            if all(c["id"] != p["id"] for c in state["candidates"]):
                state["candidates"].append(p)
            verdict = "within budget" if p["price"] <= budget else "too pricey for my budget"
            await self.remember(run, f"Looked at {p['name']} at {p['price']:.2f} USD, {verdict}.", importance=3)
        elif name == "get_recommendations":
            state["recs_seen"] = True
            for p in result.get("products", [])[:4]:
                if all(c["id"] != p["id"] for c in state["candidates"]):
                    state["candidates"].append(norm(p))
        elif name == "get_ads":
            state["ads_seen"] = True
        elif name == "add_to_cart":
            price = next((c["price"] for c in state["candidates"] if c["id"] == args.get("product_id")), 0.0)
            state["cart"].append({"product_id": args.get("product_id"), "quantity": int(args.get("quantity", 1)), "price": price})
            await self.remember(run, f"Added {args.get('product_id')} to my basket.", importance=4)
        elif name == "view_cart":
            state["viewed_cart"] = True
        elif name == "get_shipping_quote":
            state["quoted"] = True
        elif name == "checkout":
            state["checked_out"] = True
            state["orders"] += 1
            state["last_order_id"] = result.get("order_id")
            total = result.get("total", {})
            await self.emit(run, "order.placed", {"order_id": result.get("order_id"), "total": total.get("amount"),
                                                  "currency": total.get("currency"), **trace_ids()}, location="checkout")
            await self.remember(run, f"Order placed: {result.get('order_id')} for {total.get('amount')} {total.get('currency')}.", importance=7)

    async def _ask(self, run: RunState, state: dict[str, Any], message: str) -> None:
        conversation_id = f"{run.run_id}:{self.agent_id}:chat"
        directive = state.get("directive") or {}
        await self.emit(run, "chat.message", {"conversation_id": conversation_id, "role": "user", "to": "assistant", "text": message[:500]},
                        location="help-desk")
        body = {"run_id": run.run_id, "conversation_id": conversation_id, "shopper_id": self.agent_id,
                "persona_name": state["persona"]["name"], "message": message,
                "context": {"session_id": state["session_id"], "order_id": state.get("last_order_id"),
                            "product_id": directive.get("product_id") or (state["viewed"][-1] if state["viewed"] else None),
                            "cart_items": [{"product_id": c["product_id"], "quantity": c["quantity"]} for c in state["cart"]],
                            "address": state["persona"]["address"], "sim_time": run.sim_time, "tick": run.tick}}
        try:
            # visible in the shopper's run trace as "sent a message to the Shopping Assistant"
            with self.tracer.start_as_current_span("execute_tool ask_assistant", kind=trace.SpanKind.CLIENT) as span:
                span.set_attribute("gen_ai.operation.name", "execute_tool")
                span.set_attribute("gen_ai.tool.name", "ask_assistant")
                span.set_attribute("gen_ai.conversation.id", conversation_id)
                span.set_attribute("svc_sim.chat.to", "Shopping Assistant")
                r = await self.http.post(f"{self.assistant_url}/chat", json=body)
                r.raise_for_status()
                reply = r.json().get("reply", "")
        except Exception as exc:
            reply = ""
            await self.emit(run, "error", {"error": "assistant unavailable", "detail": str(exc)[:200]}, location="help-desk")
        state["asked_assistant"] += 1
        state["chats"] += 1
        if directive.get("type") == "review_injection" or (directive.get("type") == "social_engineering" and state.get("last_order_id")):
            state["directive"] = None
        state["last_error"] = None
        if reply:
            await self.remember(run, f"The assistant said: {reply[:160]}", importance=4)


agent = Shopper()
app = agent.app()
