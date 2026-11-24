"""approval-service: human approval queue for order holds/releases, flag changes and escalations.

Every decision records the human's identity. Until Keycloak (via oauth2-proxy) is wired in, the
identity is a placeholder taken from the X-Forwarded-User header or the form, prefixed "unverified:".
"""
from __future__ import annotations

import html
import json
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from svc_sim_common.events import EventPublisher
from svc_sim_common.memory import db
from svc_sim_common.telemetry import instrument_asgi, run_context, setup_telemetry

setup_telemetry("svc-sim-approval-service")
events = EventPublisher("approval-service", "approval-service", "Approval desk")
api = FastAPI(title="svc-sim approval-service")

KINDS = {"hold_order", "release_order", "flag_change", "escalation"}


class ApprovalIn(BaseModel):
    run_id: str
    kind: str
    requested_by: str
    subject: str
    request: dict[str, Any] = {}
    rationale: str


class Decision(BaseModel):
    decision: str
    operator: str = "operator"
    note: str = ""


def _row(r: Any) -> dict[str, Any]:
    d = dict(r)
    if isinstance(d.get("request"), str):
        d["request"] = json.loads(d["request"])
    for k in ("created_at", "decided_at", "applied_at"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    return d


def _identity(request: Request, supplied: str) -> str:
    # Placeholder until Keycloak: oauth2-proxy will set X-Forwarded-User / X-Forwarded-Email.
    who = request.headers.get("x-forwarded-email") or request.headers.get("x-forwarded-user")
    return who if who else f"unverified:{(supplied or 'operator').strip()[:64]}"


@api.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@api.post("/api/approvals")
async def create(a: ApprovalIn) -> dict[str, Any]:
    if a.kind not in KINDS:
        raise HTTPException(400, f"kind must be one of {sorted(KINDS)}")
    pool = await db()
    existing = await pool.fetchrow(
        "SELECT * FROM approvals WHERE run_id=$1 AND kind=$2 AND subject=$3 AND status='pending'", a.run_id, a.kind, a.subject
    )
    if existing:
        return _row(existing)
    row = await pool.fetchrow(
        """INSERT INTO approvals (run_id, kind, requested_by, subject, request, rationale)
           VALUES ($1,$2,$3,$4,$5::jsonb,$6) RETURNING *""",
        a.run_id, a.kind, a.requested_by, a.subject, json.dumps(a.request), a.rationale,
    )
    with run_context(a.run_id):
        await events.emit(a.run_id, "approval.requested",
                          {"approval_id": row["id"], "kind": a.kind, "subject": a.subject, "requested_by": a.requested_by,
                           "rationale": a.rationale[:200]}, location="approval-desk")
    return _row(row)


@api.get("/api/approvals")
async def list_(status: str | None = None, kind: str | None = None, run_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    pool = await db()
    rows = await pool.fetch(
        """SELECT * FROM approvals WHERE ($1::text IS NULL OR status=$1) AND ($2::text IS NULL OR kind=$2)
           AND ($3::text IS NULL OR run_id=$3) ORDER BY id DESC LIMIT $4""",
        status, kind, run_id, limit,
    )
    return [_row(r) for r in rows]


@api.get("/api/approvals/{approval_id}")
async def get(approval_id: int) -> dict[str, Any]:
    pool = await db()
    row = await pool.fetchrow("SELECT * FROM approvals WHERE id=$1", approval_id)
    if not row:
        raise HTTPException(404, "not found")
    return _row(row)


async def _decide(approval_id: int, decision: str, operator: str, note: str) -> dict[str, Any]:
    if decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve or reject")
    status = "approved" if decision == "approve" else "rejected"
    pool = await db()
    row = await pool.fetchrow(
        """UPDATE approvals SET status=$2, decided_by=$3, decided_at=now(), decision_note=$4
           WHERE id=$1 AND status='pending' RETURNING *""",
        approval_id, status, operator, note[:500],
    )
    if not row:
        raise HTTPException(409, "approval is not pending")
    human = EventPublisher(operator, "human", operator)
    with run_context(row["run_id"]):
        await human.emit(row["run_id"], "approval.decided",
                         {"approval_id": approval_id, "kind": row["kind"], "subject": row["subject"], "decision": status,
                          "operator": operator, "note": note[:200]}, location="approval-desk")
    return _row(row)


@api.post("/api/approvals/{approval_id}/decision")
async def decide(approval_id: int, d: Decision, request: Request) -> dict[str, Any]:
    return await _decide(approval_id, d.decision, _identity(request, d.operator), d.note)


@api.post("/api/approvals/{approval_id}/applied")
async def applied(approval_id: int) -> dict[str, Any]:
    pool = await db()
    row = await pool.fetchrow(
        "UPDATE approvals SET status='applied', applied_at=now() WHERE id=$1 AND status='approved' RETURNING *", approval_id
    )
    if not row:
        raise HTTPException(409, "approval is not in approved state")
    return _row(row)


# ---------------- minimal UI ----------------
PAGE = """<!doctype html><html lang="en-GB"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>svc-sim approvals</title><style>
body{{font-family:system-ui,sans-serif;margin:0;background:#f6f7f9;color:#1b1f24}}header{{background:#1f3a5f;color:#fff;padding:12px 16px}}
main{{padding:16px;max-width:1100px;margin:auto}}table{{width:100%;border-collapse:collapse;background:#fff}}
th,td{{padding:8px;border-bottom:1px solid #e3e6ea;text-align:left;vertical-align:top;font-size:14px}}
.pending{{color:#9a6700;font-weight:600}}.approved,.applied{{color:#1a7f37}}.rejected{{color:#cf222e}}
form{{display:inline}}input[type=text]{{width:140px}}button{{margin:2px;padding:4px 10px}}code{{font-size:12px}}
.note{{background:#fff8c5;padding:8px 12px;border:1px solid #e3d27a;margin-bottom:12px;font-size:14px}}
</style></head><body><header><strong>svc-sim approval desk</strong> &middot; synthetic data only</header><main>
<div class="note">Identity is a placeholder until Keycloak is connected. Decisions are recorded as <code>unverified:&lt;name&gt;</code>.</div>
<p><a href="/?status=pending">Pending</a> &middot; <a href="/">All</a></p>
<table><tr><th>#</th><th>Kind</th><th>Subject</th><th>Requested by</th><th>Rationale</th><th>Status</th><th>Decision</th></tr>{rows}</table>
</main></body></html>"""


@api.get("/", response_class=HTMLResponse)
async def ui(status: str | None = None) -> str:
    items = await list_(status=status)
    rows = []
    for a in items:
        e = html.escape
        action = ""
        if a["status"] == "pending":
            action = (f'<form method="post" action="/ui/{a["id"]}"><input type="text" name="operator" placeholder="your name" required>'
                      f'<input type="text" name="note" placeholder="note"><button name="decision" value="approve">Approve</button>'
                      f'<button name="decision" value="reject">Reject</button></form>')
        else:
            action = e(f"{a.get('decided_by') or ''} {a.get('decided_at') or ''}")
        rows.append(f"<tr><td>{a['id']}</td><td>{e(a['kind'])}</td><td><code>{e(a['subject'])}</code><br><code>{e(json.dumps(a['request']))[:160]}</code></td>"
                    f"<td>{e(a['requested_by'])}<br><small>{e(a['run_id'][:8])}</small></td><td>{e(a['rationale'])}</td>"
                    f"<td class='{e(a['status'])}'>{e(a['status'])}</td><td>{action}</td></tr>")
    return PAGE.format(rows="".join(rows) or "<tr><td colspan=7>No approvals.</td></tr>")


@api.post("/ui/{approval_id}")
async def ui_decide(approval_id: int, request: Request, decision: str = Form(...), operator: str = Form(...), note: str = Form("")):
    await _decide(approval_id, decision, _identity(request, operator), note)
    return RedirectResponse("/?status=pending", status_code=303)


app = instrument_asgi(api)
