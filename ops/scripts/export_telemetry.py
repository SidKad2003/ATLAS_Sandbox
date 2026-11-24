"""Export Jaeger traces and OpenSearch logs to gzipped JSON lines (used by backup.sh).

Runs in the svc-sim/py image on the svc-sim network:  python export_telemetry.py /out
"""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import httpx

JAEGER = "http://jaeger:16686/jaeger/ui/api"
OPENSEARCH = "http://opensearch:9200"


def export_traces(out: Path) -> dict:
    seen: set[str] = set()
    with httpx.Client(timeout=120) as c, gzip.open(out / "jaeger-traces.jsonl.gz", "wt", encoding="utf-8") as f:
        services = c.get(f"{JAEGER}/services").json().get("data") or []
        for svc in sorted(services):
            r = c.get(f"{JAEGER}/traces", params={"service": svc, "lookback": "7d", "limit": 2000})
            if r.status_code != 200:
                continue
            for t in r.json().get("data") or []:
                if t["traceID"] in seen:
                    continue
                seen.add(t["traceID"])
                f.write(json.dumps(t, separators=(",", ":")) + "\n")
    return {"services": len(services), "traces": len(seen)}


def export_logs(out: Path) -> dict:
    n = 0
    with httpx.Client(timeout=120) as c, gzip.open(out / "opensearch-logs.jsonl.gz", "wt", encoding="utf-8") as f:
        r = c.post(f"{OPENSEARCH}/otel-logs-*/_search", params={"scroll": "2m"}, json={"size": 2000, "query": {"match_all": {}}})
        if r.status_code != 200:
            return {"logs": 0, "note": f"opensearch returned {r.status_code}"}
        d = r.json()
        while True:
            hits = d.get("hits", {}).get("hits", [])
            if not hits:
                break
            for h in hits:
                f.write(json.dumps(h.get("_source", {}), separators=(",", ":")) + "\n")
                n += 1
            d = c.post(f"{OPENSEARCH}/_search/scroll", json={"scroll": "2m", "scroll_id": d["_scroll_id"]}).json()
    return {"logs": n}


if __name__ == "__main__":
    out = Path(sys.argv[1])
    result = {}
    for name, fn in (("jaeger", export_traces), ("opensearch", export_logs)):
        try:
            result[name] = fn(out)
        except Exception as exc:  # best effort: a failed export is recorded, not fatal
            result[name] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    (out / "telemetry-export.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))
