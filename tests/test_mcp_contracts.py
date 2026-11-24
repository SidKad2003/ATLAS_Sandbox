"""Contract tests: one per MCP tool, asserting response shape against the live shop."""
import asyncio

from conftest import REFERENCE_PRODUCT, TEST_RUN_ID

EXPECTED_TOOLS = {
    "shop-mcp": {
        "search_products", "get_product", "get_recommendations", "get_ads", "add_to_cart",
        "view_cart", "get_shipping_quote", "convert_currency", "checkout",
    },
    "store-ops-mcp": {
        "search_catalogue", "get_product", "list_products", "get_reviews", "get_recommendations",
        "cart_get", "cart_add", "get_shipping_quote", "convert_currency", "get_order_status",
        "get_order", "get_order_history", "get_payment_signals", "get_fraud_verdict",
        "hold_order", "release_order",
    },
    "observability-mcp": {
        "query_traces", "query_metrics", "search_logs", "read_flags", "open_incident", "propose_flag_change",
    },
}


def _assert_product(p: dict) -> None:
    assert set(p) >= {"id", "name", "description", "categories", "picture", "price"}
    assert set(p["price"]) == {"currency", "amount"}
    assert isinstance(p["price"]["amount"], float | int)


# ---------------- tool inventory ----------------

async def test_tool_inventory(shop, store_ops, obs):
    for client in (shop, store_ops, obs):
        names = {t["name"] for t in await client.list_tools()}
        assert EXPECTED_TOOLS[client.server_name] <= names, client.server_name


# ---------------- shop-mcp ----------------

async def test_search_products(shop):
    r = await shop.call("search_products", {"query": "telescope"})
    assert r["ok"] and r["count"] == len(r["products"]) and r["count"] > 0
    for p in r["products"]:
        _assert_product(p)


async def test_get_product(shop):
    r = await shop.call("get_product", {"product_id": REFERENCE_PRODUCT})
    assert r["ok"]
    _assert_product(r["product"])
    assert r["product"]["id"] == REFERENCE_PRODUCT


async def test_get_recommendations(shop, session_id):
    r = await shop.call("get_recommendations", {"session_id": session_id, "product_ids": [REFERENCE_PRODUCT]})
    assert r["ok"] and isinstance(r["products"], list)
    for p in r["products"]:
        _assert_product(p)


async def test_get_ads(shop):
    r = await shop.call("get_ads", {"context_keys": ["telescopes"]})
    assert r["ok"] and isinstance(r["ads"], list)
    for a in r["ads"]:
        assert set(a) == {"redirect_url", "text"}


async def test_add_to_cart_and_view_cart(shop, session_id):
    r = await shop.call("add_to_cart", {"session_id": session_id, "product_id": REFERENCE_PRODUCT, "quantity": 2})
    assert r["ok"]
    cart = await shop.call("view_cart", {"session_id": session_id})
    assert cart["ok"]
    assert any(i["product_id"] == REFERENCE_PRODUCT and i["quantity"] == 2 for i in cart["items"])
    for i in cart["items"]:
        assert set(i) == {"product_id", "quantity", "name", "unit_price"}
    assert set(cart["total"]) == {"currency", "amount"} and cart["total"]["amount"] > 0


async def test_get_shipping_quote(shop, session_id, person):
    await shop.call("add_to_cart", {"session_id": session_id, "product_id": REFERENCE_PRODUCT})
    r = await shop.call("get_shipping_quote", {"session_id": session_id, "address": person["address"]})
    assert r["ok"] and set(r["cost"]) == {"currency", "amount"}


async def test_convert_currency(shop):
    r = await shop.call("convert_currency", {"amount": 100.0, "from_currency": "USD", "to_currency": "EUR"})
    assert r["ok"] and r["currency"] == "EUR" and r["amount"] > 0 and r["method"] == "reference-product"


