"""Guardrail stand-in: reads text the way a model would, not the way a WAF does.

GUARDRAIL_MODE=inbound  sits in the request path in front of the agent. Scans
                        what the user sent (front door) and, optionally, what
                        comes back (back door: leaked secrets).
GUARDRAIL_MODE=tools    sits between the agent and its tools. Scans tool
                        results before the agent reads them (side door).

GUARDRAIL_BACKEND=heuristic (default) uses the small detector below. It is
deliberately simple and readable; booth visitors *can* beat it, which makes a
good conversation. GUARDRAIL_BACKEND=external calls a real scanning service
(see `external_scan`) such as F5 AI Guardrails.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import unicodedata

import httpx
from fastapi import Request

from .common import base_app, block, extract_text, forward, new_client, user_text

UPSTREAM = os.environ.get("UPSTREAM", "http://app:8080")
MODE = os.environ.get("GUARDRAIL_MODE", "inbound")
BACKEND = os.environ.get("GUARDRAIL_BACKEND", "heuristic")
SCAN_RESPONSES = os.environ.get("GUARDRAIL_SCAN_RESPONSES", "true").lower() == "true"
CANARIES = [c.strip() for c in os.environ.get("GUARDRAIL_CANARIES", "STAFF-40").split(",") if c.strip()]

INJECTION = [
    ("instruction_override", re.compile(
        r"\b(ignore|disregard|forget|override|bypass)\b.{0,40}\b(instructions?|rules|guidelines|guardrails|prompt|polic(y|ies))\b")),
    ("system_prompt_extraction", re.compile(
        r"\b(print|reveal|show|repeat|output|tell|give|dump|list|leak|share|display|what (is|are))\b.{0,60}"
        r"\b(system prompt|hidden prompt|initial prompt|your instructions|internal (code|instructions)|instructions above|secret)")),
    ("role_hijack", re.compile(
        r"\b(you are now|from now on you|act as (an? )?(unrestricted|unfiltered|dan)|developer mode|jailbreak|do anything now)\b")),
    ("agent_directed_instruction", re.compile(
        r"\b(ai|assistant|agent|llm|model)( agent)?\s*:\s*(ignore|email|send|forward|delete|run|execute|call)\b|note to the (ai|assistant|agent)")),
    ("instructions_to_ai", re.compile(
        r"\b(notice|note|message|instructions?|attention) (for|to) (all )?(ai|llm)? ?(assistants?|agents?|models?)\b"
        r"|\bdo not (mention|tell|reveal|disclose) (this|these|it)\b.{0,30}\b(user|customer)")),
    ("data_exfiltration", re.compile(
        r"\b(email|send|forward|post|upload|exfiltrate)\b.{0,50}\b(customer (list|data|records)|credentials|api keys?|passwords?|database)")),
]
PII = [
    ("pii_ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("pii_card", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
]
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})
_ZW = re.compile(r"[​-‏⁠﻿]")


def _variants(text: str) -> list[str]:
    t = unicodedata.normalize("NFKC", _ZW.sub("", text)).lower()
    t = re.sub(r"\s+", " ", t)
    out = [t, t.translate(_LEET), re.sub(r"(?<=\w)[.\-_*](?=\w)", "", t)]
    for chunk in re.findall(r"[A-Za-z0-9+/]{24,}={0,2}", text):   # base64-smuggled instructions
        try:
            out.append(base64.b64decode(chunk, validate=True).decode("utf-8").lower())
        except Exception:
            pass
    return out


def heuristic_scan(text: str, *, check_pii: bool) -> tuple[str, str] | None:
    for v in _variants(text):
        for name, rx in INJECTION:
            m = rx.search(v)
            if m:
                return name, f"matched {m.group(0)[:80]!r}"
    if check_pii:
        for name, rx in PII:
            m = rx.search(text)
            if m:
                return name, "sensitive data in prompt"
    return None


async def external_scan(text: str) -> tuple[str, str] | None:
    """Adapter for a real guardrail service. Configure with env vars:

    GUARDRAIL_SCAN_URL     endpoint to POST to
    GUARDRAIL_TOKEN        bearer token
    GUARDRAIL_REQUEST_KEY  JSON key the text goes under (default "input")
    GUARDRAIL_EXTRA_JSON   extra JSON merged into the request (project id, etc.)
    GUARDRAIL_RESULT_PATH  dotted path to the verdict in the response (default "outcome")
    GUARDRAIL_BLOCK_VALUES comma list of verdict values that mean block (default "flagged,blocked")

    Check the mapping against your product's API reference before the show.
    """
    url = os.environ["GUARDRAIL_SCAN_URL"]
    req = {os.environ.get("GUARDRAIL_REQUEST_KEY", "input"): text}
    req.update(json.loads(os.environ.get("GUARDRAIL_EXTRA_JSON", "{}")))
    headers = {"authorization": f"Bearer {os.environ.get('GUARDRAIL_TOKEN', '')}"}
    async with new_client() as client:
        r = await client.post(url, json=req, headers=headers)
    r.raise_for_status()
    node = r.json()
    for part in os.environ.get("GUARDRAIL_RESULT_PATH", "outcome").split("."):
        node = node.get(part) if isinstance(node, dict) else None
    blocked = {v.strip().lower() for v in os.environ.get("GUARDRAIL_BLOCK_VALUES", "flagged,blocked").split(",")}
    if str(node).lower() in blocked:
        return "external_guardrail", f"verdict {node!r}"
    return None


async def scan(text: str, *, check_pii: bool) -> tuple[str, str] | None:
    if BACKEND == "external":
        try:
            return await external_scan(text)
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            # Fail closed: a guardrail you cannot reach should not wave traffic through.
            return "guardrail_unavailable", f"{type(exc).__name__}"
    return heuristic_scan(text, check_pii=check_pii)


def leak_check(status: int, body: bytes):
    if status >= 400 or not CANARIES:
        return None
    text = body.decode("utf-8", "replace")
    for c in CANARIES:
        if c in text:
            return "secret_in_response", f"response contained protected value {c[:3]}***"
    return None


app = base_app(f"guardrail-{MODE}")


@app.api_route("/{path:path}", methods=["GET", "POST"])
async def handle(request: Request, path: str):
    started = time.perf_counter()
    body = await request.body()

    if MODE == "tools":
        if request.url.path.startswith("/actions/"):
            # Actions leaving the cluster are the service-exit door: egress policy's job.
            return await forward(request, UPSTREAM, started, body=body, detail="action passed through (egress policy decides)")

        # Scan the tool *result* on its way back to the agent.
        holder = {}

        def inspect(status: int, content: bytes):
            if status >= 400:
                return None
            holder["text"] = content.decode("utf-8", "replace")
            return heuristic_scan(holder["text"], check_pii=False) if BACKEND != "external" else None

        resp = await forward(request, UPSTREAM, started, body=body, inspect_response=inspect,
                             detail="tool result scanned, clean")
        if BACKEND == "external" and resp.status_code < 400 and holder.get("text"):
            verdict = await external_scan(holder["text"])
            if verdict:
                return block(403, verdict[0], verdict[1], started, direction="response")
        return resp

    # inbound
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        payload = body.decode("utf-8", "replace")
    verdict = await scan(user_text(payload), check_pii=True)
    if verdict:
        return block(400, verdict[0], verdict[1], started, direction="request")
    return await forward(request, UPSTREAM, started, body=body,
                         inspect_response=leak_check if SCAN_RESPONSES else None,
                         detail="prompt scanned, clean" + (" · response checked" if SCAN_RESPONSES else ""))
