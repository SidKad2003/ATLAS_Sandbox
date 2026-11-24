# Runbook: fault flags (synthetic, svc-sim)

Applies when a service shows elevated errors or latency.

1. Read the feature flags. If a fault flag is active, it is the most likely cause.
2. Confirm with metrics (error rate by service), traces (error spans for the suspect service) and logs.
3. Open an incident with the evidence.
4. Propose turning the flag to its off variant. The change needs human approval; never change flags directly.

| Flag | Service | Symptom |
|---|---|---|
| paymentFailure, paymentUnreachable | payment | checkout fails with payment errors |
| cartFailure | cart | add to basket fails |
| productCatalogFailure | product-catalog | one product page errors |
| recommendationCacheFailure | recommendation | slow or failing recommendations |
| adFailure, adHighCpu, adManualGc | ad | ads fail or are slow |
| kafkaQueueProblems | kafka, fraud-detection, accounting | consumer lag |
| imageSlowLoad | image-provider | slow images |
| intlShippingSlowdown | shipping | slow international quotes |
| emailMemoryLeak | email | email memory growth |
