"""MCP client helper (streamable HTTP). Each tool call is wrapped in a gen_ai execute_tool span."""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from opentelemetry import trace

from .telemetry import GEN_AI_OPERATION, GEN_AI_TOOL_CALL_ID, GEN_AI_TOOL_NAME

log = logging.getLogger("svc_sim.mcp_client")
tracer = trace.get_tracer("svc_sim.mcp_client")


class ToolError(RuntimeError):
    pass


def _decode(result: Any) -> Any:
    if getattr(result, "structuredContent", None):
        sc = result.structuredContent
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    texts = [c.text for c in result.content if getattr(c, "type", "") == "text"]
    joined = "\n".join(texts)
    try:
        return json.loads(joined)
    except (json.JSONDecodeError, TypeError):
        return joined


class MCPTools:
    """Connects per call (servers run stateless), which keeps agents simple and restart-safe."""

    def __init__(self, server_name: str, url: str) -> None:
        self.server_name = server_name
        self.url = url.rstrip("/") + "/mcp"
        self._schemas: list[dict[str, Any]] | None = None

    async def list_tools(self) -> list[dict[str, Any]]:
        if self._schemas is None:
            async with streamablehttp_client(self.url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
            self._schemas = [
                {"name": t.name, "description": t.description or "", "input_schema": t.inputSchema, "server": self.server_name}
                for t in tools.tools
            ]
        return self._schemas

    async def call(self, tool: str, args: dict[str, Any] | None = None, call_id: str | None = None) -> Any:
        call_id = call_id or uuid.uuid4().hex[:12]
        with tracer.start_as_current_span(f"execute_tool {tool}", kind=trace.SpanKind.CLIENT) as span:
            span.set_attribute(GEN_AI_OPERATION, "execute_tool")
            span.set_attribute(GEN_AI_TOOL_NAME, tool)
            span.set_attribute(GEN_AI_TOOL_CALL_ID, call_id)
            span.set_attribute("svc_sim.mcp.server", self.server_name)
            async with streamablehttp_client(self.url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool, args or {})
            payload = _decode(result)
            if result.isError:
                span.set_status(trace.Status(trace.StatusCode.ERROR, str(payload)[:200]))
                raise ToolError(f"{tool}: {payload}")
            return payload
