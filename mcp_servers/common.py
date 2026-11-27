"""Shared scaffolding for svc-sim MCP servers.

Each server is a FastMCP app over streamable HTTP (stateless, JSON responses) at /mcp, with
GET /healthz, wrapped in OTel ASGI middleware so the caller's traceparent and baggage
(atlas.run_id) continue into the server. Every tool call opens a server span
"execute_tool <name>" carrying the gen_ai.* tool attributes.
"""
import functools
import inspect
import logging
import os
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from starlette.requests import Request
from starlette.responses import JSONResponse

from svc_sim_common.telemetry import (
    GEN_AI_OPERATION,
    GEN_AI_TOOL_NAME,
    instrument_asgi,
    setup_telemetry,
)

log = logging.getLogger("svc_sim.mcp")


def _incoming_headers() -> dict[str, str] | None:
    """Headers of the HTTP request that carried this MCP call, if available."""
    try:
        from mcp.server.lowlevel.server import request_ctx

        req = request_ctx.get().request
        if req is not None and hasattr(req, "headers"):
            return {k.lower(): v for k, v in req.headers.items()}
    except (LookupError, AttributeError):
        pass
    return None


class SvcSimMCP:
    """Thin wrapper holding the FastMCP instance, tracer and server name."""

    def __init__(self, server_name: str, instructions: str) -> None:
        self.server_name = server_name
        self.tracer = setup_telemetry(f"svc-sim-{server_name}")
        # Internal Docker network service: Host headers are container names, so the
        # DNS-rebinding protection (meant for browser-reachable localhost servers) is disabled.
        self.mcp = FastMCP(
            name=server_name,
            instructions=instructions,
            host="0.0.0.0",
            port=8000,
            stateless_http=True,
            json_response=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )

        @self.mcp.custom_route("/healthz", methods=["GET"])
        async def healthz(_: Request) -> JSONResponse:
            return JSONResponse({"status": "ok", "server": server_name})

    def tool(self, description: str | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register an async function as an MCP tool, traced as execute_tool <name>."""

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            name = fn.__name__
            server = self.server_name
            tracer = self.tracer

            @functools.wraps(fn)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                token = None
                if not trace.get_current_span().get_span_context().is_valid:
                    headers = _incoming_headers()
                    if headers:
                        token = otel_context.attach(propagate.extract(headers))
                try:
                    with tracer.start_as_current_span(f"execute_tool {name}", kind=trace.SpanKind.SERVER) as span:
                        span.set_attribute(GEN_AI_OPERATION, "execute_tool")
                        span.set_attribute(GEN_AI_TOOL_NAME, name)
                        span.set_attribute("svc_sim.mcp.server", server)
                        try:
                            return await fn(*args, **kwargs)
                        except Exception as exc:
                            span.record_exception(exc)
                            span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)[:200]))
                            raise
                finally:
                    if token is not None:
                        otel_context.detach(token)

            wrapper.__signature__ = inspect.signature(fn)  # type: ignore[attr-defined]
            self.mcp.tool(name=name, description=description or inspect.getdoc(fn))(wrapper)
            return fn

        return decorator

    def asgi_app(self) -> Any:
        return instrument_asgi(self.mcp.streamable_http_app())

    def run(self) -> None:
        import uvicorn

        uvicorn.run(self.asgi_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), log_level="warning")


def money_to_amount(m: dict[str, Any] | None) -> float:
    """Convert a shop Money object (units + nanos) to a 2dp float."""
    if not m:
        return 0.0
    units = int(m.get("units", 0) or 0)
    nanos = int(m.get("nanos", 0) or 0)
    return round(units + nanos / 1e9, 2)


def money_currency(m: dict[str, Any] | None, default: str = "USD") -> str:
    if not m:
        return default
    return m.get("currencyCode") or m.get("currency_code") or default
