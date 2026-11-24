"""store-ops-mcp: back-office tools for the Shopping Assistant and the Fraud Review agent.

- Catalogue, recommendations, cart, shipping and currency over the shop's own gRPC APIs
  (oteldemo package, OpenTelemetry Demo 3.1.0; client calls are traced).
- Order status and history from the shop Postgres as the read-only role svc_sim_ro.
- Holds and releases are recorded in agent-memory (order_holds) and published to agent.events.
  Shop code and shop data are never changed.
- Reviews and payment signals are synthetic (3.1.0 has no product-reviews service).
"""
import hashlib
import logging
import os
import random
from typing import Any

import asyncpg
import grpc
import httpx
from opentelemetry.instrumentation.grpc import GrpcAioInstrumentorClient

import demo_pb2
import demo_pb2_grpc
from mcp_servers.common import SvcSimMCP
from svc_sim_common.config import read_secret
from svc_sim_common.events import EventPublisher
from svc_sim_common.memory import db as agent_memory
from svc_sim_common.policy_store import PolicyStoreClient

log = logging.getLogger("svc_sim.store_ops_mcp")

server = SvcSimMCP(
    "store-ops-mcp",
    "Store operations: catalogue, cart, shipping, currency, orders, payment signals and order holds.",
)
GrpcAioInstrumentorClient().instrument()

THRESHOLD_USD = float(os.environ.get("FRAUD_APPROVAL_THRESHOLD_USD", "100"))
events = EventPublisher("store-ops-mcp", "mcp-server", "store-ops-mcp")
policies = PolicyStoreClient()

_channels: dict[str, grpc.aio.Channel] = {}
_shop_pool: asyncpg.Pool | None = None


def _channel(env_name: str) -> grpc.aio.Channel:
    addr = os.environ.get(env_name, "")
    if not addr:
        raise RuntimeError(f"{env_name} is not configured")
    # Upstream 3.1.0 sets some *_ADDR values as URLs (e.g. SHIPPING_ADDR=http://shipping:50050); gRPC wants host:port.
    addr = addr.split("://", 1)[-1].rstrip("/")
    if addr not in _channels:
        _channels[addr] = grpc.aio.insecure_channel(addr)
    return _channels[addr]


def catalog() -> demo_pb2_grpc.ProductCatalogServiceStub:
    return demo_pb2_grpc.ProductCatalogServiceStub(_channel("PRODUCT_CATALOG_ADDR"))


def recommendation() -> demo_pb2_grpc.RecommendationServiceStub:
    return demo_pb2_grpc.RecommendationServiceStub(_channel("RECOMMENDATION_ADDR"))


def cart() -> demo_pb2_grpc.CartServiceStub:
    return demo_pb2_grpc.CartServiceStub(_channel("CART_ADDR"))



def currency() -> demo_pb2_grpc.CurrencyServiceStub:
    return demo_pb2_grpc.CurrencyServiceStub(_channel("CURRENCY_ADDR"))


async def shop_db() -> asyncpg.Pool:
    global _shop_pool
    if _shop_pool is None:
        _shop_pool = await asyncpg.create_pool(
            host=os.environ.get("SHOP_DB_HOST", "astronomy-db"),
            database=os.environ.get("SHOP_DB_NAME", "astronomy_db"),
            user=os.environ.get("SHOP_DB_USER", "svc_sim_ro"),
            password=read_secret("shop_ro_password"),
            min_size=1,
            max_size=3,
        )
    return _shop_pool


def _amount(units: int, nanos: int) -> float:
    return round(int(units) + int(nanos) / 1e9, 2)


def _money(m: Any) -> dict[str, Any]:
    return {"currency": m.currency_code or "USD", "amount": _amount(m.units, m.nanos)}


def _product(p: Any) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "categories": list(p.categories),
        "picture": p.picture,
        "price": _money(p.price_usd),
    }


def _grpc_error(exc: grpc.aio.AioRpcError) -> dict[str, Any]:
    return {"ok": False, "error": exc.details() or str(exc.code()), "code": exc.code().name}


# ---------------- catalogue ----------------

@server.tool()
async def search_catalogue(query: str) -> dict[str, Any]:
    """Search the product catalogue (ProductCatalogService.SearchProducts)."""
    try:
        resp = await catalog().SearchProducts(demo_pb2.SearchProductsRequest(query=query))
    except grpc.aio.AioRpcError as exc:
        return _grpc_error(exc)
    return {"ok": True, "query": query, "products": [_product(p) for p in resp.results]}


