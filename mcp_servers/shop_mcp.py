"""shop-mcp: the Astronomy Shop as a customer sees it.

Every tool goes through frontend-proxy to the frontend's HTTP API, the same path a browser
takes (verified against OpenTelemetry Demo 3.1.0 src/frontend/pages/api). httpx is
instrumented, so the shopper agent's trace continues through frontend into the shop services.
Shop errors (including injected faults) are returned as data, never raised, so agents can react.
"""
import json
import os
from typing import Any

import httpx

from mcp_servers.common import SvcSimMCP, money_currency, money_to_amount

FRONTEND = os.environ.get("FRONTEND_PROXY_URL", "http://frontend-proxy:8080").rstrip("/")
REFERENCE_PRODUCT_ID = os.environ.get("SHOP_MCP_REFERENCE_PRODUCT", "OLJCESPC7Z")

server = SvcSimMCP(
    "shop-mcp",
    "Customer-facing tools for the Astronomy Shop. Acts exactly like a browser through frontend-proxy.",
)
_http = httpx.AsyncClient(base_url=FRONTEND, timeout=30, headers={"User-Agent": "svc-sim-shop-mcp"})


def normalise_product(p: dict[str, Any]) -> dict[str, Any]:
    price = p.get("priceUsd") or p.get("price_usd") or {}
    return {
        "id": p.get("id", ""),
        "name": p.get("name", ""),
        "description": p.get("description", ""),
        "categories": list(p.get("categories") or []),
        "picture": p.get("picture", ""),
        "price": {"currency": money_currency(price), "amount": money_to_amount(price)},
    }


def _error(r: httpx.Response) -> dict[str, Any]:
    try:
        body = r.json()
    except ValueError:
        body = {"error": r.text[:300]}
    if not isinstance(body, dict):
        body = {"error": str(body)[:300]}
    return {"ok": False, "status": r.status_code, "error": body.get("error", "shop error"), "code": body.get("code")}


async def _get(path: str, params: Any = None) -> httpx.Response:
    return await _http.get(path, params=params)


async def _list_products(currency: str) -> list[dict[str, Any]]:
    r = await _get("/api/products", {"currencyCode": currency})
    r.raise_for_status()
    return [normalise_product(p) for p in r.json()]


@server.tool()
async def search_products(query: str, currency: str = "USD") -> dict[str, Any]:
    """Search the catalogue. The frontend has no search endpoint, so this lists all products
    and filters on name, description and categories, as a browser search box would."""
    try:
        products = await _list_products(currency)
    except httpx.HTTPStatusError as exc:
        return _error(exc.response)
    terms = [t for t in query.lower().split() if t]
    if terms:
        def hit(p: dict[str, Any]) -> bool:
            text = " ".join([p["name"], p["description"], " ".join(p["categories"])]).lower()
            return all(t in text for t in terms) or any(t in [c.lower() for c in p["categories"]] for t in terms)

        products = [p for p in products if hit(p)]
    return {"ok": True, "query": query, "count": len(products), "products": products}


@server.tool()
async def get_product(product_id: str, currency: str = "USD") -> dict[str, Any]:
    """Get one product (GET /api/products/{id})."""
    r = await _get(f"/api/products/{product_id}", {"currencyCode": currency})
    if r.status_code != 200:
        return _error(r)
    return {"ok": True, "product": normalise_product(r.json())}


@server.tool()
async def get_recommendations(session_id: str, product_ids: list[str], currency: str = "USD") -> dict[str, Any]:
    """Recommended products for the session, given products being viewed."""
    params: list[tuple[str, str]] = [("sessionId", session_id), ("currencyCode", currency)]
    params += [("productIds", pid) for pid in product_ids]
    r = await _get("/api/recommendations", params)
    if r.status_code != 200:
        return _error(r)
    return {"ok": True, "products": [normalise_product(p) for p in r.json()]}


@server.tool()
async def get_ads(context_keys: list[str]) -> dict[str, Any]:
    """Ads for the given context keys (usually product categories)."""
    r = await _get("/api/data", {"contextKeys": ",".join(context_keys)})
    if r.status_code != 200:
        return _error(r)
    ads = [{"redirect_url": a.get("redirectUrl", ""), "text": a.get("text", "")} for a in r.json()]
    return {"ok": True, "ads": ads}


@server.tool()
async def add_to_cart(session_id: str, product_id: str, quantity: int = 1) -> dict[str, Any]:
    """Add a product to the session's cart (POST /api/cart)."""
    r = await _http.post("/api/cart", json={"userId": session_id, "item": {"productId": product_id, "quantity": quantity}})
    if r.status_code != 200:
        return _error(r)
    return {"ok": True, "session_id": session_id, "product_id": product_id, "quantity": quantity}


