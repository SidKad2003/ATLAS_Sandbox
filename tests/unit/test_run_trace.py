"""Per-agent run traces: every step of an agent in a run shares one trace, linked back to its tick."""
import os

os.environ.setdefault("OTEL_SDK_DISABLED", "true")

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

from svc_sim_common.agent_base import BaseAgent, RunState  # noqa: E402


def test_agent_steps_share_one_trace_per_run(monkeypatch):
    agent = BaseAgent("shopper-9", "Amara")
    # a local, recording provider: the global one is disabled in offline and CI runs
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    agent.tracer = provider.get_tracer("svc-sim-test")
    tracer = provider.get_tracer("test")
    run = RunState(run_id="run-1", seed="1", config={})

    with tracer.start_as_current_span("conductor.tick 1"):
        agent.start_run_trace(run)
        with agent.step(run, "invoke_agent Amara"):
            pass
    with tracer.start_as_current_span("conductor.tick 2"):
        with agent.step(run, "invoke_agent Amara"):
            pass
    agent.end_run_trace(run, {"orders": 1})

    spans = exporter.get_finished_spans()
    root = next(s for s in spans if s.name == "agent_run Amara")
    steps = [s for s in spans if s.name == "invoke_agent Amara"]
    ticks = sorted((s for s in spans if s.name.startswith("conductor.tick")), key=lambda s: s.name)

    assert len(steps) == 2
    assert all(s.context.trace_id == root.context.trace_id for s in steps)
    assert all(s.parent is not None and s.parent.span_id == root.context.span_id for s in steps)
    assert root.context.trace_id not in {t.context.trace_id for t in ticks}
    assert [s.links[0].context.span_id for s in steps] == [t.context.span_id for t in ticks]
    assert root.attributes["svc_sim.summary.orders"] == 1
    assert root.attributes["gen_ai.agent.name"] == "Amara"
