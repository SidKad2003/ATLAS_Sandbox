"""OpenTelemetry setup shared by all svc-sim services.

Every span carries atlas.testbed=svc-sim (resource) and atlas.run_id (span attribute,
taken from the current run context). GenAI semantic convention helpers live here too.
"""
from __future__ import annotations

import contextvars
import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import baggage, context, metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .config import TESTBED

_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("svc_sim_run_id", default=None)
_configured = False

log = logging.getLogger("svc_sim.telemetry")


class _RunIdProcessor(SpanProcessor):
    """Stamps atlas.run_id on every span from the run context or baggage."""

    def on_start(self, span, parent_context=None):  # type: ignore[override]
        run_id = _run_id.get() or baggage.get_baggage("atlas.run_id", parent_context)
        if run_id:
            span.set_attribute("atlas.run_id", str(run_id))
        span.set_attribute("atlas.testbed", TESTBED)
        # The acting agent, propagated as baggage, labels every svc-sim span in its step (incl. MCP servers).
        agent_name = baggage.get_baggage("gen_ai.agent.name", parent_context)
        if agent_name:
            span.set_attribute("gen_ai.agent.name", str(agent_name))

    def on_end(self, span):  # type: ignore[override]
        return None

    def shutdown(self):  # type: ignore[override]
        return None

    def force_flush(self, timeout_millis: int = 30000):  # type: ignore[override]
        return True


def setup_telemetry(service_name: str) -> trace.Tracer:
    global _configured
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not _configured:
        resource = Resource.create(
            {
                "service.name": service_name,
                "service.namespace": TESTBED,
                "atlas.testbed": TESTBED,
                "deployment.environment.name": os.environ.get("SVC_SIM_ENVIRONMENT", "testbed"),
            }
        )
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(_RunIdProcessor())
        readers = []
        if os.environ.get("OTEL_SDK_DISABLED", "false").lower() != "true":
            endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True)))
            readers.append(PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint, insecure=True), export_interval_millis=10000))
        trace.set_tracer_provider(provider)
        metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers))
        try:
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

            HTTPXClientInstrumentor().instrument()
        except Exception:  # pragma: no cover - instrumentation is best effort
            log.warning("httpx instrumentation unavailable")
        _configured = True
    return trace.get_tracer(service_name)


def meter(name: str = "svc_sim") -> metrics.Meter:
    return metrics.get_meter(name)


def set_run_id(run_id: str | None) -> None:
    _run_id.set(run_id)


def current_run_id() -> str | None:
    return _run_id.get()


@contextmanager
def run_context(run_id: str) -> Iterator[None]:
    """Set the run id for spans and propagate it as baggage to downstream services."""
    token_var = _run_id.set(run_id)
    ctx = baggage.set_baggage("atlas.run_id", run_id)
    token_ctx = context.attach(ctx)
    try:
        yield
    finally:
        context.detach(token_ctx)
        _run_id.reset(token_var)


@contextmanager
def agent_context(agent_name: str) -> Iterator[None]:
    """Mark everything done inside this block (spans, downstream calls) as done by this agent."""
    token = context.attach(baggage.set_baggage("gen_ai.agent.name", agent_name))
    try:
        yield
    finally:
        context.detach(token)


@contextmanager
def sim_clock_context(tick: int, sim_time: str) -> Iterator[None]:
    """Propagate the sim clock as baggage so downstream MCP servers stamp their events correctly."""
    ctx = baggage.set_baggage("svc_sim.sim_tick", str(tick))
    ctx = baggage.set_baggage("svc_sim.sim_time", sim_time, context=ctx)
    token = context.attach(ctx)
    try:
        yield
    finally:
        context.detach(token)


def sim_clock_from_baggage() -> tuple[int, str]:
    tick = baggage.get_baggage("svc_sim.sim_tick")
    sim_time = baggage.get_baggage("svc_sim.sim_time")
    try:
        return int(tick) if tick is not None else 0, str(sim_time or "")
    except ValueError:
        return 0, str(sim_time or "")


def instrument_asgi(app: Any) -> Any:
    """Wrap an ASGI app so incoming requests continue the caller's trace."""
    from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware

    return OpenTelemetryMiddleware(app)


def trace_ids() -> dict[str, str]:
    span = trace.get_current_span()
    sc = span.get_span_context()
    if not sc.is_valid:
        return {}
    return {"trace_id": format(sc.trace_id, "032x"), "span_id": format(sc.span_id, "016x")}


# GenAI semantic convention attribute names (OTel semconv gen_ai.*)
GEN_AI_OPERATION = "gen_ai.operation.name"
GEN_AI_PROVIDER = "gen_ai.provider.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_AGENT_NAME = "gen_ai.agent.name"
GEN_AI_AGENT_ID = "gen_ai.agent.id"
GEN_AI_CONVERSATION_ID = "gen_ai.conversation.id"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call.id"
GEN_AI_FINISH_REASONS = "gen_ai.response.finish_reasons"
