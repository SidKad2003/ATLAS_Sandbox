"""observability-mcp: read-only view of the shop's telemetry and flags for the SRE / Ops agent.

Reads Jaeger (traces), Prometheus (metrics), OpenSearch (logs) and the flagd flag file.
The only writes are incidents (agent-memory) and flag-change PROPOSALS sent to
approval-service. This server never changes a flag.
Names verified against OpenTelemetry Demo 3.1.0: spanmetrics series traces_span_metrics_*
(labels service_name, status_code), logs index otel-logs-<date> with fields
resource.service.name, severity.text, body, observedTimestamp.
"""
import json
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from mcp_servers.common import SvcSimMCP
from svc_sim_common.events import EventPublisher
from svc_sim_common.memory import db as agent_memory

server = SvcSimMCP(
    "observability-mcp",
    "Read-only observability tools (traces, metrics, logs, flags). Flag changes are proposals that need human approval.",
)

JAEGER_URL = os.environ.get("JAEGER_URL", "http://jaeger:16686/jaeger/ui").rstrip("/")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
OPENSEARCH_URL = os.environ.get("OPENSEARCH_URL", "http://opensearch:9200").rstrip("/")
FLAGD_FILE = os.environ.get("FLAGD_FILE", "/flags/demo.flagd.json")
APPROVAL_URL = os.environ.get("APPROVAL_SERVICE_URL", "http://approval-service:8000").rstrip("/")

events = EventPublisher("observability-mcp", "mcp-server", "observability-mcp")
_http = httpx.AsyncClient(timeout=20)

PRESETS = {
    "error_rate_by_service": (
        'sum by (service_name) (rate(traces_span_metrics_calls_total{status_code="STATUS_CODE_ERROR"}[2m]))'
        " / sum by (service_name) (rate(traces_span_metrics_calls_total[2m]))"
    ),
    "request_rate_by_service": "sum by (service_name) (rate(traces_span_metrics_calls_total[2m]))",
    "latency_p95_by_service": (
        "histogram_quantile(0.95, sum by (service_name, le) (rate(traces_span_metrics_duration_milliseconds_bucket[2m])))"
    ),
    # kafkametrics receiver (compose.full); exported name may vary, so this is best effort.
    "kafka_consumer_lag": "sum by (group, topic) (kafka_consumer_group_lag)",
}


def _service_of(trace_obj: dict[str, Any], process_id: str) -> str:
    return (trace_obj.get("processes", {}).get(process_id) or {}).get("serviceName", "")


def _is_error(span: dict[str, Any]) -> bool:
    for t in span.get("tags", []):
        if t.get("key") == "error" and str(t.get("value")).lower() == "true":
            return True
        if t.get("key") == "otel.status_code" and t.get("value") == "ERROR":
            return True
    return False


@server.tool()
async def query_traces(service: str, error_only: bool = True, lookback_minutes: int = 15, limit: int = 20) -> dict[str, Any]:
    """Recent traces for a service from Jaeger, summarised (trace id, root operation,
    duration, error span names, services involved)."""
    params: dict[str, Any] = {"service": service, "lookback": f"{int(lookback_minutes)}m", "limit": int(limit)}
    if error_only:
        params["tags"] = json.dumps({"error": "true"})
    try:
        r = await _http.get(f"{JAEGER_URL}/api/traces", params=params)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"jaeger query failed: {exc}", "code": "UNAVAILABLE"}
    out = []
    for t in r.json().get("data") or []:
        spans = t.get("spans", [])
        if not spans:
            continue
        root = min(spans, key=lambda s: s.get("startTime", 0))
        start = min(s.get("startTime", 0) for s in spans)
        end = max(s.get("startTime", 0) + s.get("duration", 0) for s in spans)
        out.append(
            {
                "trace_id": t.get("traceID", ""),
                "root_operation": root.get("operationName", ""),
                "root_service": _service_of(t, root.get("processID", "")),
                "duration_ms": round((end - start) / 1000, 1),
                "span_count": len(spans),
                "error_spans": sorted({f"{_service_of(t, s.get('processID', ''))}:{s.get('operationName', '')}" for s in spans if _is_error(s)})[:10],
                "services": sorted({_service_of(t, s.get("processID", "")) for s in spans}),
            }
        )
    return {"ok": True, "service": service, "count": len(out), "traces": out}


@server.tool()
async def query_metrics(query: str | None = None, preset: str | None = None) -> dict[str, Any]:
    """Instant PromQL query against Prometheus. Either a raw query or a preset:
    error_rate_by_service, request_rate_by_service, latency_p95_by_service, kafka_consumer_lag."""
    if preset:
        if preset not in PRESETS:
            return {"ok": False, "error": f"unknown preset {preset}", "code": "INVALID_ARGUMENT", "presets": sorted(PRESETS)}
        promql = PRESETS[preset]
    elif query:
        promql = query
    else:
        return {"ok": False, "error": "query or preset is required", "code": "INVALID_ARGUMENT", "presets": sorted(PRESETS)}
    try:
        r = await _http.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": promql})
        r.raise_for_status()
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"prometheus query failed: {exc}", "code": "UNAVAILABLE"}
    data = r.json().get("data", {})
    results = []
    for row in data.get("result", []):
        value = row.get("value", [None, None])[1]
        try:
            num = float(value)
            num = None if num != num else round(num, 6)  # NaN to None
        except (TypeError, ValueError):
            num = None
        results.append({"labels": row.get("metric", {}), "value": num})
    return {"ok": True, "query": promql, "preset": preset, "result_type": data.get("resultType"), "results": results}


