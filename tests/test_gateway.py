"""llm-gateway contract: mock provider chat and embeddings."""
import os

import httpx

GATEWAY = os.environ.get("LLM_GATEWAY_URL", "http://llm-gateway:8000")


async def test_chat_mock():
    body = {
        "tier": "small",
        "agent": {"id": "contract-test", "type": "shopper"},
        "run_id": "contract-test",
        "conversation_id": "contract-test:1",
        "messages": [{"role": "user", "content": "Find me a telescope."}],
        "tools": [
            {"name": "search_products", "description": "Search the catalogue",
             "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}
        ],
        "purpose": "act",
        "state": {},
        "seed": "contract",
        "max_tokens": 128,
    }
    async with httpx.AsyncClient(base_url=GATEWAY, timeout=30) as c:
        r = await c.post("/v1/chat", json=body)
    assert r.status_code == 200, r.text
    d = r.json()
    assert set(d) >= {"content", "tool_calls", "model", "provider", "usage"}
    assert d["provider"] == "mock"
    assert isinstance(d["content"], str) and isinstance(d["tool_calls"], list)
    assert d["content"] or d["tool_calls"]
    for tc in d["tool_calls"]:
        assert set(tc) >= {"id", "name", "arguments"} and isinstance(tc["arguments"], dict)
    assert d["usage"]["input_tokens"] > 0 and d["usage"]["output_tokens"] >= 0


async def test_chat_mock_is_deterministic():
    body = {
        "tier": "small", "agent": {"id": "contract-test", "type": "shopper"}, "run_id": "contract-test",
        "conversation_id": None, "messages": [{"role": "user", "content": "hello"}], "tools": [],
        "purpose": "chat", "state": {}, "seed": "same-seed", "max_tokens": 64,
    }
    async with httpx.AsyncClient(base_url=GATEWAY, timeout=30) as c:
        a = (await c.post("/v1/chat", json=body)).json()
        b = (await c.post("/v1/chat", json=body)).json()
    assert a["content"] == b["content"] and a["tool_calls"] == b["tool_calls"]


async def test_embeddings():
    async with httpx.AsyncClient(base_url=GATEWAY, timeout=30) as c:
        r = await c.post(
            "/v1/embeddings",
            json={"agent": {"id": "contract-test", "type": "shopper"}, "run_id": "contract-test", "input": ["telescope", "telescope"]},
        )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert len(data) == 2 and len(data[0]) == 64 and data[0] == data[1]
    assert all(isinstance(x, float) for x in data[0])
