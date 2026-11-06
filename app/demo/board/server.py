"""Toggle board: flips layers in the cluster and fires demo requests through them."""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import time
from collections import deque

import httpx
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..common import read_trace
from .drivers import KubeDriver, LocalDriver, load_manifest

HERE = pathlib.Path(__file__).parent
CONFIG_PATH = os.environ.get("BOARD_CONFIG", str(HERE / "default-config.yaml"))
CFG = yaml.safe_load(pathlib.Path(CONFIG_PATH).read_text())
CFG["gatewayUrl"] = os.environ.get("GATEWAY_URL", CFG["gatewayUrl"])
SETTLE = float(os.environ.get("BOARD_SETTLE_SECONDS", "1.0"))
READY_TIMEOUT = float(os.environ.get("BOARD_READY_TIMEOUT", "30"))
REQUEST_TIMEOUT = float(os.environ.get("BOARD_REQUEST_TIMEOUT", "120"))

if os.environ.get("BOARD_DRIVER", "kubernetes") == "local":
    DRIVER = LocalDriver(os.environ.get("DEVROUTER_URL", "http://127.0.0.1:9000"), CFG["selectorKey"])
else:
    DRIVER = KubeDriver(CFG["namespace"], CFG["selectorKey"], settle=SETTLE)

LAYERS = {l["id"]: l for l in CFG["layers"]}
LOCK = asyncio.Lock()
EVENTS: deque = deque(maxlen=40)
HISTORY: deque = deque(maxlen=12)

