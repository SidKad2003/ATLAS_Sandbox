# svc-sim architecture

Matches `svc-sim_architecture.excalidraw` (24 Nov 2026), with the differences noted at the end.

```mermaid
flowchart LR
  subgraph AL[Agent layer]
    C[Conductor<br/>deterministic, no LLM]
    S[Shopper agents x N<br/>small tier]
    A[Shopping Assistant<br/>mid tier]
    F[Fraud Review<br/>mid tier]
    R[SRE / Ops<br/>mid tier]
  end
  subgraph AP[Agent platform]
    G[llm-gateway<br/>mock now, Bedrock later]
    SM[shop-mcp]
    SO[store-ops-mcp]
    OM[observability-mcp]
    BUS[(Kafka: agent.events,<br/>agent.handoff, orders)]
  end
  subgraph ST[Storage and human control]
    MEM[(agent-memory<br/>Postgres + pgvector)]
    WM[(agent-working-memory<br/>Valkey)]
    PS[(policy-store)]
    AS[approval-service + UI]
  end
  subgraph SHOP[Astronomy Shop, OTel Demo 3.1.0]
    FP[frontend-proxy] --> FE[frontend]
    FE --> CO[checkout] & PC[product-catalog] & CA[cart] & RE[recommendation] & SH[shipping] & CU[currency] & AD[ad]
    CO --> PA[payment] & EM[email] & K[(kafka orders)]
    K --> FD[fraud-detection] & AC[accounting] --> DB[(astronomy-db)]
    FL[flagd]
  end
  subgraph OBS[Observability and visualisation]
    OC[otel-collector] --> J[jaeger] & P[prometheus] & OS[opensearch]
    VB[sim-viz-bridge] --> VZ[sim-viz<br/>Phaser 3]
    RC[RAIT Connector<br/>disabled]
  end

  C -- tick, directives --> S & A & F & R
  C -- inject_fault / approved changes --> FL
  S -- MCP --> SM -- HTTP as a browser --> FP
  S -- ask_assistant chat --> A
  A -- MCP --> SO
  A -- handoff_to_fraud --> BUS
  F -- MCP --> SO
  BUS -- orders + handoffs --> F
  R -- MCP --> OM --> J & P & OS & FL
  SO -- gRPC --> PC & RE & CA & SH & CU
  SO -- read-only role --> DB
  F & R -- approval requests --> AS
  S & A & F & R --> G
  S & A & F & R --> MEM & WM
  A & F & R --> PS
  S & A & F & R & C -. agent.events .-> BUS
  BUS -. agent.events .-> VB
  VB -. spans .-> J
  AL & AP -. OTLP gen_ai.* rait.* .-> OC
  OC -. disabled .-> RC
```

## Components

| Component | Role | Notes |
|---|---|---|
| Conductor | Sim clock, personas, fault schedule, adversarial scenarios; drives agents in lockstep | Only writer of flagd (scripted faults and human-approved changes) |
| Shoppers | Persona-driven customers using `shop-mcp` and `ask_assistant` | Memory: working, episodic (recency + importance + relevance), semantic (nightly reflection) |
| Shopping Assistant | Answers chats with `store-ops-mcp`; hands suspicious cases to Fraud | Guardrails on tool input (injection) and on replies (policy breaches) |
| Fraud Review | Reviews every order and handoff; holds above threshold need human approval | `store-ops-mcp` also refuses holds without an approved approval id |
| SRE / Ops | Investigates faults and alerts; opens incidents; proposes flag changes | Never changes flags directly |
| llm-gateway | Single path to a model; `gen_ai.*` spans; per-run budget, call cap, rate limit; fails closed | Mock is deterministic and seeded |
| approval-service | Human decisions with identity | Placeholder identity until Keycloak |
| sim-viz-bridge, sim-viz | Read-only projection of `agent.events` and spans; live and replay | The simulation never depends on it |

## Telemetry

Every svc-sim span carries `rait.testbed=svc-sim` and `rait.run_id`. The collector layer adds `rait.testbed=svc-sim` to all upstream telemetry too.
Each Conductor tick is a root trace; agent steps, MCP calls and shop service calls in that tick are children, so one shopper action can be followed from `invoke_agent` through `execute_tool`, `shop-mcp`, `frontend-proxy` and into `checkout`, `payment` and `shipping`.

## Data flow: what leaves the host

Nothing leaves the host during a run. The only outbound data is on-demand backups and evidence to the team's S3 bucket in the same AWS account. Full detail in `docs/DATA-FLOW.md`; security controls and open gaps in `docs/SECURITY.md`.

| Path | State |
|---|---|
| LLM calls | Mock provider, in-process. Bedrock provider disabled (`BEDROCK_ENABLED=false`). |
| RAIT Connector OTLP export | Exporter commented out in `ops/collector/otelcol-config-svc-sim.yml` |
| Backups | `make backup` to `s3://your-s3-bucket/svc-sim/backups/`, on demand |
| policy-store | Local volume. S3 backend available but not enabled |
| UIs | Bound to `127.0.0.1`; reached only through SSM or SSH port-forwarding |
| Outbound at build time only | GitHub (code, upstream), container registries, PyPI |

## Differences from the diagram

- No `product-reviews` service in 3.1.0; `get_reviews` uses synthetic reviews from policy-store.
- The shop database is `astronomy-db` (`astronomy_db`).
- `sim-viz-bridge` stores its event store in `agent-memory` (Postgres), not S3.
- SRE fault triggers arrive as Conductor tick directives (mirrored on `agent.events`) so runs stay reproducible.