@server.tool()
async def search_logs(
    query: str = "", service: str | None = None, severity: str | None = None, lookback_minutes: int = 15, limit: int = 20
) -> dict[str, Any]:
    """Search recent logs in OpenSearch (index otel-logs-*), newest first."""
    must: list[dict[str, Any]] = [{"range": {"observedTimestamp": {"gte": f"now-{int(lookback_minutes)}m"}}}]
    if query:
        must.append({"simple_query_string": {"query": query, "fields": ["body", "body.*"], "lenient": True}})
    if service:
        must.append({"match_phrase": {"resource.service.name": service}})
    if severity:
        must.append({"match": {"severity.text": {"query": severity, "lenient": True}}})
    body = {"size": int(limit), "sort": [{"observedTimestamp": {"order": "desc", "unmapped_type": "date"}}], "query": {"bool": {"must": must}}}
    try:
        r = await _http.post(f"{OPENSEARCH_URL}/otel-logs-*/_search", json=body, params={"ignore_unavailable": "true"})
        r.raise_for_status()
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"opensearch query failed: {exc}", "code": "UNAVAILABLE"}
    hits = []
    for h in r.json().get("hits", {}).get("hits", []):
        src = h.get("_source", {})
        res = src.get("resource") or {}
        svc = res.get("service.name") or (res.get("service") or {}).get("name") if isinstance(res, dict) else None
        sev = src.get("severity") or {}
        sev_text = sev.get("text") if isinstance(sev, dict) else src.get("severityText")
        msg = src.get("body")
        if isinstance(msg, (dict, list)):
            msg = json.dumps(msg)[:500]
        hits.append(
            {
                "timestamp": src.get("observedTimestamp") or src.get("@timestamp"),
                "service": svc,
                "severity": sev_text,
                "body": (msg or "")[:500],
                "trace_id": src.get("traceId") or src.get("trace_id"),
            }
        )
    return {"ok": True, "count": len(hits), "logs": hits}


def _load_flags() -> dict[str, Any]:
    with open(FLAGD_FILE, encoding="utf-8") as fh:
        return json.load(fh).get("flags", {})


def _off_value(variants: dict[str, Any]) -> Any:
    if "off" in variants:
        return variants["off"]
    return False


@server.tool()
async def read_flags() -> dict[str, Any]:
    """Current flagd flags (fault toggles). active=true when the default variant is not off, or when a
    targeting rule (for example productCatalogFailure's product match) can resolve to a non-off variant."""
    try:
        flags = _load_flags()
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"flag file unreadable: {exc}", "code": "UNAVAILABLE"}
    out = []
    for name in sorted(flags):
        f = flags[name]
        variants = f.get("variants", {})
        default = f.get("defaultVariant")
        value = variants.get(default)
        active = default != "off" and value not in (False, 0, None, _off_value(variants))
        rule = (f.get("targeting") or {}).get("if")
        if isinstance(rule, list):
            branch_variants = [v for v in rule[1:] if isinstance(v, str)]
            if any(v != "off" and variants.get(v) not in (False, 0, None, _off_value(variants)) for v in branch_variants):
                active = True
                value = next((variants.get(v) for v in branch_variants if v != "off"), value)
        out.append(
            {
                "flag": name,
                "default_variant": default,
                "value": value,
                "variants": variants,
                "description": f.get("description", ""),
                "state": f.get("state", ""),
                "has_targeting": "targeting" in f,
                "active": bool(active),
            }
        )
    return {"ok": True, "flags": out, "active": [f["flag"] for f in out if f["active"]]}


@server.tool()
async def open_incident(run_id: str, title: str, severity: str, evidence: dict, opened_by: str) -> dict[str, Any]:
    """Open an incident record in agent-memory and announce it on agent.events."""
    pool = await agent_memory()
    sim_time = (evidence or {}).get("sim_time")
    try:
        ts = datetime.fromisoformat(sim_time) if sim_time else datetime.now(timezone.utc).replace(tzinfo=None)
    except ValueError:
        ts = datetime.now(timezone.utc).replace(tzinfo=None)
    incident_id = await pool.fetchval(
        """INSERT INTO incidents (run_id, opened_by, sim_time, title, severity, evidence)
           VALUES ($1,$2,$3,$4,$5,$6::jsonb) RETURNING id""",
        run_id, opened_by, ts, title, severity, json.dumps(evidence or {}, default=str),
    )
    await events.emit(
        run_id,
        "incident.opened",
        {"incident_id": incident_id, "title": title, "severity": severity, "opened_by": opened_by},
        location="ops-room",
    )
    return {"ok": True, "incident_id": incident_id, "status": "open"}


@server.tool()
async def propose_flag_change(run_id: str, flag: str, variant: str, rationale: str, requested_by: str) -> dict[str, Any]:
    """Propose a flag change. Creates a human approval request; never changes the flag."""
    try:
        flags = _load_flags()
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"flag file unreadable: {exc}", "code": "UNAVAILABLE"}
    if flag not in flags:
        return {"ok": False, "error": f"unknown flag {flag}", "code": "INVALID_ARGUMENT"}
    variants = flags[flag].get("variants", {})
    if variant not in variants:
        return {"ok": False, "error": f"unknown variant {variant} for {flag}", "code": "INVALID_ARGUMENT", "variants": sorted(variants)}
    body = {
        "run_id": run_id,
        "kind": "flag_change",
        "requested_by": requested_by,
        "subject": flag,
        "request": {"flag": flag, "variant": variant, "current_variant": flags[flag].get("defaultVariant")},
        "rationale": rationale,
    }
    try:
        r = await _http.post(f"{APPROVAL_URL}/api/approvals", json=body)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"approval-service unavailable: {exc}", "code": "UNAVAILABLE"}
    d = r.json()
    return {"ok": True, "approval_id": d.get("id"), "status": d.get("status", "pending"), "flag": flag, "variant": variant}


if __name__ == "__main__":
    server.run()