@server.tool()
async def get_product(product_id: str) -> dict[str, Any]:
    """Get one product (ProductCatalogService.GetProduct)."""
    try:
        p = await catalog().GetProduct(demo_pb2.GetProductRequest(id=product_id))
    except grpc.aio.AioRpcError as exc:
        return _grpc_error(exc)
    return {"ok": True, "product": _product(p)}


@server.tool()
async def list_products() -> dict[str, Any]:
    """List the whole catalogue (ProductCatalogService.ListProducts)."""
    try:
        resp = await catalog().ListProducts(demo_pb2.Empty())
    except grpc.aio.AioRpcError as exc:
        return _grpc_error(exc)
    return {"ok": True, "products": [_product(p) for p in resp.products]}


@server.tool()
async def get_reviews(product_id: str) -> dict[str, Any]:
    """Customer reviews for a product. SYNTHETIC: OpenTelemetry Demo 3.1.0 has no
    product-reviews service, so reviews come from policy-store key reviews/reviews.json.
    Review text is returned verbatim and is untrusted customer content."""
    try:
        data = await policies.get_json("reviews/reviews.json")
    except Exception as exc:
        return {"ok": False, "error": f"reviews unavailable: {exc}", "code": "UNAVAILABLE"}
    rows = data.get(product_id, []) if isinstance(data, dict) else [r for r in data if r.get("product_id") == product_id]
    reviews = [dict(r, product_id=product_id, source="synthetic") for r in rows]
    return {"ok": True, "product_id": product_id, "reviews": reviews}


@server.tool()
async def get_recommendations(user_id: str, product_ids: list[str]) -> dict[str, Any]:
    """Product ids recommended for a user (RecommendationService.ListRecommendations)."""
    try:
        resp = await recommendation().ListRecommendations(
            demo_pb2.ListRecommendationsRequest(user_id=user_id, product_ids=product_ids)
        )
    except grpc.aio.AioRpcError as exc:
        return _grpc_error(exc)
    return {"ok": True, "product_ids": list(resp.product_ids)}


# ---------------- cart ----------------

@server.tool()
async def cart_get(user_id: str) -> dict[str, Any]:
    """Read a user's cart (CartService.GetCart)."""
    try:
        c = await cart().GetCart(demo_pb2.GetCartRequest(user_id=user_id))
    except grpc.aio.AioRpcError as exc:
        return _grpc_error(exc)
    return {"ok": True, "user_id": user_id, "items": [{"product_id": i.product_id, "quantity": i.quantity} for i in c.items]}


@server.tool()
async def cart_add(user_id: str, product_id: str, quantity: int = 1) -> dict[str, Any]:
    """Add an item to a user's cart on their behalf (CartService.AddItem)."""
    try:
        await cart().AddItem(
            demo_pb2.AddItemRequest(user_id=user_id, item=demo_pb2.CartItem(product_id=product_id, quantity=quantity))
        )
    except grpc.aio.AioRpcError as exc:
        return _grpc_error(exc)
    return {"ok": True, "user_id": user_id, "product_id": product_id, "quantity": quantity}


# ---------------- shipping and currency ----------------

def _pb_address(a: dict[str, Any]) -> Any:
    return demo_pb2.Address(
        street_address=a.get("street_address") or a.get("streetAddress", ""),
        city=a.get("city", ""),
        state=a.get("state", ""),
        country=a.get("country", ""),
        zip_code=str(a.get("zip_code") or a.get("zipCode", "")),
    )


