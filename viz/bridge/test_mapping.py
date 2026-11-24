"""Unit tests for the event -> sprite action mapping (pure functions, no services needed)."""
from viz_bridge.mapping import fault_place, map_event, map_trace, truncate


def ev(kind, atype="shopper", payload=None, location=None):
    return {
        "schema": "agent-event.v1", "event_id": "e1", "run_id": "r1", "seq": 3, "sim_tick": 7,
        "sim_time": "2026-11-24T23:50:00", "wall_time": "x",
        "agent": {"id": "shopper-1", "type": atype, "name": "Ada"},
        "kind": kind, "location": location, "payload": payload or {},
    }


def test_location_hint_wins():
    assert map_event(ev("tool.call", payload={"tool": "checkout"}, location="aisle:books"))["to"] == "aisle:books"


def test_product_aisle_from_result():
    a = map_event(ev("tool.result", payload={"tool": "get_product", "result": {"categories": ["accessories", "telescopes"]}}))
    assert a["action"] == "move" and a["to"] == "aisle:telescopes"


def test_other_aisle():
    a = map_event(ev("tool.call", payload={"tool": "get_product", "args": {"category": "flashlights"}}))
    assert a["to"] == "aisle:other"


def test_search_query_guess():
    a = map_event(ev("tool.call", payload={"tool": "search_products", "args": {"query": "binocular"}}))
    assert a["to"] == "aisle:binoculars"


def test_cart_and_entrance_tools():
    assert map_event(ev("tool.call", payload={"tool": "add_to_cart"}))["to"] == "checkout"
    assert map_event(ev("tool.call", payload={"tool": "get_ads"}))["to"] == "entrance"


def test_chat_is_say_at_help_desk():
    a = map_event(ev("chat.message", payload={"text": "x" * 200}))
    assert a["action"] == "say" and a["to"] == "help-desk" and len(a["text"]) <= 90


def test_roles_go_home():
    assert map_event(ev("tool.call", atype="fraud", payload={"tool": "hold_order"}))["to"] == "fraud-office"
    assert map_event(ev("incident.opened", atype="sre"))["to"] == "ops-room"
    assert map_event(ev("approval.requested", atype="fraud"))["to"] == "approval-desk"


def test_faults():
    a = map_event(ev("fault.injected", atype="conductor", payload={"flag": "paymentFailure", "variant": "50%"}))
    assert a["action"] == "fault" and a["to"] == "checkout" and "paymentFailure" in a["text"]
    assert map_event(ev("fault.cleared", atype="conductor", payload={"flag": "kafkaQueueProblems"}))["action"] == "fault_clear"
    assert fault_place({"flag": "unknownFlag"}) == "ops-room"


def test_status_and_order():
    assert map_event(ev("clock.tick", atype="conductor"))["action"] == "status"
    a = map_event(ev("order.placed", payload={"order_id": "abc"}))
    assert a["to"] == "checkout" and "abc" in a["text"]


def test_truncate():
    assert truncate(None) is None
    assert truncate("short") == "short"


def test_map_trace():
    jaeger = {"data": [{"traceID": "t1", "processes": {"p1": {"serviceName": "checkout"}, "p2": {"serviceName": "shipping"}},
                        "spans": [
                            {"spanID": "s2", "operationName": "oteldemo.ShippingService/ShipOrder", "processID": "p2", "startTime": 2, "tags": []},
                            {"spanID": "s1", "operationName": "oteldemo.CheckoutService/PlaceOrder", "processID": "p1", "startTime": 1, "tags": []},
                            {"spanID": "s3", "operationName": "orders publish", "processID": "p1", "startTime": 3, "tags": [{"key": "messaging.system", "value": "kafka"}]},
                            {"spanID": "s4", "operationName": "irrelevant", "processID": "p1", "startTime": 4, "tags": []},
                        ]}]}
    actions = map_trace(ev("order.placed"), jaeger)
    assert [a["to"] for a in actions] == ["checkout", "shipping-dock", "warehouse"]
    assert actions[0]["event_id"] == map_trace(ev("order.placed"), jaeger)[0]["event_id"]
    assert map_trace(ev("order.placed"), {"data": []}) == []
