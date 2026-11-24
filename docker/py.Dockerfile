# One image for every svc-sim Python service; each container picks its entrypoint via `command`.
FROM python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b AS base

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONPATH=/app/lib:/app/gen:/app

RUN groupadd --system --gid 10001 svcsim && useradd --system --uid 10001 --gid svcsim --home /app svcsim

WORKDIR /app
COPY docker/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# gRPC stubs from the pinned upstream proto (store-ops-mcp, fraud agent).
COPY upstream/opentelemetry-demo/pb/demo.proto /app/proto/demo.proto
RUN mkdir -p /app/gen && python -m grpc_tools.protoc -I /app/proto --python_out=/app/gen --grpc_python_out=/app/gen /app/proto/demo.proto

COPY lib /app/lib
COPY schemas /app/schemas
COPY gateway /app/gateway
COPY mcp_servers /app/mcp_servers
COPY agents /app/agents
COPY approval /app/approval
COPY policy-store /app/policy_store
COPY viz/bridge /app/viz_bridge
COPY tests /app/tests
COPY ops/scripts/init_kafka.py /app/ops/init_kafka.py

# Named volumes inherit this ownership on first creation (policy-store writes /data).
RUN mkdir -p /data && chown svcsim:svcsim /data

USER svcsim