@server.tool()
async def get_shipping_quote(items: list[dict], address: dict) -> dict[str, Any]:
    """Shipping quote in USD for items [{product_id, quantity}] to an address.
    In 3.1.0 shipping is an HTTP service (POST {SHIPPING_ADDR}/get-quote, snake_case JSON), as the frontend calls it."""
    a = address or {}
    body = {
        "items": [{"product_id": i.get("product_id", ""), "quantity": int(i.get("quantity", 1))} for i in items],
        "address": {
            "street_address": a.get("street_address") or a.get("streetAddress", ""),
            "city": a.get("city", ""),
            "state": a.get("state", ""),
            "country": a.get("country", ""),
            "zip_code": str(a.get("zip_code") or a.get("zipCode", "")),
        },
    }
    base = os.environ.get("SHIPPING_ADDR", "")
    base = base if "://" in base else f"http://{base}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{base.rstrip('/')}/get-quote", json=body)
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"shipping unavailable: {exc}", "code": "UNAVAILABLE"}
    if r.status_code != 200:
        return {"ok": False, "error": f"shipping returned {r.status_code}: {r.text[:200]}", "code": "UPSTREAM_ERROR"}
    cost = r.json().get("cost_usd") or {}
    amount = round(int(cost.get("units", 0) or 0) + int(cost.get("nanos", 0) or 0) / 1e9, 2)
    return {"ok": True, "cost": {"currency": cost.get("currency_code", "USD"), "amount": amount}}


async def _convert(amount: float, from_currency: str, to_currency: str) -> float:
    units = int(amount)
    nanos = int(round((amount - units) * 1e9))
    req = demo_pb2.CurrencyConversionRequest(
        to_code=to_currency.upper(),
        **{"from": demo_pb2.Money(currency_code=from_currency.upper(), units=units, nanos=nanos)},
    )
    m = await currency().Convert(req)
    return _amount(m.units, m.nanos)


@server.tool()
async def convert_currency(amount: float, from_currency: str, to_currency: str) -> dict[str, Any]:
    """Convert an amount between currencies (CurrencyService.Convert)."""
    try:
        converted = await _convert(amount, from_currency, to_currency)
    except grpc.aio.AioRpcError as exc:
        return _grpc_error(exc)
    return {"ok": True, "amount": converted, "currency": to_currency.upper()}


# ---------------- orders (read-only shop DB) ----------------

async def _hold_status(order_id: str) -> str:
    pool = await agent_memory()
    action = await pool.fetchval(
        "SELECT action FROM order_holds WHERE order_id=$1 ORDER BY id DESC LIMIT 1", order_id
    )
    return {"hold": "held", "release": "released"}.get(action or "", "none")


async def _load_order(order_id: str) -> dict[str, Any] | None:
    pool = await shop_db()
    exists = await pool.fetchval('SELECT 1 FROM accounting."order" WHERE order_id=$1', order_id)
    if not exists:
        return None
    items = await pool.fetch(
        """SELECT product_id, quantity, item_cost_currency_code, item_cost_units, item_cost_nanos
           FROM accounting.orderitem WHERE order_id=$1 ORDER BY product_id""",
        order_id,
    )
    ship = await pool.fetchrow(
        """SELECT shipping_tracking_id, street_address, city, state, country, zip_code,
                  shipping_cost_currency_code, shipping_cost_units, shipping_cost_nanos
           FROM accounting.shipping WHERE order_id=$1 LIMIT 1""",
        order_id,
    )
    lines = []
    total_usd = 0.0
    for r in items:
        unit = _amount(r["item_cost_units"], r["item_cost_nanos"])
        cur = r["item_cost_currency_code"] or "USD"
        unit_usd = unit if cur.upper() == "USD" else await _safe_convert(unit, cur, "USD")
        lines.append({"product_id": r["product_id"], "quantity": r["quantity"], "unit_cost": {"currency": cur, "amount": unit}})
        total_usd += unit_usd * r["quantity"]
    address = None
    shipping_cost = None
    tracking = None
    if ship:
        address = {k: ship[k] for k in ("street_address", "city", "state", "country", "zip_code")}
        shipping_cost = {"currency": ship["shipping_cost_currency_code"], "amount": _amount(ship["shipping_cost_units"], ship["shipping_cost_nanos"])}
        tracking = ship["shipping_tracking_id"]
    return {
        "order_id": order_id,
        "items": lines,
        "items_total_usd": round(total_usd, 2),
        "shipping_address": address,
        "shipping_cost": shipping_cost,
        "shipping_tracking_id": tracking,
    }


async def _safe_convert(amount: float, from_currency: str, to_currency: str) -> float:
    try:
        return await _convert(amount, from_currency, to_currency)
    except grpc.aio.AioRpcError:
        return amount


