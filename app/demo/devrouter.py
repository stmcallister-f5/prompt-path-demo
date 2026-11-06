"""Laptop stand-in for Kubernetes Services, used only by `make dev`.

Requests to /slot/<service>/<path> are proxied to whichever component that
"Service" currently selects. The toggle board's local driver updates the
mapping through /_slots, exactly the way the Kubernetes driver patches a
Service selector in the cluster.
"""
import json
import os

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

COMPONENTS = json.loads(os.environ.get("DEV_COMPONENTS", "{}"))   # component -> base URL
SLOTS: dict[str, str] = json.loads(os.environ.get("DEV_SLOTS", "{}"))  # service -> component

app = FastAPI(docs_url=None, redoc_url=None)


@app.get("/_slots")
async def get_slots():
    return SLOTS


@app.put("/_slots/{service}")
async def put_slot(service: str, request: Request):
    SLOTS[service] = (await request.json())["component"]
    return {service: SLOTS[service]}


@app.api_route("/slot/{service}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(service: str, path: str, request: Request):
    comp = SLOTS.get(service, service)
    base = COMPONENTS.get(comp)
    if not base:
        return JSONResponse({"error": f"no component for slot {service}"}, status_code=502)
    url = f"{base}/{path}" + (f"?{request.url.query}" if request.url.query else "")
    headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
    async with httpx.AsyncClient(timeout=120, limits=httpx.Limits(max_keepalive_connections=0)) as c:
        r = await c.request(request.method, url, content=await request.body(), headers=headers)
    out = {k: v for k, v in r.headers.items() if k.lower() not in ("content-length", "transfer-encoding", "connection", "content-encoding")}
    return Response(r.content, status_code=r.status_code, headers=out)
