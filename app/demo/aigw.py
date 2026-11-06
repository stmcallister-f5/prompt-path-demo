"""AI gateway stand-in: identity, token budgets and model routing.

Right layer for cost and access control. It knows who is calling and how many
tokens they are spending, but it does not judge what the prompt means.
"""
import json
import os
import time
from collections import defaultdict, deque

from fastapi import Request

from .common import base_app, block, extract_text, forward

UPSTREAM = os.environ.get("UPSTREAM", "http://slot-guard:8080")
API_KEYS = {k.strip() for k in os.environ.get("AIGW_API_KEYS", "booth-demo-key").split(",") if k.strip()}
MAX_PROMPT_TOKENS = int(os.environ.get("AIGW_MAX_PROMPT_TOKENS", "2000"))
TOKENS_PER_MINUTE = int(os.environ.get("AIGW_TOKENS_PER_MINUTE", "20000"))
# Friendly model names the gateway exposes, mapped to what the backend serves.
ROUTES = json.loads(os.environ.get("AIGW_MODEL_ROUTES", '{"booth-assistant": "demo-model"}'))

_spend: dict[str, deque] = defaultdict(deque)
app = base_app("ai-gateway")


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text.split()) * 1.33))


@app.api_route("/{path:path}", methods=["GET", "POST"])
async def handle(request: Request, path: str):
    started = time.perf_counter()
    auth = request.headers.get("authorization", "")
    key = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if key not in API_KEYS:
        return block(401, "unauthenticated", "missing or unknown API key", started)

    body = await request.body()
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        return block(400, "bad_request", "body is not JSON", started)

    tokens = estimate_tokens(extract_text(payload))
    if tokens > MAX_PROMPT_TOKENS:
        return block(429, "prompt_token_limit", f"~{tokens} tokens > {MAX_PROMPT_TOKENS} per request", started,
                     tokens=tokens)

    now = time.time()
    window = _spend[key]
    while window and now - window[0][0] > 60:
        window.popleft()
    spent = sum(t for _, t in window)
    if spent + tokens > TOKENS_PER_MINUTE:
        return block(429, "token_budget", f"{spent + tokens} tokens this minute > {TOKENS_PER_MINUTE}", started,
                     tokens=tokens)
    window.append((now, tokens))

    requested = payload.get("model", "booth-assistant")
    routed = ROUTES.get(requested, requested)
    if isinstance(payload, dict):
        payload["model"] = routed
    return await forward(request, UPSTREAM, started, body=json.dumps(payload).encode(),
                         detail=f"key ok, ~{tokens} tokens, model {requested} -> {routed}", )