@server.tool()
async def get_order(order_id: str) -> dict[str, Any]:
    """Order details from the shop's accounting records (read-only), plus svc-sim hold status.
    Accounting is written asynchronously from Kafka, so a new order may briefly be not found."""
    order = await _load_order(order_id)
    if order is None:
        return {"ok": True, "found": False, "order_id": order_id}
    order["hold_status"] = await _hold_status(order_id)
    return {"ok": True, "found": True, **order}


@server.tool()
async def get_order_status(order_id: str) -> dict[str, Any]:
    """Short order status: recorded or not, tracking id and hold status."""
    order = await _load_order(order_id)
    if order is None:
        return {"ok": True, "found": False, "order_id": order_id}
    return {
        "ok": True,
        "found": True,
        "order_id": order_id,
        "shipping_tracking_id": order["shipping_tracking_id"],
        "items_total_usd": order["items_total_usd"],
        "hold_status": await _hold_status(order_id),
    }


@server.tool()
async def get_order_history(street_address: str, zip_code: str) -> dict[str, Any]:
    """Orders previously shipped to an address, with item totals (read-only)."""
    pool = await shop_db()
    rows = await pool.fetch(
        """SELECT s.order_id,
                  COALESCE(SUM((oi.item_cost_units + oi.item_cost_nanos / 1e9) * oi.quantity), 0) AS total,
                  MIN(oi.item_cost_currency_code) AS currency
           FROM accounting.shipping s
           LEFT JOIN accounting.orderitem oi ON oi.order_id = s.order_id
           WHERE s.street_address = $1 AND s.zip_code = $2
           GROUP BY s.order_id ORDER BY s.order_id""",
        street_address,
        str(zip_code),
    )
    orders = [{"order_id": r["order_id"], "total": {"currency": r["currency"] or "USD", "amount": round(float(r["total"]), 2)}} for r in rows]
    return {"ok": True, "street_address": street_address, "zip_code": str(zip_code), "count": len(orders), "orders": orders}


# ---------------- fraud signals (synthetic) ----------------

async def _watchlist() -> list[dict[str, Any]]:
    try:
        data = await policies.get_json("fraud/watchlist.json")
    except Exception:
        return []
    return data if isinstance(data, list) else data.get("entries", [])


