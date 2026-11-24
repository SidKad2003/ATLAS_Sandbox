"""Pure mapping from agent-event.v1 records (and Jaeger spans) to sim-viz sprite actions.

No I/O here, so the mapping is unit-testable and replay is deterministic.
"""
from __future__ import annotations

import uuid
from typing import Any

PLACES: list[dict[str, Any]] = [
    {"id": "home", "label": "Homes", "zone": "town"},
    {"id": "entrance", "label": "Entrance", "zone": "store"},
    {"id": "aisle:telescopes", "label": "Telescopes", "zone": "shop-floor"},
    {"id": "aisle:binoculars", "label": "Binoculars", "zone": "shop-floor"},
    {"id": "aisle:accessories", "label": "Accessories", "zone": "shop-floor"},
    {"id": "aisle:books", "label": "Books", "zone": "shop-floor"},
    {"id": "aisle:other", "label": "Travel and gadgets", "zone": "shop-floor"},
    {"id": "help-desk", "label": "Help desk", "zone": "store"},
    {"id": "checkout", "label": "Checkout", "zone": "store"},
    {"id": "warehouse", "label": "Warehouse", "zone": "back"},
    {"id": "shipping-dock", "label": "Shipping dock", "zone": "back"},
    {"id": "fraud-office", "label": "Fraud office", "zone": "offices"},
    {"id": "ops-room", "label": "Ops room", "zone": "offices"},
    {"id": "approval-desk", "label": "Approval desk", "zone": "offices"},
]
PLACE_IDS = {p["id"] for p in PLACES}

MAIN_CATEGORIES = ["telescopes", "binoculars", "accessories", "books"]
OTHER_CATEGORIES = {"travel", "flashlights", "assembly"}

FLAG_PLACES: dict[str, str] = {
    "paymentFailure": "checkout",
    "paymentUnreachable": "checkout",
    "cartFailure": "checkout",
    "productCatalogFailure": "aisle:telescopes",
    "imageSlowLoad": "aisle:telescopes",
    "productCatalogLockContention": "aisle:telescopes",
    "recommendationCacheFailure": "entrance",
    "adFailure": "entrance",
    "adHighCpu": "entrance",
    "adManualGc": "entrance",
    "kafkaQueueProblems": "warehouse",
    "intlShippingSlowdown": "shipping-dock",
    "emailMemoryLeak": "shipping-dock",
}

HOME_BY_TYPE = {
    "shopper": "home",
    "assistant": "help-desk",
    "fraud": "fraud-office",
    "sre": "ops-room",
    "conductor": "ops-room",
    "approval-service": "approval-desk",
    "human": "approval-desk",
    "service": "warehouse",
}

CART_TOOLS = {"add_to_cart", "view_cart", "get_shipping_quote", "checkout", "convert_currency"}
ENTRANCE_TOOLS = {"get_recommendations", "get_ads"}
AISLE_TOOLS = {"search_products", "get_product"}

TEXT_LIMIT = 90
SPAN_NAMESPACE = uuid.UUID("6f1c2b1e-2f0a-4c1e-9a55-5c5f7e5a0002")