async def test_checkout(shop, session_id, person):
    await shop.call("add_to_cart", {"session_id": session_id, "product_id": REFERENCE_PRODUCT})
    r = await shop.call(
        "checkout",
        {"session_id": session_id, "email": person["email"], "address": person["address"], "credit_card": person["credit_card"]},
    )
    if r["ok"]:
        assert r["order_id"] and set(r["total"]) == {"currency", "amount"}
        assert set(r["shipping_cost"]) == {"currency", "amount"}
        assert isinstance(r["items"], list) and r["items"]
    else:  # an injected fault (for example paymentFailure) is returned as data, not raised
        assert set(r) >= {"ok", "status", "error", "code"}


# ---------------- store-ops-mcp ----------------

async def test_search_catalogue(store_ops):
    r = await store_ops.call("search_catalogue", {"query": "telescope"})
    assert r["ok"] and r["products"]
    for p in r["products"]:
        _assert_product(p)


async def test_store_get_product(store_ops):
    r = await store_ops.call("get_product", {"product_id": REFERENCE_PRODUCT})
    assert r["ok"] and r["product"]["id"] == REFERENCE_PRODUCT


async def test_list_products(store_ops):
    r = await store_ops.call("list_products", {})
    assert r["ok"] and len(r["products"]) >= 10


async def test_get_reviews(store_ops):
    r = await store_ops.call("get_reviews", {"product_id": REFERENCE_PRODUCT})
    assert r["ok"] and isinstance(r["reviews"], list)
    for rev in r["reviews"]:
        assert rev["source"] == "synthetic" and rev["product_id"] == REFERENCE_PRODUCT


async def test_store_get_recommendations(store_ops):
    r = await store_ops.call("get_recommendations", {"user_id": "contract-test", "product_ids": [REFERENCE_PRODUCT]})
    assert r["ok"] and isinstance(r["product_ids"], list)


async def test_cart_add_and_get(store_ops, session_id):
    r = await store_ops.call("cart_add", {"user_id": session_id, "product_id": REFERENCE_PRODUCT, "quantity": 1})
    assert r["ok"]
    c = await store_ops.call("cart_get", {"user_id": session_id})
    assert c["ok"] and any(i["product_id"] == REFERENCE_PRODUCT for i in c["items"])


async def test_store_shipping_quote(store_ops, person):
    r = await store_ops.call(
        "get_shipping_quote", {"items": [{"product_id": REFERENCE_PRODUCT, "quantity": 1}], "address": person["address"]}
    )
    assert r["ok"] and set(r["cost"]) == {"currency", "amount"}


async def test_store_convert_currency(store_ops):
    r = await store_ops.call("convert_currency", {"amount": 10.0, "from_currency": "USD", "to_currency": "EUR"})
    assert r["ok"] and r["currency"] == "EUR" and r["amount"] > 0


async def _place_order(shop, session_id, person) -> str | None:
    await shop.call("add_to_cart", {"session_id": session_id, "product_id": REFERENCE_PRODUCT, "quantity": 1})
    r = await shop.call(
        "checkout",
        {"session_id": session_id, "email": person["email"], "address": person["address"], "credit_card": person["credit_card"]},
    )
    return r["order_id"] if r["ok"] else None


