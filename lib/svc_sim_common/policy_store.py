"""Client for policy-store (procedural memory: policies, runbooks, personas)."""
from __future__ import annotations

from typing import Any

import httpx

from .config import env


class PolicyStoreClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(base_url=env("POLICY_STORE_URL", "http://policy-store:8000"), timeout=30)
        self._cache: dict[str, str] = {}

    async def get_text(self, key: str) -> str:
        if key not in self._cache:
            r = await self._http.get(f"/objects/{key}")
            r.raise_for_status()
            self._cache[key] = r.text
        return self._cache[key]

    async def get_json(self, key: str) -> Any:
        import json

        return json.loads(await self.get_text(key))

    async def list(self, prefix: str = "") -> list[str]:
        r = await self._http.get("/objects", params={"prefix": prefix})
        r.raise_for_status()
        return [o["key"] for o in r.json()["objects"]]
