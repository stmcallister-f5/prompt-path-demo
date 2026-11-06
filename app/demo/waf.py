"""Edge WAF stand-in: classic signature matching, the way a WAF actually works.

It inspects the URL and the raw request body for well-known attack patterns
(a small, readable subset of the kind of rules in OWASP CRS). It is good at
what it does, and it has nothing to match in a polite English prompt. That is
the point of beat 1.
"""
import os
import re
import time
from urllib.parse import unquote_plus

from fastapi import Request

from .common import base_app, block, forward

UPSTREAM = os.environ.get("UPSTREAM", "http://slot-aigw:8080")
MAX_BODY = int(os.environ.get("WAF_MAX_BODY_BYTES", str(64 * 1024)))

SIGNATURES = [
    ("sqli", "SQL tautology", re.compile(r"'\s*or\s+'?\d+'?\s*=\s*'?\d+", re.I)),
    ("sqli", "SQL DDL/DML keyword chain", re.compile(r";\s*(drop|delete|truncate|alter|insert|update)\s+(table|from|into)?", re.I)),
    ("sqli", "UNION-based SQL injection", re.compile(r"union\s+(all\s+)?select", re.I)),
    ("sqli", "SQL comment terminator", re.compile(r"'\s*(--|#|/\*)")),
    ("xss", "Script tag", re.compile(r"<\s*script\b", re.I)),
    ("xss", "Inline event handler", re.compile(r"\bon(error|load|click|mouseover)\s*=", re.I)),
    ("xss", "javascript: URI", re.compile(r"javascript\s*:", re.I)),
    ("lfi", "Path traversal", re.compile(r"(\.\./){2,}|%2e%2e%2f", re.I)),
    ("rce", "Shell command injection", re.compile(r"(;|\|\||&&|\$\()\s*(cat|curl|wget|bash|sh|nc)\b", re.I)),
]

app = base_app("edge-waf")


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def handle(request: Request, path: str):
    started = time.perf_counter()
    body = await request.body()
    if len(body) > MAX_BODY:
        return block(413, "body_too_large", f"{len(body)} bytes", started)
    haystack = unquote_plus(str(request.url)) + "\n" + body.decode("utf-8", "replace")
    for family, name, rx in SIGNATURES:
        m = rx.search(haystack)
        if m:
            return block(403, f"signature:{family}", f"{name} matched {m.group(0)!r}", started,
                         signatures_matched=1)
    return await forward(request, UPSTREAM, started, body=body,
                         detail=f"{len(SIGNATURES)} signatures checked, 0 matched")