async def test_order_tools(shop, store_ops, session_id, person):
    missing = await store_ops.call("get_order", {"order_id": "no-such-order"})
    assert missing["ok"] and missing["found"] is False
    order_id = await _place_order(shop, session_id, person)
    assert order_id, "checkout failed; is a payment fault active?"
    # accounting writes asynchronously from Kafka, so poll for up to 30s
    o = {"found": False}
    for _ in range(30):
        o = await store_ops.call("get_order", {"order_id": order_id})
        if o.get("found"):
            break
        await asyncio.sleep(1)
    assert o["ok"] and o["found"], "order not in accounting after 30s"
    assert set(o) >= {"items", "items_total_usd", "shipping_address", "hold_status"}
    s = await store_ops.call("get_order_status", {"order_id": order_id})
    assert s["ok"] and s["hold_status"] in {"none", "held", "released"}
    a = o["shipping_address"]
    h = await store_ops.call("get_order_history", {"street_address": a["street_address"], "zip_code": a["zip_code"]})
    assert h["ok"] and any(x["order_id"] == order_id for x in h["orders"])
    sig = await store_ops.call("get_payment_signals", {"order_id": order_id})
    assert sig["ok"] and set(sig["signals"]) >= {
        "card_bin_country", "avs_match", "cvv_match", "velocity_24h", "ip_distance_km", "device_age_days", "email_domain_age_days"
    }
    again = await store_ops.call("get_payment_signals", {"order_id": order_id})
    assert again["signals"] == sig["signals"], "signals must be deterministic"
    v = await store_ops.call("get_fraud_verdict", {"order_id": order_id})
    assert v["ok"] and 0 <= v["score"] <= 1 and v["verdict"] in {"approve", "review", "decline"}
    assert isinstance(v["reasons"], list)


async def test_hold_release_fail_closed(store_ops):
    # Unknown order value fails closed: approval is mandatory and none is given.
    for tool in ("hold_order", "release_order"):
        r = await store_ops.call(
            tool,
            {"order_id": "no-such-order", "reason": "contract test", "requested_by": "contract-test", "run_id": TEST_RUN_ID},
        )
        assert r["ok"] is False and r["code"] == "APPROVAL_REQUIRED"
    bogus = await store_ops.call(
        "hold_order",
        {"order_id": "no-such-order", "reason": "contract test", "requested_by": "contract-test", "run_id": TEST_RUN_ID, "approval_id": 999999999},
    )
    assert bogus["ok"] is False and bogus["code"] == "APPROVAL_INVALID"


# ---------------- observability-mcp ----------------

async def test_query_traces(obs):
    r = await obs.call("query_traces", {"service": "frontend", "error_only": False, "lookback_minutes": 30, "limit": 5})
    assert r["ok"] and isinstance(r["traces"], list)
    for t in r["traces"]:
        assert set(t) >= {"trace_id", "root_operation", "duration_ms", "error_spans", "services"}


async def test_query_metrics(obs):
    r = await obs.call("query_metrics", {"preset": "request_rate_by_service"})
    assert r["ok"] and isinstance(r["results"], list)
    bad = await obs.call("query_metrics", {"preset": "nope"})
    assert bad["ok"] is False and bad["code"] == "INVALID_ARGUMENT"


async def test_search_logs(obs):
    r = await obs.call("search_logs", {"lookback_minutes": 30, "limit": 5})
    assert r["ok"] and isinstance(r["logs"], list)
    for entry in r["logs"]:
        assert set(entry) >= {"timestamp", "service", "severity", "body"}


async def test_read_flags(obs):
    r = await obs.call("read_flags", {})
    assert r["ok"]
    names = {f["flag"] for f in r["flags"]}
    assert {"paymentFailure", "cartFailure", "kafkaQueueProblems"} <= names
    for f in r["flags"]:
        assert set(f) >= {"flag", "default_variant", "variants", "description", "state", "active"}


async def test_open_incident(obs):
    r = await obs.call(
        "open_incident",
        {"run_id": TEST_RUN_ID, "title": "contract test incident", "severity": "low", "evidence": {"note": "test"}, "opened_by": "contract-test"},
    )
    assert r["ok"] and isinstance(r["incident_id"], int)


async def test_propose_flag_change(obs):
    bad = await obs.call(
        "propose_flag_change",
        {"run_id": TEST_RUN_ID, "flag": "noSuchFlag", "variant": "off", "rationale": "test", "requested_by": "contract-test"},
    )
    assert bad["ok"] is False and bad["code"] == "INVALID_ARGUMENT"
    r = await obs.call(
        "propose_flag_change",
        {"run_id": TEST_RUN_ID, "flag": "paymentFailure", "variant": "off", "rationale": "contract test", "requested_by": "contract-test"},
    )
    assert r["ok"] and r["approval_id"] and r["status"] == "pending"