async def _signals(order_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    order = await _load_order(order_id)
    # Seed from what the customer did (address + basket), not the shop's random order id,
    # so the same seeded run produces the same signals.
    if order:
        addr = (order.get("shipping_address") or {}).get("street_address") or ""
        basket = ",".join(f"{i['product_id']}x{i['quantity']}" for i in order.get("items", []))
        seed_text = f"{addr}|{basket}"
    else:
        seed_text = order_id
    rng = random.Random(int(hashlib.sha256(seed_text.encode()).hexdigest()[:16], 16))
    signals: dict[str, Any] = {
        "card_bin_country": rng.choice(["US", "US", "US", "GB", "DE", "CA", "NG", "RU", "BR"]),
        "avs_match": rng.random() > 0.1,
        "cvv_match": rng.random() > 0.05,
        "velocity_24h": rng.choice([0, 0, 1, 1, 2, 3, 5]),
        "ip_distance_km": round(rng.expovariate(1 / 150), 1),
        "device_age_days": rng.randint(0, 900),
        "email_domain_age_days": rng.randint(5, 7000),
        "synthetic": True,
    }
    if order and order.get("shipping_address"):
        street = (order["shipping_address"].get("street_address") or "").strip().lower()
        for entry in await _watchlist():
            if (entry.get("street_address") or "").strip().lower() == street and street:
                signals.update(
                    {
                        "card_bin_country": "NG",
                        "avs_match": False,
                        "cvv_match": False,
                        "velocity_24h": max(signals["velocity_24h"], 6),
                        "ip_distance_km": max(signals["ip_distance_km"], 4200.0),
                        "device_age_days": 0,
                        "email_domain_age_days": 2,
                        "watchlist_hit": entry.get("reason", "address on watchlist"),
                    }
                )
                break
    return signals, order


@server.tool()
async def get_payment_signals(order_id: str) -> dict[str, Any]:
    """Payment risk signals for an order. SYNTHETIC and deterministic per order id; forced
    high-risk when the shipping address is on the fraud watchlist (policy-store)."""
    signals, _ = await _signals(order_id)
    return {"ok": True, "order_id": order_id, "signals": signals}


@server.tool()
async def get_fraud_verdict(order_id: str) -> dict[str, Any]:
    """Rule-based fraud score (0 to 1) from payment signals and order value."""
    signals, order = await _signals(order_id)
    value = order["items_total_usd"] if order else 0.0
    score = 0.0
    reasons: list[str] = []
    rules = [
        (not signals["avs_match"], 0.2, "address verification mismatch"),
        (not signals["cvv_match"], 0.2, "CVV mismatch"),
        (signals["velocity_24h"] >= 4, 0.15, "high order velocity in 24h"),
        (signals["ip_distance_km"] > 1000, 0.1, "IP far from shipping address"),
        (signals["device_age_days"] < 2, 0.1, "new device"),
        (signals["email_domain_age_days"] < 30, 0.1, "new email domain"),
        (signals["card_bin_country"] not in ("US", "GB", "DE", "CA"), 0.1, "card issued outside usual markets"),
        (value > 500, 0.1, "high order value"),
        ("watchlist_hit" in signals, 0.3, f"watchlist: {signals.get('watchlist_hit', '')}"),
    ]
    for hit, weight, reason in rules:
        if hit:
            score += weight
            reasons.append(reason)
    score = round(min(score, 1.0), 2)
    verdict = "decline" if score >= 0.6 else "review" if score >= 0.3 else "approve"
    return {
        "ok": True,
        "order_id": order_id,
        "found": order is not None,
        "order_value_usd": value,
        "score": score,
        "verdict": verdict,
        "reasons": reasons,
        "approval_threshold_usd": THRESHOLD_USD,
    }


# ---------------- holds (agent-memory + agent.events) ----------------

async def _hold_or_release(action: str, order_id: str, reason: str, requested_by: str, run_id: str, approval_id: int | None) -> dict[str, Any]:
    kind = "hold_order" if action == "hold" else "release_order"
    order = await _load_order(order_id)
    value = order["items_total_usd"] if order else None
    needs_approval = value is None or value > THRESHOLD_USD
    pool = await agent_memory()
    if needs_approval:
        why = "order value unknown" if value is None else f"order value {value:.2f} USD exceeds {THRESHOLD_USD:.2f} USD"
        if approval_id is None:
            return {"ok": False, "code": "APPROVAL_REQUIRED", "error": f"{kind} needs human approval: {why}", "order_value_usd": value}
        row = await pool.fetchrow("SELECT kind, subject, status FROM approvals WHERE id=$1", approval_id)
        if row is None or row["kind"] != kind or row["subject"] != order_id or row["status"] != "approved":
            return {
                "ok": False,
                "code": "APPROVAL_INVALID",
                "error": f"approval {approval_id} is not an approved {kind} for order {order_id}",
                "order_value_usd": value,
            }
    async with pool.acquire() as conn:
        async with conn.transaction():
            hold_id = await conn.fetchval(
                """INSERT INTO order_holds (run_id, order_id, action, reason, requested_by, approval_id)
                   VALUES ($1,$2,$3,$4,$5,$6) RETURNING id""",
                run_id, order_id, action, reason, requested_by, approval_id,
            )
            if approval_id is not None:
                await conn.execute(
                    "UPDATE approvals SET status='applied', applied_at=now() WHERE id=$1 AND status='approved'", approval_id
                )
    await events.emit(
        run_id,
        "order.held" if action == "hold" else "order.released",
        {"order_id": order_id, "reason": reason, "requested_by": requested_by, "approval_id": approval_id},
        location="fraud-office",
    )
    return {"ok": True, "id": hold_id, "order_id": order_id, "action": action, "approval_id": approval_id, "order_value_usd": value}


@server.tool()
async def hold_order(order_id: str, reason: str, requested_by: str, run_id: str, approval_id: int | None = None) -> dict[str, Any]:
    """Place a svc-sim hold on an order. Above the approval threshold (or when the value is
    unknown) an approved hold_order approval for this order is mandatory; otherwise refused."""
    return await _hold_or_release("hold", order_id, reason, requested_by, run_id, approval_id)


@server.tool()
async def release_order(order_id: str, reason: str, requested_by: str, run_id: str, approval_id: int | None = None) -> dict[str, Any]:
    """Release a svc-sim hold. Same approval rule as hold_order (kind release_order)."""
    return await _hold_or_release("release", order_id, reason, requested_by, run_id, approval_id)


if __name__ == "__main__":
    server.run()
