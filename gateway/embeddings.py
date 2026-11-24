"""Deterministic hash-based embeddings for the mock provider (feature hashing over word tokens)."""
from __future__ import annotations

import hashlib
import math
import re

_TOKEN = re.compile(r"[a-z0-9]+")  # lowercase alphanumeric word tokens


def hash_embedding(text: str, dim: int = 64) -> list[float]:
    vec = [0.0] * dim
    for tok in _TOKEN.findall(text.lower()):
        h = hashlib.sha256(tok.encode()).digest()
        idx = int.from_bytes(h[:4], "big") % dim
        sign = 1.0 if h[4] & 1 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        vec[0] = 1.0
        return vec
    return [round(v / norm, 6) for v in vec]
