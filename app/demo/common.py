"""Shared plumbing for every hop in the demo request path.

Each hop is a tiny reverse proxy. On the way back it adds one record to the
`x-demo-trace` response header, so the toggle board can show exactly which
hops a request crossed, what each one decided, and how long each one took.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

TRACE_HEADER = "x-demo-trace"
COMPONENT_HEADER = "x-demo-component"

# Hop-by-hop headers that must not be forwarded.
_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}


def component() -> str:
    return os.environ.get("COMPONENT", "unknown")


def display_name() -> str:
    return os.environ.get("DISPLAY_NAME", component())


def new_client() -> httpx.AsyncClient:
    # No keep-alive on purpose: when the toggle board rewires a slot Service,
    # a pooled connection would stay pinned to the old pod through conntrack.
    return httpx.AsyncClient(
        timeout=httpx.Timeout(float(os.environ.get("UPSTREAM_TIMEOUT", "120"))),
        limits=httpx.Limits(max_keepalive_connections=0),
    )


def record(verdict: str, ms: float, detail: str = "", **extra: Any) -> dict:
    r = {"hop": component(), "name": display_name(), "verdict": verdict,
         "ms": round(ms, 1), "detail": detail}
    r.update(extra)
    return r


def read_trace(headers: httpx.Headers | dict) -> list:
    raw = headers.get(TRACE_HEADER)
    if not raw:
        return []
    try:
        return json.loads(raw)
    except ValueError:
        return []


def with_trace(resp: Response, trace: list) -> Response:
    resp.headers[TRACE_HEADER] = json.dumps(trace, separators=(",", ":"))
    resp.headers[COMPONENT_HEADER] = component()
    return resp


def block(status: int, reason: str, detail: str, started: float, **extra: Any) -> Response:
    """Stop the request at this hop."""
    ms = (time.perf_counter() - started) * 1000
    body = {"error": {"type": "blocked", "blocked_by": display_name(), "hop": component(),
                      "reason": reason, "detail": detail}}
    return with_trace(JSONResponse(body, status_code=status),
                      [record("block", ms, f"{reason}: {detail}", **extra)])


async def forward(request: Request, upstream: str, started: float, body: bytes | None = None,
                  detail: str = "", inspect_response=None) -> Response:
    """Proxy the request to the next hop and prepend this hop's trace record.

    `inspect_response(status, body_bytes)` may return (reason, detail) to block
    the response on its way back (used for response scanning).
    """
    url = upstream.rstrip("/") + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS}
    if body is None:
        body = await request.body()
    t_up = time.perf_counter()
    try:
        async with new_client() as client:
            up = await client.request(request.method, url, content=body, headers=headers)
    except httpx.HTTPError as exc:
        ms = (time.perf_counter() - started) * 1000
        resp = JSONResponse({"error": {"type": "upstream_unreachable", "hop": component(),
                                       "detail": f"{type(exc).__name__}: {exc}"}}, status_code=502)
        return with_trace(resp, [record("error", ms, f"next hop unreachable ({upstream})")])
    up_ms = (time.perf_counter() - t_up) * 1000
    downstream = read_trace(up.headers)

    if inspect_response is not None:
        verdict = inspect_response(up.status_code, up.content)
        if verdict:
            reason, why = verdict
            mine_ms = (time.perf_counter() - started) * 1000 - up_ms
            body_out = {"error": {"type": "blocked", "blocked_by": display_name(), "hop": component(),
                                  "reason": reason, "detail": why, "direction": "response"}}
            return with_trace(JSONResponse(body_out, status_code=403),
                              [record("block", mine_ms, f"{reason}: {why}", direction="response")] + downstream)

    mine_ms = (time.perf_counter() - started) * 1000 - up_ms
    out_headers = {k: v for k, v in up.headers.items()
                   if k.lower() not in _HOP_HEADERS and k.lower() not in (TRACE_HEADER, COMPONENT_HEADER, "content-encoding")}
    resp = Response(content=up.content, status_code=up.status_code, headers=out_headers)
    return with_trace(resp, [record("pass", mine_ms, detail)] + downstream)


def base_app(title: str) -> FastAPI:
    app = FastAPI(title=title, docs_url=None, redoc_url=None)

    @app.get("/healthz")
    async def healthz() -> Response:
        # The toggle board probes slot Services with this to confirm a rewire landed.
        r = JSONResponse({"ok": True, "component": component()})
        r.headers[COMPONENT_HEADER] = component()
        return r

    return app


def extract_text(payload: Any) -> str:
    """Flatten an OpenAI-style chat payload (or anything JSON) into inspectable text."""
    if isinstance(payload, dict) and isinstance(payload.get("messages"), list):
        parts = []
        for m in payload["messages"]:
            c = m.get("content")
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, list):
                parts.extend(p.get("text", "") for p in c if isinstance(p, dict))
        return "\n".join(parts)
    if isinstance(payload, str):
        return payload
    return json.dumps(payload)


def user_text(payload: Any) -> str:
    """Only the messages a caller sent as the user (what an inbound guardrail inspects)."""
    if isinstance(payload, dict) and isinstance(payload.get("messages"), list):
        return "\n".join(m.get("content", "") for m in payload["messages"]
                         if m.get("role") == "user" and isinstance(m.get("content"), str))
    return extract_text(payload)
