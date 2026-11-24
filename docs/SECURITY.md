# svc-sim security notes (24 Nov 2026)

## Controls in place

| Area | Control |
|---|---|
| Exposure | Only 127.0.0.1:8080, 8090, 8091 published; checked in CI. Access through SSM or SSH port-forward from an admin workstation. |
| Containers (svc-sim Python services, sim-viz) | Non-root user, read-only root filesystem, `cap_drop: [ALL]`, `no-new-privileges`, memory and CPU limits, healthchecks |
| Containers (agent-memory, Valkey, shop read-only init) | `no-new-privileges`, memory limits; init job also read-only with no capabilities |
| Supply chain | Upstream images pinned by digest (`compose/shop.pins.yaml`), base images pinned by digest, Python packages pinned by version, Phaser vendored with its licence |
| Secrets | Generated locally, never printed, mounted as files; `.env`-style files git-ignored; no AWS keys (instance role only) |
| Shop data access | Agents read the shop database through role `svc_sim_ro` (read-only, `default_transaction_read_only`) |
| Human control | Holds and releases above threshold and every flag change need an approved approval; `store-ops-mcp` refuses (fails closed) without one, and the gateway refuses calls when budget state is unavailable |
| LLM cost and runaway | Per-run token budget, per-run call cap, per-agent rate limit in `llm-gateway` |
| Guardrails | Assistant screens tool output for prompt injection and blocks replies that breach store policy |
| Change hygiene | Banned-term check in CI (list from a repository secret) and as a pre-commit hook (`make hooks`, list from `BANNED_TERMS_FILE` outside the repo) |
| Backups | `make backup` to S3 with SSE, manifest and checksums; `make restore` |

## Known gaps and risks (open)

| Gap | Risk | Proposed fix |
|---|---|---|
| Approval identity is a placeholder (`unverified:<name>`) | Decisions are not attributable to a verified person | Keycloak OIDC through oauth2-proxy in front of 8090 and 8091 (needs client details) |
| Secret files are 0644 inside a 0700 directory | Any process on the host running as ec2-user or root can read them | Acceptable on a single-user test host; move to per-uid ownership if the host is shared |
| No authentication between services on the Docker network (MCP, gateway, Kafka, Postgres for the app role) | A compromised container could call other services | Network is internal and not published; add mTLS or per-service tokens if the bed is shared |
| Agent memory isolation is enforced by the application (queries scoped to the agent id), not by the database | A bug could let one agent read another's memory | Per-agent schemas and roles with row-level security |
| Upstream images run with their own defaults (some as root) | Larger attack surface in the shop | Out of scope: upstream is unmodified by design |
| Kafka plaintext on the internal network | Local sniffing on the host | Acceptable for the test bed |
| `vm.max_map_count`, swap and SSH config changed on the host | Drift from the base AMI | Recorded in the runbook; covered by the change request |
