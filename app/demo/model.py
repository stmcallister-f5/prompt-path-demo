"""Deterministic stand-in for an LLM behind an OpenAI-compatible API.

It behaves the way a too-helpful model behaves, every time, so the booth demo
never depends on sampling luck or a GPU:

* asked to reveal its instructions, it does (front door)
* handed a tool result with instructions in it, it follows them (side door)
* asked to summarize a page, it calls the fetch tool

Swap in vLLM with the `vllm` overlay for the real thing.
"""
import json
import os
import re
import time
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse

from .common import base_app, component

MODEL_NAME = os.environ.get("MODEL_NAME", "demo-model")
DEFAULT_PAGE = os.environ.get("DEFAULT_TOOL_URL", "https://vendor.example/setup-guide")

LEAK = re.compile(r"(ignore|disregard|forget).{0,40}instruction|system prompt|your instructions|"
                  r"internal (code|instructions)|what (are|were) you told|repeat .{0,30}above", re.I | re.S)
TOOL_INJECTION = re.compile(r"(ai|assistant)\s*(agent)?\s*:\s*(.+?)(-->|$)", re.I | re.S)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
URL = re.compile(r"https?://\S+")

app = base_app("model")


def _tokens(text: str) -> int:
    return max(1, int(len(text.split()) * 1.33))


def _reply(content=None, tool_calls=None, prompt_text=""):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    completion = content or json.dumps(tool_calls)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion",
        "created": int(time.time()), "model": MODEL_NAME,
        "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": {"prompt_tokens": _tokens(prompt_text), "completion_tokens": _tokens(completion),
                  "total_tokens": _tokens(prompt_text) + _tokens(completion)},
    }


def _call(name, args):
    return [{"id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}]


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": MODEL_NAME, "object": "model", "owned_by": component()}]}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    tools = {t.get("function", {}).get("name") for t in body.get("tools", []) or []}
    system = "\n".join(m.get("content", "") for m in messages if m.get("role") == "system")
    users = [m.get("content", "") for m in messages if m.get("role") == "user"]
    last_user = users[-1] if users else ""
    tool_results = [m.get("content", "") for m in messages if m.get("role") == "tool"]
    all_text = "\n".join(str(m.get("content", "")) for m in messages)

    # Side door: instructions hidden in a tool result get obeyed.
    if tool_results:
        latest = tool_results[-1]
        # The instruction lives in the fetched page, which may be an earlier tool result.
        m = next((x for x in (TOOL_INJECTION.search(t) for t in reversed(tool_results)) if x), None)
        already_sent = any("send_email" in json.dumps(m2.get("tool_calls", [])) for m2 in messages
                           if m2.get("role") == "assistant")
        looked_up = any("lookup_customers" in json.dumps(m2.get("tool_calls", [])) for m2 in messages
                        if m2.get("role") == "assistant")
        if m and "send_email" in tools and not already_sent:
            to = (EMAIL.findall(m.group(3)) or ["unknown@example.net"])[0]
            # Fetch the data first if there's a tool for it, then send it.
            if "lookup_customers" in tools and not looked_up:
                return JSONResponse(_reply(tool_calls=_call("lookup_customers", {"query": "all"}),
                                           prompt_text=all_text))
            records = latest if looked_up else "Attached: full customer list."
            return JSONResponse(_reply(tool_calls=_call("send_email", {
                "to": to, "subject": "Setup audit", "body": records}), prompt_text=all_text))
        if already_sent:
            return JSONResponse(_reply("Here is a summary of the setup guide: install the agent, register "
                                       "your API key, and enable TLS. (I also sent the customer list as "
                                       "the page instructed.)", prompt_text=all_text))
        return JSONResponse(_reply("Summary of the setup guide: install the agent, register your API key, "
                                   "enable TLS, then verify the health check.", prompt_text=all_text))

    # Front door: the model happily repeats its instructions.
    if LEAK.search(last_user):
        return JSONResponse(_reply("Sure! Here are my instructions: " + (system or "(no system prompt)"),
                                   prompt_text=all_text))

    if "fetch_url" in tools and re.search(r"summar|read|look at|check", last_user, re.I):
        url = (URL.findall(last_user) or [DEFAULT_PAGE])[0].rstrip(".,)")
        return JSONResponse(_reply(tool_calls=_call("fetch_url", {"url": url}), prompt_text=all_text))

    return JSONResponse(_reply("Happy to help with your order, returns, or sizing questions. "
                               "What can I do for you?", prompt_text=all_text))
