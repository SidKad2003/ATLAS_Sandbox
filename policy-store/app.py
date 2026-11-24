"""policy-store: procedural memory (store policy, fraud policy, runbooks, personas, synthetic reviews).

Object-store style interface (list by prefix, get by key). Backend is a local volume now and can
switch to S3 under an approved prefix with POLICY_STORE_BACKEND=s3 (instance role only, no keys).
"""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

from svc_sim_common.telemetry import instrument_asgi, setup_telemetry

setup_telemetry("svc-sim-policy-store")

BACKEND = os.environ.get("POLICY_STORE_BACKEND", "local")
ROOT = Path(os.environ.get("POLICY_STORE_ROOT", "/data"))
SEED = Path(os.environ.get("POLICY_STORE_SEED", "/app/policy_store/seed"))
BUCKET = os.environ.get("POLICY_STORE_BUCKET", "")
PREFIX = os.environ.get("POLICY_STORE_PREFIX", "svc-sim/policy-store/")


class LocalBackend:
    def __init__(self) -> None:
        ROOT.mkdir(parents=True, exist_ok=True)
        for src in SEED.rglob("*"):
            if src.is_file():
                dst = ROOT / src.relative_to(SEED)
                # the repo seed is the source of truth: add new files and refresh changed ones
                if not dst.exists() or dst.read_bytes() != src.read_bytes():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, dst)

    def _path(self, key: str) -> Path:
        p = (ROOT / key).resolve()
        if ROOT.resolve() not in p.parents:
            raise HTTPException(400, "invalid key")
        return p

    async def list(self, prefix: str) -> list[dict]:
        out = []
        for p in sorted(ROOT.rglob("*")):
            if p.is_file():
                key = p.relative_to(ROOT).as_posix()
                if key.startswith(prefix):
                    out.append({"key": key, "size": p.stat().st_size})
        return out

    async def get(self, key: str) -> str:
        p = self._path(key)
        if not p.is_file():
            raise HTTPException(404, "not found")
        return p.read_text(encoding="utf-8")


class S3Backend:
    def __init__(self) -> None:
        import boto3

        if not BUCKET:
            raise RuntimeError("POLICY_STORE_BUCKET is required for the s3 backend")
        self.s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "eu-west-2"))

    async def list(self, prefix: str) -> list[dict]:
        resp = await asyncio.to_thread(self.s3.list_objects_v2, Bucket=BUCKET, Prefix=PREFIX + prefix)
        return [{"key": o["Key"][len(PREFIX):], "size": o["Size"]} for o in resp.get("Contents", [])]

    async def get(self, key: str) -> str:
        try:
            obj = await asyncio.to_thread(self.s3.get_object, Bucket=BUCKET, Key=PREFIX + key)
        except Exception as exc:
            raise HTTPException(404, "not found") from exc
        return obj["Body"].read().decode("utf-8")


backend = S3Backend() if BACKEND == "s3" else LocalBackend()
api = FastAPI(title="svc-sim policy-store")


@api.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "backend": BACKEND}


@api.get("/objects")
async def list_objects(prefix: str = "") -> dict:
    return {"objects": await backend.list(prefix)}


@api.get("/objects/{key:path}", response_class=PlainTextResponse)
async def get_object(key: str) -> str:
    return await backend.get(key)


app = instrument_asgi(api)
