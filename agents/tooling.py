"""Helpers shared by the LLM agents: tool catalogues, place mapping, safe summaries."""
from __future__ import annotations

import json
from typing import Any

from svc_sim_common.mcp_client import MCPTools

MAIN_AISLES = ("telescopes", "binoculars", "accessories", "books")


def aisle_for(categories: list[str] | str | None) -> str:
    if isinstance(categories, str):
        categories = [c.strip() for c in categories.split(",")]
    for c in categories or []:
        if c in MAIN_AISLES:
            return f"aisle:{c}"
    return "aisle:other"


def summarise(result: Any, limit: int = 240) -> str:
    text = result if isinstance(result, str) else json.dumps(result, default=str)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def local_tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": properties, "required": required}}


async def tool_catalogue(server: MCPTools, allow: set[str] | None = None) -> list[dict[str, Any]]:
    tools = await server.list_tools()
    return [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
            for t in tools if allow is None or t["name"] in allow]


def assistant_tool_message(tool_calls: list[Any], content: str = "") -> dict[str, Any]:
    return {"role": "assistant", "content": content,
            "tool_calls": [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in tool_calls]}


def tool_result_message(call_id: str, result: Any) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": summarise(result, 2000)}
