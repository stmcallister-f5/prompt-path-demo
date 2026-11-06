"""Relay: puts a third-party product (F5 WAF for NGINX, F5 AI Gateway, ...) in a slot.

The slot Service selects this relay's pods; the relay forwards to the product's
own Service, and the product forwards on to the next slot. The relay adds the
trace record the product doesn't, and when the product refuses a request it
records the block against the product's name, so the toggle board can light
the right hop.

RELAY_BLOCK_STATUSES   statuses that mean "this product blocked it" (default 400,401,403,406,429)
RELAY_BLOCK_PATTERN    text in the response body that means "this product blocked it", for products
                       that answer a block with 200 (F5 WAF for NGINX's default block page is a 200
                       containing "support ID"). Only counts when nothing downstream answered.
"""
import json
import os
import time

from fastapi import Request
from fastapi.responses import Response

from .common import _HOP_HEADERS, COMPONENT_HEADER, TRACE_HEADER, base_app, display_name, new_client, read_trace, record, with_trace

UPSTREAM = os.environ.get("UPSTREAM", "")
BLOCK_STATUSES = {int(s) for s in os.environ.get("RELAY_BLOCK_STATUSES", "400,401,403,406,429").split(",")}
BLOCK_PATTERN = os.environ.get("RELAY_BLOCK_PATTERN", "")

app = base_app("relay")


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def handle(request: Request, path: str):
    started = time.perf_counter()
    url = UPSTREAM.rstrip("/") + request.url.path + (f"?{request.url.query}" if request.url.query else "")
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS}
    async with new_client() as client:
        up = await client.request(request.method, url, content=await request.body(), headers=headers)
    total = (time.perf_counter() - started) * 1000
    downstream = read_trace(up.headers)
    down_ms = sum(t.get("ms", 0) for t in downstream)
    out_headers = {k: v for k, v in up.headers.items()
                   if k.lower() not in _HOP_HEADERS and k.lower() not in (TRACE_HEADER, COMPONENT_HEADER, "content-encoding")}

    blocked = up.status_code in BLOCK_STATUSES or (BLOCK_PATTERN and BLOCK_PATTERN in up.text)
    if blocked and not downstream:
        # Nothing behind the product answered: the product itself refused the request.
        # A block page served as 200 still goes back as 403, so nothing upstream mistakes it for success.
        status = up.status_code if up.status_code in BLOCK_STATUSES else 403
        reason = f"HTTP {up.status_code}" if status == up.status_code else "block page"
        snippet = " ".join(up.text.split())[:160]
        body = {"error": {"type": "blocked", "blocked_by": display_name(), "hop": os.environ.get("COMPONENT", "relay"),
                          "reason": reason, "detail": snippet}}
        resp = Response(json.dumps(body), status_code=status, media_type="application/json")
        return with_trace(resp, [record("block", total, f"{reason} from {display_name()}")])

    resp = Response(content=up.content, status_code=up.status_code, headers=out_headers)
    return with_trace(resp, [record("pass", max(0.0, total - down_ms), f"via {UPSTREAM}")] + downstream)
