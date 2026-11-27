# Reading svc-sim agent telemetry

Open the UIs through an SSH tunnel (see RUNBOOK): Grafana at http://localhost:8080/grafana, Jaeger at http://localhost:8080/jaeger/ui. Get run ids from `make run-status` or the sim-viz run list.

## 1. Start in Grafana: "svc-sim agents" dashboard

Grafana → Dashboards → Demo → **svc-sim agents**. Choose a **Run**, optionally an **Agent**, and set the time range to cover the run.

| Panel | Question it answers |
|---|---|
| Top row | How many orders, chats, fraud handoffs, human approvals, incidents, guardrail flags, failed tool calls and LLM tokens in this run |
| Agent actions per minute | When each agent was busy |
| What each agent did | Every action kind per agent (tool calls, chats, handoffs, memory writes, plans, reflections). Click an agent to open its traces in Jaeger |
| Tool calls per agent | Which tools each agent used, and how many failed |
| LLM tokens by agent, LLM latency p95 | Cost and speed of each agent's model calls |
| Tool call latency p95 | How long each agent waited on each tool (MCP server plus the shop behind it) |
| Run timeline | One trace per sim-clock tick; click a tick to see every agent's step in that minute |

## 2. Then Jaeger: one agent's story, step by step

Each agent has **one trace per run**, named `agent_run <name>` (for example `agent_run Amara`). Open it and the waterfall shows everything that agent did in that run, in time order: A, then B, then C.

| To see | Service | Operation |
|---|---|---|
| Amara's whole run | `svc-sim-shopper` | `agent_run Amara` |
| Shopping Assistant's whole run (every reply, each linked to the shopper who asked) | `svc-sim-assistant` | `agent_run Shopping Assistant` |
| Fraud Review's whole run (each `review_case` links to the assistant's handoff) | `svc-sim-fraud` | `agent_run Fraud Review` |
| SRE / Ops's whole run | `svc-sim-sre` | `agent_run SRE / Ops` |
| Everything in one sim minute, across agents | `svc-sim-conductor` | `conductor.tick N` |

Inside an agent's run trace:

```
agent_run Amara                                   the whole run (summary on the span: orders, chats)
├─ invoke_agent Amara   (tick 3)                  one step; "References" link to the Conductor tick
│  ├─ chat svc-sim-mock-small-v1                  the model decided what to do
│  └─ execute_tool search_products → shop-mcp → frontend-proxy → frontend → product-catalog
├─ invoke_agent Amara   (tick 5)
│  └─ execute_tool ask_assistant → POST /chat     a message to the Shopping Assistant (its reply is in the assistant's trace, linked)
├─ invoke_agent Amara   (tick 9)
│  └─ execute_tool add_to_cart → ... → cart
└─ invoke_agent Amara   (tick 11)
   └─ execute_tool checkout → ... → checkout, payment, shipping, email
```

The links between traces ("References" in Jaeger's span detail) are how you follow a conversation or a case: shopper `ask_assistant` → assistant `invoke_agent` (reply) → assistant `execute_tool handoff_to_fraud` → fraud `review_case` → `execute_tool escalate_to_human`.

During a live run the agent's steps already appear together in one trace; Jaeger flags a missing parent until the run ends and the `agent_run` span is written. Runs recorded before this change only have per-tick traces.

Older style search (still works): **Service** = the agent's role and **Tags** = the agent:

| Agent | Service | Tags |
|---|---|---|
| A shopper (e.g. Amara) | `svc-sim-shopper` | `gen_ai.agent.name=Amara` |
| Shopping Assistant | `svc-sim-assistant` | `gen_ai.agent.name="Shopping Assistant"` |
| Fraud Review | `svc-sim-fraud` | |
| SRE / Ops | `svc-sim-sre` | |
| Whole run | `svc-sim-conductor` | `atlas.run_id=<run id>` |

Each result is one agent step. Expand it and read top to bottom:

```
invoke_agent Amara                        one step of the agent (gen_ai.agent.name, gen_ai.conversation.id, atlas.run_id)
├─ chat svc-sim-mock-small-v1             the model decided what to do (gen_ai.usage.input_tokens / output_tokens, finish reason)
├─ execute_tool add_to_cart               the agent called a tool (gen_ai.tool.name, gen_ai.tool.call.id)
│  └─ POST shop-mcp /mcp                  the MCP server received it
│     └─ execute_tool add_to_cart         the MCP server ran it
│        └─ POST frontend-proxy /api/cart the shop, exactly as a browser
│           └─ frontend → cart (gRPC)     upstream microservices
└─ embeddings ...                         memory retrieval
```

A red span is an error (for example a payment fault). Every svc-sim span carries `atlas.testbed`, `atlas.run_id` and the acting agent's `gen_ai.agent.name`, so a search by agent also finds the MCP server spans done on its behalf.

## 3. What the telemetry does not contain

- Prompt and reply text: spans carry metadata only. The words are in the agent events (sim-viz log, and `viz_events` in agent-memory).
- Upstream shop spans carry the trace context but not the agent attributes (the shop is unmodified); follow the trace tree to attribute them.

## 4. Where the data is kept

| Data | Live view | Kept |
|---|---|---|
| Traces | Jaeger (newest 10,000, memory) | Full copy in `s3://your-s3-bucket/svc-sim/telemetry/<instance id>/traces*.jsonl.zst`, every 15 minutes |
| Metrics | Prometheus via Grafana (7 days) | S3 `metrics*.jsonl.zst` |
| Logs | OpenSearch via Grafana | S3 `logs*.jsonl.zst` |
| Agent events and run data | sim-viz, agent-memory | Nightly `make backup` to `s3://.../svc-sim/backups/` |

S3 files are OTLP JSON, one export batch per line, zstd-compressed: `zstd -dc traces-*.jsonl.zst | jq .` to read.

## Metric names (Prometheus)

| Metric | Labels |
|---|---|
| `svc_sim_agent_events_total` | `gen_ai_agent_name`, `svc_sim_agent_type`, `svc_sim_event_kind`, `gen_ai_tool_name`, `svc_sim_outcome`, `atlas_run_id` |
| `svc_sim_llm_tokens_total` | `gen_ai_agent_name`, `gen_ai_token_type`, `gen_ai_request_model`, `svc_sim_llm_purpose`, `atlas_run_id` |
| `gen_ai_client_token_usage_*`, `gen_ai_client_operation_duration_seconds_*` | as above (OTel GenAI semantic conventions) |
| `traces_span_metrics_*` | upstream span metrics plus `gen_ai_agent_name`, `gen_ai_tool_name`, `gen_ai_operation_name` |