app = FastAPI(title="toggle-board", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


def log(msg: str, kind: str = "info") -> None:
    EVENTS.appendleft({"at": time.time(), "msg": msg, "kind": kind})


# ------------------------------------------------------------------ state
async def layer_on(layer: dict) -> bool:
    if layer["mechanism"] == "slot":
        for chain in CFG["chains"]:
            svc = chain["slots"].get(layer["component"])
            if svc:
                return (await DRIVER.get_selector(svc)) == layer["component"]
        return False
    if layer["mechanism"] == "manifest":
        return await DRIVER.manifest_exists(load_manifest(layer))
    return False


async def current_state() -> dict[str, bool]:
    return {lid: await layer_on(l) for lid, l in LAYERS.items()}


def component_on(component: str, state: dict[str, bool], chain: dict) -> bool:
    if component == chain["order"][-1]:
        return True
    for l in LAYERS.values():
        if l["mechanism"] == "slot" and l.get("component") == component:
            return state.get(l["id"], False)
    return True                     # components with no toggle are always in path


async def rewire(state: dict[str, bool]) -> list[dict]:
    """Point every slot at the first component at or after it that is on."""
    changes = []
    for chain in CFG["chains"]:
        order = chain["order"]
        for i, comp in enumerate(order):
            svc = chain["slots"].get(comp)
            if not svc:
                continue
            target = next(c for c in order[i:] if component_on(c, state, chain))
            current = await DRIVER.get_selector(svc)
            if current != target:
                await DRIVER.set_selector(svc, target)
                changes.append({"service": svc, "from": current, "to": target})
    for ch in changes:
        ms = await DRIVER.wait_ready(ch["service"], ch["to"], READY_TIMEOUT)
        ch["ready_ms"] = round(ms)
        log(f"patched service/{ch['service']} selector {CFG['selectorKey']}: {ch['from']} → {ch['to']}"
            f"  (endpoints ready in {ms / 1000:.1f}s)", "change")
    return changes


# ------------------------------------------------------------------ API
class Toggle(BaseModel):
    on: bool


class Run(BaseModel):
    scenario: str = "direct"
    prompt: str | None = None


@app.get("/")
async def index():
    return FileResponse(HERE / "static" / "index.html")


@app.get("/api/state")
async def state():
    try:
        st = await current_state()
        healthy, err = True, None
    except Exception as exc:                       # surfaced on the board, not hidden
        st, healthy, err = {}, False, f"{type(exc).__name__}: {exc}"
    return {
        "title": CFG.get("title"), "driver": DRIVER.describe(), "healthy": healthy, "error": err,
        "layers": [{**{k: v for k, v in l.items() if k != "manifest"}, "on": st.get(l["id"], False)}
                   for l in CFG["layers"]],
        "pipeline": CFG["pipeline"], "toolPipeline": CFG.get("toolPipeline", []),
        "canaries": CFG.get("canaries", []),
        "scenarios": [{k: v for k, v in s.items() if k not in ("apiKey", "repeat")} for s in CFG["scenarios"]],
        "events": list(EVENTS), "history": list(HISTORY),
    }


@app.post("/api/layers/{layer_id}")
async def toggle(layer_id: str, body: Toggle):
    layer = LAYERS.get(layer_id)
    if not layer:
        raise HTTPException(404, f"unknown layer {layer_id}")
    async with LOCK:
        t0 = time.perf_counter()
        if layer["mechanism"] == "manifest":
            m = load_manifest(layer)
            ref = f"{m['kind'].lower()}/{m['metadata']['name']}"
            if body.on:
                await DRIVER.manifest_apply(m)
                log(f"applied {ref}", "change")
            else:
                await DRIVER.manifest_delete(m)
                log(f"deleted {ref}", "change")
            await asyncio.sleep(float(layer.get("settleSeconds", SETTLE)))
            changes = [{"object": ref, "on": body.on}]
        else:
            st = await current_state()
            st[layer_id] = body.on
            changes = await rewire(st)
        log(f"{layer['label']} {'ON' if body.on else 'OFF'} in {(time.perf_counter() - t0):.1f}s")
    return {"layer": layer_id, "on": body.on, "changes": changes}


@app.post("/api/preset/{name}")
async def preset(name: str):
    if name not in ("all-on", "all-off"):
        raise HTTPException(404)
    want = name == "all-on"
    async with LOCK:
        st = {lid: want for lid in LAYERS}
        for l in LAYERS.values():
            if l["mechanism"] == "manifest":
                m = load_manifest(l)
                await (DRIVER.manifest_apply(m) if want else DRIVER.manifest_delete(m))
        await rewire(st)
        log(f"preset: every layer {'ON' if want else 'OFF'}")
    return {"preset": name}


def classify(status: int, data: dict, canaries: list[str]) -> dict:
    err = data.get("error") if isinstance(data, dict) else None
    if status >= 400:
        if isinstance(err, dict) and err.get("type") == "blocked":
            word = {401: "REJECTED", 429: "THROTTLED"}.get(status, "BLOCKED")
            return {"outcome": word, "tone": "good", "by": err.get("blocked_by"), "hop": err.get("hop"),
                    "reason": err.get("reason"), "detail": err.get("detail"), "direction": err.get("direction")}
        for l in CFG["layers"]:                    # attribute blocks from products that don't add trace headers
            sig = l.get("blockSignature")
            if sig and status == sig.get("status", status) and re.search(sig.get("bodyRegex", "."), json.dumps(data)):
                return {"outcome": "BLOCKED", "tone": "good", "by": l.get("label"), "hop": l["id"],
                        "reason": sig.get("reason", "blocked"), "detail": ""}
        return {"outcome": "ERROR", "tone": "warn", "by": None, "reason": f"HTTP {status}",
                "detail": json.dumps(err or data)[:300]}
    reply = ""
    try:
        reply = data["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        pass
    demo = data.get("demo", {}) if isinstance(data, dict) else {}
    if demo.get("actions"):
        a = demo["actions"][0]["args"]
        return {"outcome": "EXFILTRATED", "tone": "bad", "by": None, "reason": "agent sent email",
                "detail": f"to {a.get('to')}: {a.get('subject', '')}"}
    blocked_steps = [s for s in demo.get("steps", []) if s.get("status") == "blocked"]
    if blocked_steps:
        b = blocked_steps[0].get("blocked") or {}
        return {"outcome": "BLOCKED", "tone": "good", "by": b.get("blocked_by"), "hop": b.get("hop"),
                "reason": b.get("reason"), "detail": b.get("detail"), "direction": "tool result"}
    if any(c and c in reply for c in canaries):
        return {"outcome": "LEAKED", "tone": "bad", "by": None, "reason": "system prompt and secret returned",
                "detail": ""}
    return {"outcome": "ANSWERED", "tone": "neutral", "by": None, "reason": "no secret in the reply", "detail": ""}


@app.post("/api/run")
async def run(body: Run):
    sc = next((s for s in CFG["scenarios"] if s["id"] == body.scenario), None)
    if sc is None and body.scenario != "custom":
        raise HTTPException(404, f"unknown scenario {body.scenario}")
    sc = sc or {"id": "custom", "label": "Beat the guardrail", "door": "Your prompt"}
    prompt = body.prompt if body.prompt is not None else sc.get("prompt", "")
    if sc.get("repeat"):
        prompt = prompt + " ".join(["lorem"] * int(sc["repeat"]))
    api_key = sc.get("apiKey", CFG.get("apiKey", ""))
    headers = {"content-type": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    if CFG.get("gatewayHost"):
        headers["host"] = CFG["gatewayHost"]
    payload = {"model": "booth-assistant", "messages": [{"role": "user", "content": prompt}]}
    layers_on = await current_state()

    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, limits=httpx.Limits(max_keepalive_connections=0)) as c:
            r = await c.post(CFG["gatewayUrl"].rstrip("/") + "/v1/chat/completions", json=payload, headers=headers)
        total_ms = (time.perf_counter() - t0) * 1000
        status, trace = r.status_code, read_trace(r.headers)
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text[:500]}
    except httpx.HTTPError as exc:
        total_ms, status, trace, data = (time.perf_counter() - t0) * 1000, 0, [], {}
        result = {"outcome": "ERROR", "tone": "warn", "by": None, "reason": "gateway unreachable",
                  "detail": f"{type(exc).__name__}: {exc}"}
    else:
        result = classify(status, data, CFG.get("canaries", []))

    reply = ""
    if isinstance(data, dict) and data.get("choices"):
        reply = data["choices"][0]["message"].get("content") or ""
    steps = data.get("demo", {}).get("steps", []) if isinstance(data, dict) else []
    out = {**result, "scenario": sc["id"], "label": sc["label"], "door": sc.get("door"), "prompt": prompt[:400],
           "status": status, "total_ms": round(total_ms), "trace": trace, "reply": reply,
           "steps": [{k: v for k, v in s.items()} for s in steps], "layers": layers_on, "at": time.time()}
    HISTORY.appendleft({k: out[k] for k in ("label", "outcome", "tone", "by", "total_ms", "layers", "at")})
    return JSONResponse(out)


@app.get("/healthz")
async def healthz():
    return {"ok": True}
