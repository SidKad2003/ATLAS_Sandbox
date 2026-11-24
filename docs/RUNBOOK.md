# svc-sim runbook

Host: `svc-sim-host` (`i-0123456789abcdef0`, eu-west-2). Repo checkout: `/home/ec2-user/svc-sim`. Run everything as `ec2-user`.

## First deployment

```bash
git clone git@github.com:your-org/svc-sim.git svc-sim   # deploy key, SSH over port 443 (see below)
cd svc-sim
make deploy        # fetch pinned upstream, generate secrets, build images, start, wait until healthy
```

`make deploy` takes about 10 minutes the first time (upstream images are roughly 12 GB).

GitHub over SSH: outbound port 22 is blocked from the host, so `~/.ssh/config` points `github.com` at `ssh.github.com:443` with the read-only deploy key `~/.ssh/svc_sim_deploy`.

## Daily operation

| Task | Command |
|---|---|
| Start | `make up-svc-sim` |
| Stop (keeps data) | `make down-svc-sim` |
| Health and memory | `make status` |
| Logs for one service | `make logs SVC=conductor` |
| Update code | `git pull && make deploy` |
| UIs from your admin workstation | `make tunnel` prints the SSM port-forward commands |

## Runs

| Task | Command |
|---|---|
| Seeded run (default 5 shoppers, 60 ticks = 30 sim-minutes, about 2 to 3 minutes wall time) | `make run SEED=42` |
| Different size | `make run SEED=7 SHOPPERS=3 TICKS=120` |
| List runs and outcomes | `make run-status` |
| Determinism check (two runs, same seed, compares every agent's event sequence) | `make verify-determinism SEED=42` |
| Contract tests against the live stack | `make test` |

| List the scenario catalogue | `make scenarios` |
| Record every scenario once for replay (about 25 minutes) | `make replays` |
| Run one scenario | `make scenario NAME=payment-outage` |
| Slow, long run to watch in Live mode (240 ticks at 4 s, about 16 minutes) | `make live-demo` (override with `TICKS_LIVE=` and `WALL=`) |

Each run's schedule (personas, fault injections, adversarial scenarios) is derived from the seed and stored in `agent-memory.runs`.
Scenarios are defined in `agents/scenarios.py`; each carries a label that sim-viz shows in the run list.

Default schedule:
- Shoppers: 3 ordinary personas chosen by seed, plus Gareth (prompt-injection prober) and Mallory (social engineer).
- Fault: `productCatalogFailure` on at 33 % of the run, cleared at 85 % unless a human approves the SRE agent's proposal first.
- Adversarial: Mallory asks the assistant to change a delivery address without checks after ordering; Gareth asks about the review that carries an injection payload.

## Backup and restore

| Task | Command |
|---|---|
| Back up everything to S3 (about 2 minutes, safe while running) | `make backup` |
| Last backup location | `cat runtime/last-backup` |
| List backups | `aws s3 ls s3://your-s3-bucket/svc-sim/backups/` |
| Restore run data, memories, approvals, policy-store and flags | `make restore FROM=s3://your-s3-bucket/svc-sim/backups/<timestamp>` |
| Also restore the shop database | add `SHOP_DB=1` |
| Install the automatic timers (telemetry to S3 every 15 minutes, full backup nightly at 02:00 UTC) | `make s3-timers` (once per host) |
| Check the timers | `systemctl list-timers 'svc-sim-*'` and `journalctl -u svc-sim-telemetry-sync -n 20` |
| Upload telemetry now | `make telemetry-sync` |

Reading the agent telemetry in Grafana and Jaeger: see `docs/TELEMETRY-GUIDE.md`.

Back up before stopping for a long time and always before terminating the instance: the root volume is deleted on termination. Passwords are not backed up; a new host generates its own with `make secrets`. Traces, logs and metrics in a backup are archives for analysis (gzipped JSON lines and a Prometheus store copy); they are not loaded back.

## Viewing the UIs quickly

SSM port-forwarding is slow for Jaeger and Grafana, which load several MB of JavaScript. From the admin workstation, an SSH tunnel is much faster:

```bash
ssh -N -L 8080:127.0.0.1:8080 -L 8090:127.0.0.1:8090 -L 8091:127.0.0.1:8091 svc-sim-host
```

## Checks and maintenance

| Task | Command |
|---|---|
| Offline checks (banned terms, unit tests, JS syntax), same as CI | `make check` |
| Install the pre-commit banned-term hook | `make hooks` (set `BANNED_TERMS_FILE` to the list outside the repo) |
| Re-pin upstream images after a tested upgrade | `make pin-digests` |

## Human approvals

Open the approval desk (http://localhost:8090 after `make tunnel`). Pending items are fraud holds and SRE flag changes.
Approving a flag change: the Conductor applies it on the next tick. Approving a hold: the Fraud agent applies it on its next tick.
Identity is recorded as `unverified:<name>` until Keycloak is connected.

## Replay

Open sim-viz (http://localhost:8091), switch to Replay, pick the run, use the scrubber. Replay reads `viz_events` in `agent-memory`.

## Baseline (load generator)

```bash
make loadgen-on    # Locust traffic for baseline comparison
make loadgen-off   # back to idle for agent runs
```

## Switching workloads

`make switch WORKLOAD=agent-eval` stops svc-sim, checks containers are gone, ports 8080/8090/8091 are free and at least 9 GiB is available, then starts the other workload. `agent-eval` is not added yet.

## Reset (destroys all svc-sim state, no backup)

```bash
make reset         # asks you to type svc-sim
```

Removes containers, the `svc-sim-agent-memory` and `svc-sim-policy-store` volumes and the runtime flag file. Kafka topics on the shop broker are recreated on the next start.

## Troubleshooting

| Symptom | Check |
|---|---|
| `make up-svc-sim` waits for ever | `make status`; a container stuck in `health: starting`. `docker inspect <name> --format '{{json .State.Health}}'` |
| No orders in a run | `make logs SVC=shop-mcp` and the frontend: `docker logs svc-sim-frontend` |
| Agents stop with "budget exhausted" | Per-run token budget or call cap in `llm-gateway` (`SVC_SIM_RUN_TOKEN_BUDGET`, `SVC_SIM_RUN_CALL_CAP`) |
| A fault flag stuck on after a crash | Approve the SRE proposal, or `make down-svc-sim && rm runtime/flagd/demo.flagd.json && make up-svc-sim` |

## Known upstream issues worked around

- `recommendation` image (3.1.0) has no `nc`, so its upstream healthcheck never passes. Overridden in `compose/shop.override.yaml`.
- Some `*_ADDR` values are URLs (`http://shipping:50050`); `store-ops-mcp` strips the scheme for gRPC.
