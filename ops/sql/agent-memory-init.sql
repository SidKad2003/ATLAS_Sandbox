-- svc-sim agent-memory schema (Postgres 16 + pgvector). Separate from the shop's astronomy-db.
-- Embedding dimension is fixed here (64). Change EMBED_DIM and this file together, then `make reset`.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    seed        TEXT NOT NULL,
    config      JSONB NOT NULL,
    schedule    JSONB NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',
    sim_start   TIMESTAMP NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    summary     JSONB
);

CREATE TABLE IF NOT EXISTS episodic_memory (
    id          BIGSERIAL PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    sim_time    TIMESTAMP NOT NULL,
    kind        TEXT NOT NULL,
    content     TEXT NOT NULL,
    importance  INT NOT NULL CHECK (importance BETWEEN 1 AND 10),
    embedding   vector(64) NOT NULL
);
CREATE INDEX IF NOT EXISTS episodic_agent_run ON episodic_memory (agent_id, run_id, sim_time);

CREATE TABLE IF NOT EXISTS semantic_memory (
    id          BIGSERIAL PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    sim_time    TIMESTAMP NOT NULL,
    subject     TEXT NOT NULL,
    belief      TEXT NOT NULL,
    confidence  REAL NOT NULL,
    embedding   vector(64) NOT NULL
);
CREATE INDEX IF NOT EXISTS semantic_agent_run ON semantic_memory (agent_id, run_id);

CREATE TABLE IF NOT EXISTS case_log (
    id          BIGSERIAL PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    sim_time    TIMESTAMP NOT NULL,
    order_id    TEXT,
    source      TEXT NOT NULL,
    verdict     TEXT NOT NULL,
    score       REAL NOT NULL,
    details     JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS incidents (
    id          BIGSERIAL PRIMARY KEY,
    run_id      TEXT NOT NULL,
    opened_by   TEXT NOT NULL,
    sim_time    TIMESTAMP NOT NULL,
    title       TEXT NOT NULL,
    severity    TEXT NOT NULL,
    evidence    JSONB NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS order_holds (
    id          BIGSERIAL PRIMARY KEY,
    run_id      TEXT NOT NULL,
    order_id    TEXT NOT NULL,
    action      TEXT NOT NULL CHECK (action IN ('hold', 'release')),
    reason      TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    approval_id BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS order_holds_order ON order_holds (order_id, id);

CREATE TABLE IF NOT EXISTS approvals (
    id          BIGSERIAL PRIMARY KEY,
    run_id      TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('hold_order', 'release_order', 'flag_change', 'escalation')),
    requested_by TEXT NOT NULL,
    subject     TEXT NOT NULL,
    request     JSONB NOT NULL,
    rationale   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'applied', 'expired')),
    decided_by  TEXT,
    decided_at  TIMESTAMPTZ,
    decision_note TEXT,
    applied_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS approvals_status ON approvals (status, kind);

CREATE TABLE IF NOT EXISTS chat_log (
    id          BIGSERIAL PRIMARY KEY,
    run_id      TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    sim_time    TIMESTAMP NOT NULL
);

-- Event store for sim-viz replay (written only by sim-viz-bridge).
CREATE TABLE IF NOT EXISTS viz_events (
    event_id    TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    sim_tick    INT NOT NULL,
    seq         INT NOT NULL,
    agent_id    TEXT NOT NULL,
    kind        TEXT NOT NULL,
    body        JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS viz_events_run ON viz_events (run_id, sim_tick, received_at);