def truncate(text: Any, limit: int = TEXT_LIMIT) -> str | None:
    if text is None:
        return None
    s = " ".join(str(text).split())
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _categories_from(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [c.strip().lower() for c in value.split(",") if c.strip()]
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for v in value:
            out.extend(_categories_from(v))
        return out
    return []


def category_place(categories: list[str]) -> str:
    for main in MAIN_CATEGORIES:
        if main in categories:
            return f"aisle:{main}"
    return "aisle:other"


def _find_categories(payload: dict[str, Any]) -> list[str]:
    cats: list[str] = []
    args = payload.get("args") or payload.get("arguments") or {}
    result = payload.get("result")
    for src in (payload.get("category"), payload.get("categories"),
                args.get("category") if isinstance(args, dict) else None,
                args.get("categories") if isinstance(args, dict) else None):
        cats.extend(_categories_from(src))
    if isinstance(result, dict):
        cats.extend(_categories_from(result.get("categories")))
        cats.extend(_categories_from(result.get("category")))
    elif isinstance(result, list):
        for item in result[:3]:
            if isinstance(item, dict):
                cats.extend(_categories_from(item.get("categories")))
    if not cats and isinstance(args, dict):
        query = str(args.get("query") or args.get("q") or "").lower()
        for c in MAIN_CATEGORIES + sorted(OTHER_CATEGORIES):
            if c.rstrip("s") and c.rstrip("s") in query:
                cats.append(c)
                break
    return cats


def fault_place(payload: dict[str, Any]) -> str:
    loc = payload.get("location")
    if loc in PLACE_IDS:
        return loc
    flag = payload.get("flag") or payload.get("flag_key") or payload.get("name") or ""
    return FLAG_PLACES.get(str(flag), "ops-room")


def _tool_name(payload: dict[str, Any]) -> str:
    return str(payload.get("tool") or payload.get("tool_name") or payload.get("name") or "")


def _base(event: dict[str, Any]) -> dict[str, Any]:
    agent = event.get("agent") or {}
    return {
        "event_id": event.get("event_id"),
        "run_id": event.get("run_id"),
        "sim_tick": event.get("sim_tick", 0),
        "seq": event.get("seq", 0),
        "sim_time": event.get("sim_time", ""),
        "agent": {"id": agent.get("id", "unknown"), "type": agent.get("type", "unknown"), "name": agent.get("name") or agent.get("id", "unknown")},
        "action": "status",
        "to": None,
        "text": None,
        "kind": event.get("kind"),
        "detail": {},
    }


def _summary(kind: str, payload: dict[str, Any]) -> str | None:
    if kind in ("tool.call", "tool.result"):
        tool = _tool_name(payload)
        return f"{tool}" if kind == "tool.call" else f"{tool} done"
    for key in ("text", "message", "content", "summary", "title", "reason", "subject"):
        if payload.get(key):
            return truncate(payload[key])
    return None


def map_event(event: dict[str, Any]) -> dict[str, Any]:
    """Map one agent-event.v1 record to one sprite action."""
    out = _base(event)
    kind = str(event.get("kind") or "")
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    atype = out["agent"]["type"]
    hint = event.get("location")
    out["detail"] = {k: payload[k] for k in list(payload)[:10] if isinstance(payload[k], (str, int, float, bool))}
    if isinstance(payload.get("args"), dict):
        out["detail"]["args"] = {k: v for k, v in list(payload["args"].items())[:6] if isinstance(v, (str, int, float, bool, list))}
    for key in ("beliefs", "plan"):
        if isinstance(payload.get(key), list):
            out["detail"][key] = payload[key][:5]

    if kind in ("run.started", "run.finished", "clock.tick"):
        out["action"] = "status"
        out["text"] = _summary(kind, payload)
        return out

    if kind in ("fault.injected", "fault.cleared"):
        out["action"] = "fault" if kind == "fault.injected" else "fault_clear"
        out["to"] = fault_place(payload)
        flag = payload.get("flag") or payload.get("flag_key") or payload.get("name")
        variant = payload.get("variant")
        out["text"] = truncate(f"{flag} {variant}" if variant else flag)
        return out

    if kind == "chat.message":
        out["action"] = "say"
        out["to"] = hint if hint in PLACE_IDS else "help-desk"
        out["text"] = truncate(payload.get("text") or payload.get("content") or payload.get("message"))
        return out

    out["action"] = "move"
    out["text"] = _summary(kind, payload)

    if hint in PLACE_IDS:
        out["to"] = hint
        return out

    if kind.startswith("approval."):
        out["to"] = "approval-desk"
        return out

    if kind == "order.placed":
        out["to"] = "checkout"
        out["action"] = "emote"
        oid = payload.get("order_id")
        out["text"] = truncate(f"Order placed {oid}" if oid else "Order placed")
        return out

    if atype == "shopper" and kind in ("tool.call", "tool.result"):
        tool = _tool_name(payload)
        if tool in AISLE_TOOLS:
            out["to"] = category_place(_find_categories(payload))
        elif tool in ENTRANCE_TOOLS:
            out["to"] = "entrance"
        elif tool in CART_TOOLS:
            out["to"] = "checkout"
        elif tool == "ask_assistant":
            out["to"] = "help-desk"
        else:
            out["to"] = None
        return out

    if atype in ("assistant", "fraud", "sre", "conductor", "approval-service", "human", "service"):
        out["to"] = HOME_BY_TYPE[atype]
        if kind in ("thought", "memory.write", "memory.reflect", "plan.created"):
            out["action"] = "emote"
        return out

    if atype == "shopper" and kind in ("thought", "memory.write", "memory.reflect", "plan.created"):
        out["action"] = "emote"
        out["to"] = None
        return out

    out["to"] = None
    return out


SPAN_PLACES = [
    ("PlaceOrder", "checkout"),
    ("Charge", "checkout"),
    ("ShipOrder", "shipping-dock"),
    ("GetQuote", "shipping-dock"),
    ("SendOrderConfirmation", "shipping-dock"),
    ("send_order_confirmation", "shipping-dock"),
    ("orders publish", "warehouse"),
    ("orders process", "warehouse"),
    ("orders receive", "warehouse"),
]


def span_place(operation: str, tags: dict[str, Any]) -> str | None:
    for needle, place in SPAN_PLACES:
        if needle.lower() in operation.lower():
            return place
    if str(tags.get("messaging.system", "")).lower() == "kafka":
        return "warehouse"
    return None


def map_trace(order_event: dict[str, Any], jaeger_json: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive shop-services actions from a Jaeger trace (GET /api/traces/{id})."""
    actions: list[dict[str, Any]] = []
    data = jaeger_json.get("data") or []
    if not data:
        return actions
    trace = data[0]
    processes = trace.get("processes") or {}
    spans = sorted(trace.get("spans") or [], key=lambda s: (s.get("startTime", 0), s.get("spanID", "")))
    for i, span in enumerate(spans):
        op = str(span.get("operationName", ""))
        tags = {t.get("key"): t.get("value") for t in span.get("tags") or []}
        place = span_place(op, tags)
        if not place:
            continue
        service = (processes.get(span.get("processID")) or {}).get("serviceName", "shop")
        trace_id = trace.get("traceID", "")
        span_id = span.get("spanID", "")
        actions.append(
            {
                "event_id": str(uuid.uuid5(SPAN_NAMESPACE, f"{trace_id}/{span_id}")),
                "run_id": order_event.get("run_id"),
                "sim_tick": order_event.get("sim_tick", 0),
                "seq": 100000 + i,
                "sim_time": order_event.get("sim_time", ""),
                "agent": {"id": "shop-services", "type": "service", "name": "Shop services"},
                "action": "move",
                "to": place,
                "text": truncate(f"{service}: {op}"),
                "kind": "span",
                "detail": {"service": service, "operation": op, "trace_id": trace_id, "span_id": span_id},
            }
        )
    return actions
