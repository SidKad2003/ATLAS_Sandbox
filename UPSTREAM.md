# Upstream: OpenTelemetry Demo (Astronomy Shop)

| Item | Value |
|---|---|
| Repository | https://github.com/open-telemetry/opentelemetry-demo |
| Release tag | `3.1.0` (published 18 Sep 2026) |
| Commit SHA | `dedc0178918e260823323b8d95005a8cb924b007` |
| Licence | Apache-2.0, copied to `upstream/LICENSE`. The release has no NOTICE file. |
| How it is fetched | `make fetch-upstream` clones the tag into `upstream/opentelemetry-demo/` (git-ignored) and checks the SHA |

## Compose files used

`compose.yaml` + `compose.full.yaml` + `compose.observability.yaml`, then `compose/shop.override.yaml`.
`compose.agent.yaml` (upstream's own agent, chatbot and MCP services) is **not** used; svc-sim supplies its own agent layer.

## What svc-sim changes (configuration only, no service code)

- All upstream port publications removed; only `frontend-proxy` is published, on `127.0.0.1`.
- Container names prefixed `svc-sim-`; network renamed `svc-sim`.
- `load-generator`: `LOCUST_AUTOSTART=false` and browser traffic off by default (`make loadgen-on` for baseline runs).
- `flagd` and `flagd-ui` read `runtime/flagd/demo.flagd.json`, a copy of the upstream file, so upstream files stay untouched.
- `flagd-ui` `SECRET_KEY_BASE` replaced with a locally generated value (upstream ships a fixed public value).
- `jaeger` memory limit 800M and `MEMORY_MAX_TRACES=10000` (upstream 1200M / 25000).
- Collector extras seam (`/etc/otelcol-config-extras.yml`) replaced with `ops/collector/otelcol-config-svc-sim.yml`, which adds `rait.testbed=svc-sim` to all telemetry.

## Differences from the build brief and the architecture diagram (verified against 3.1.0)

| Brief / diagram | 3.1.0 reality | svc-sim handling |
|---|---|---|
| `product-reviews` service | Not present | `get_reviews` serves synthetic reviews from policy-store (`reviews/reviews.json`) |
| Shop Postgres `postgres` | Service is `astronomy-db`, database `astronomy_db`, schemas `accounting` and `catalog` | Read-only role `svc_sim_ro` created by `svc-sim-shop-ro-init` |
| Frontend search API | No search route; the browser filters client-side | `shop-mcp.search_products` lists and filters, as a browser does |
| Frontend currency conversion | Only `GET /api/currency` (supported list) | `shop-mcp.convert_currency` derives the rate from a reference product priced in both currencies |
| Flag names | Confirmed: `adFailure`, `adHighCpu`, `adManualGc`, `cartFailure`, `emailMemoryLeak`, `failedReadinessProbe`, `imageSlowLoad`, `intlShippingSlowdown`, `kafkaQueueProblems`, `loadGeneratorFloodHomepage`, `paymentFailure`, `paymentUnreachable`, `productCatalogFailure`, `productCatalogLockContention`, `recommendationCacheFailure`, plus `aiRunawayAgent`, `aiSlowResponse`, `emitRawPii` | `productCatalogFailure` is controlled by its targeting rule (product `OLJCESPC7Z`), not by `defaultVariant`; the Conductor flips the rule branch |
| gRPC methods | Confirmed as in the diagram (`ListProducts`, `GetProduct`, `SearchProducts`, `ListRecommendations`, `AddItem`, `GetCart`, `EmptyCart`, `GetQuote`, `ShipOrder`, `Convert`, `GetSupportedCurrencies`, `Charge`, `SendOrderConfirmation`, `PlaceOrder`, `GetAds`) | Stubs generated from `pb/demo.proto` at image build |
| Orders topic | `${KAFKA_TOPIC}` carries protobuf `OrderResult` (no user id or email) | Fraud agent links orders to customers by shipping address |

## Image digests

Upstream images are referenced by tag from the upstream `.env` (not read or modified by svc-sim). Record digests after the first pull on the host with:

```bash
docker images --digests --format '{{.Repository}}:{{.Tag}} {{.Digest}}' | grep -i opentelemetry-demo
```
