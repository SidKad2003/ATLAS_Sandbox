"""Configuration helpers. Secrets are read from files under /run/secrets, never from the environment."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

SECRETS_DIR = Path(os.environ.get("SVC_SIM_SECRETS_DIR", "/run/secrets"))

TESTBED = "svc-sim"


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"missing required setting {name}")
    return value


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=None)
def read_secret(name: str) -> str:
    """Read a secret file mounted by Compose. The value is never logged."""
    path = SECRETS_DIR / name
    if not path.exists():
        raise RuntimeError(f"secret file {name} is not mounted")
    return path.read_text(encoding="utf-8").strip()


def agent_memory_dsn() -> str:
    host = env("AGENT_MEMORY_HOST", "agent-memory")
    db = env("AGENT_MEMORY_DB", "svc_sim")
    user = env("AGENT_MEMORY_USER", "svc_sim")
    return f"postgresql://{user}:{read_secret('agent_memory_password')}@{host}:5432/{db}"


def working_memory_url() -> str:
    host = env("AGENT_WORKING_MEMORY_HOST", "agent-working-memory")
    return f"redis://:{read_secret('agent_working_memory_password')}@{host}:6379/0"


def kafka_bootstrap() -> str:
    return env("KAFKA_ADDR", "kafka:9092")


ORDERS_TOPIC = os.environ.get("KAFKA_TOPIC", "orders")
TOPIC_EVENTS = "agent.events"
TOPIC_HANDOFF = "agent.handoff"