async def _cart(session_id: str, currency: str) -> dict[str, Any]:
    r = await _get("/api/cart", {"sessionId": session_id, "currencyCode": currency})
    if r.status_code != 200:
        return _error(r)
    items = []
    total = 0.0
    for it in r.json().get("items", []):
        p = normalise_product(it.get("product") or {})
        qty = int(it.get("quantity", 0))
        items.append({"product_id": it.get("productId", p["id"]), "quantity": qty, "name": p["name"], "unit_price": p["price"]["amount"]})
        total += p["price"]["amount"] * qty
    return {"ok": True, "items": items, "total": {"currency": currency, "amount": round(total, 2)}}


@server.tool()
async def view_cart(session_id: str, currency: str = "USD") -> dict[str, Any]:
    """View the session's cart with line prices and total."""
    return await _cart(session_id, currency)


def _address(a: dict[str, Any]) -> dict[str, Any]:
    return {
        "streetAddress": a.get("streetAddress") or a.get("street_address", ""),
        "city": a.get("city", ""),
        "state": a.get("state", ""),
        "country": a.get("country", ""),
        "zipCode": str(a.get("zipCode") or a.get("zip_code", "")),
    }


@server.tool()
async def get_shipping_quote(session_id: str, address: dict, currency: str = "USD") -> dict[str, Any]:
    """Shipping quote for the current cart to an address (GET /api/shipping)."""
    cart = await _cart(session_id, currency)
    if not cart.get("ok"):
        return cart
    item_list = [{"productId": i["product_id"], "quantity": i["quantity"]} for i in cart["items"]]
    r = await _get(
        "/api/shipping",
        {"itemList": json.dumps(item_list), "currencyCode": currency, "address": json.dumps(_address(address))},
    )
    if r.status_code != 200:
        return _error(r)
    m = r.json()
    return {"ok": True, "cost": {"currency": money_currency(m, currency), "amount": money_to_amount(m)}}


@server.tool()
async def convert_currency(amount: float, from_currency: str, to_currency: str) -> dict[str, Any]:
    """Convert an amount. The frontend exposes no conversion endpoint, so the rate is derived
    from a reference product fetched in both currencies (the shop converts prices server-side)."""
    if from_currency.upper() == to_currency.upper():
        return {"ok": True, "amount": round(amount, 2), "currency": to_currency.upper(), "rate": 1.0, "method": "identity"}
    a = await _get(f"/api/products/{REFERENCE_PRODUCT_ID}", {"currencyCode": from_currency.upper()})
    b = await _get(f"/api/products/{REFERENCE_PRODUCT_ID}", {"currencyCode": to_currency.upper()})
    if a.status_code != 200:
        return _error(a)
    if b.status_code != 200:
        return _error(b)
    pa = money_to_amount(a.json().get("priceUsd"))
    pb = money_to_amount(b.json().get("priceUsd"))
    if pa <= 0:
        return {"ok": False, "status": 500, "error": "reference price unavailable", "code": None}
    rate = pb / pa
    return {
        "ok": True,
        "amount": round(amount * rate, 2),
        "currency": to_currency.upper(),
        "rate": round(rate, 6),
        "method": "reference-product",
    }


@server.tool()
async def checkout(session_id: str, email: str, address: dict, credit_card: dict, currency: str = "USD") -> dict[str, Any]:
    """Place the order for the session's cart (POST /api/checkout). Returns ok=false with the
    shop's status and error on failure (for example an injected payment fault)."""
    card = {
        "creditCardNumber": credit_card.get("creditCardNumber") or credit_card.get("number", ""),
        "creditCardExpirationMonth": int(credit_card.get("creditCardExpirationMonth") or credit_card.get("expiration_month", 1)),
        "creditCardExpirationYear": int(credit_card.get("creditCardExpirationYear") or credit_card.get("expiration_year", 2039)),
        "creditCardCvv": int(credit_card.get("creditCardCvv") or credit_card.get("cvv", 0)),
    }
    body = {"userId": session_id, "email": email, "address": _address(address), "userCurrency": currency, "creditCard": card}
    r = await _http.post("/api/checkout", params={"currencyCode": currency}, json=body)
    if r.status_code != 200:
        return _error(r)
    d = r.json()
    items = []
    total = 0.0
    for it in d.get("items", []):
        item = it.get("item") or {}
        cost = money_to_amount(it.get("cost"))
        qty = int(item.get("quantity", 0))
        items.append({"product_id": item.get("productId", ""), "quantity": qty, "name": (item.get("product") or {}).get("name", ""), "cost": cost})
        total += cost * qty
    ship = d.get("shippingCost") or {}
    shipping = money_to_amount(ship)
    return {
        "ok": True,
        "order_id": d.get("orderId", ""),
        "shipping_tracking_id": d.get("shippingTrackingId", ""),
        "shipping_cost": {"currency": money_currency(ship, currency), "amount": shipping},
        "items": items,
        "total": {"currency": currency, "amount": round(total + shipping, 2)},
    }


if __name__ == "__main__":
    server.run()
