# The Prompt Got Past Your WAF: toggle board

A live booth demo for **"The Prompt Got Past Your WAF: A Request-Path Map for AI Security on Kubernetes."**
One malicious prompt goes through a real request path on Kubernetes. Each layer is switched on with a toggle,
and the board shows which hop let it through, which hop stopped it, and how long each hop took.

```
internet → Gateway API (NGINX Gateway Fabric) → Edge WAF → AI gateway → Guardrail → Agent → Model
                                                                                   │
                                                                                   └→ Guardrail (tool results) → Tools
```

![Toggle board during the twist: every ingress layer on, and the agent still emails the customer list](docs/toggle-board.png)

The toggles change the cluster. Flipping a layer patches a Kubernetes Service, so you can run
`make watch` on a second screen and show the audience the selector changing.

## How a toggle works

Each position in the path sits behind a stable **slot Service** (`slot-waf`, `slot-aigw`, `slot-guard`,
`slot-tools`). Each hop forwards to the next slot by name. Turning a layer **off** points that slot's
`spec.selector` at the next layer that is still on, so traffic skips it. Turning it back **on** points the
slot at the layer again. The board waits until the slot's EndpointSlices contain only the new pods before
it reports the toggle as done.

```
kubectl -n prompt-demo get svc -l demo/slot=true -o wide -w
slot-aigw   ...   app.kubernetes.io/component=aigw    # AI gateway on
slot-aigw   ...   app.kubernetes.io/component=guard   # AI gateway off: traffic skips straight to the guardrail
```

Each hop adds a record to an `x-demo-trace` response header on the way back. The board uses those
records to light up the pipeline and fill in the timing table.

## What's in the box

| Hop | Mock stack (default, deterministic) | F5 booth stack (`deploy/overlays/f5-waf-guardrails`) |
|---|---|---|
| Gateway API | NGINX Gateway Fabric | NGINX Gateway Fabric |
| Edge WAF | `waf`: signature matching (SQLi, XSS, traversal, command injection) | relay → F5 WAF for NGINX |
| AI gateway | `aigw`: API keys, token limits per request and per minute, model routing | mock `aigw` (no F5 AI Gateway for this demo) |
| Guardrail (prompts) | `guardrail` inbound: injection and PII detection on the prompt, secret check on the response | same hop, scanning by F5 AI Guardrails |
| Agent | `agent`: system prompt holding a secret, `fetch_url` and `send_email` tools | same |
| Model | `model`: deterministic, overly helpful LLM stand-in | mock, or vLLM via `components/vllm` |
| Guardrail (tool results) | `guardrail` tools mode: scans fetched pages before the agent reads them | same hop, scanning by F5 AI Guardrails |
| Tools | `tools`: a vendor page with a hidden instruction, and an email action | same |

Every hop runs from one image. The `ROLE` environment variable picks which hop a container plays.

The **relay** role is how a product without trace headers joins the demo. The slot selects the relay, the
relay forwards to the product, and the product forwards to the next slot. If the product refuses a request
(400, 401, 403, 406 or 429, with no trace from further downstream), the relay records the block under the
product's name, so the board lights the correct hop.

## Quick start

**On a laptop, no cluster** (good for rehearsing and for building the UI). Needs Python 3.10 or newer;
`make dev` creates a virtualenv in `.venv` and installs the dependencies the first time.

```bash
make dev                                    # board on http://127.0.0.1:9100
BOARD_URL=http://127.0.0.1:9100 make test   # walks the whole booth script
```

**On kind with NGINX Gateway Fabric** (recommended for the booth: everything runs on the laptop, no expo Wi-Fi):

```bash
make kind-up        # builds the image and loads it into a kind cluster
make ngf            # Gateway API CRDs + NGINX Gateway Fabric (check NGF_VERSION first)
make deploy
make board          # http://localhost:8080
make watch          # optional second terminal: slots flipping live
BOARD_URL=http://localhost:8080 make test
```

No NGINX Gateway Fabric? Use `OVERLAY=deploy/overlays/no-gateway make deploy`. The board then calls the
first slot directly.

The board calls the Gateway through NGF's Service for the `demo` Gateway, which is `demo-nginx` in
`prompt-demo`. If your NGF version names it differently, check `kubectl -n prompt-demo get svc` and update
`gatewayUrl` in the board config.

## Running the demo

### Before you present

1. **Connect to the internet.** Use a phone hotspot rather than venue Wi-Fi. F5 AI Guardrails is a hosted
   service and NGINX Plus reports usage to F5. Without a connection both block everything, and the twist
   shows BLOCKED instead of EXFILTRATED.
2. **Start the stack at least a few minutes early.** Check that every pod is running
   (`kubectl -n prompt-demo get pods`; `f5-waf` should be 3/3). The WAF needs about 10 seconds after
   it starts before it's ready.
3. **Open the board:** `make board`, then http://localhost:8080. Optional: `make watch` on a second screen.
4. **Run the booth tests:** `make reset`, then `BOARD_URL=http://localhost:8080 make test`. One Guardrails
   test is flaky (see [docs/f5-overlay-setup.md](docs/f5-overlay-setup.md), section 5); any other failure
   means a step won't work.
5. **Reset:** `make reset` (or press `0`), so every layer is off.
6. **Keep the backup recordings** of Steps 3 to 4 on the hidden slides, in case the connection drops.

### Opening (about 60 seconds)

> Everything you're about to see runs in a Kubernetes cluster on this laptop. *(Point at the pipeline.)*
> This is the path a prompt takes to reach an AI agent: in through the Gateway API, then a WAF, an AI
> gateway, a guardrail, the agent and the model. The agent also has tools: it can fetch web pages and
> send email.
>
> Each layer has a toggle. Switching one off really changes the cluster: Kubernetes reroutes traffic
> around it. I'll send one malicious prompt and turn the defenses on one at a time. The board shows
> which layer let it through, which one stopped it, and how much latency each one added.
>
> The WAF is F5 WAF for NGINX and the guardrail is F5 AI Guardrails, a hosted service. The AI gateway
> is a stand-in. Let's start with everything off.

Then press `0` and `Q` for the hook.

### Steps

Keyboard: **1 to 4** toggle layers · **Q W E R T** fire scenarios · **/** type your own prompt · **0** all off · **9** all on.
What each key does is in [Keyboard reference](#keyboard-reference) below.

| Slide | Do this | Board shows |
|---|---|---|
| 2 · Hook | `0`, then `Q` | LEAKED: the system prompt and `STAFF-40` come back |
| 6 · All layers off | (already there) | Every layer BYPASSED |
| 7 · Step 1: WAF on | `1`, `Q`, then `W` | Prompt still LEAKED, but the SQL injection is BLOCKED by F5 WAF for NGINX |
| 8 · Step 2: AI gateway on | `2`, `E`, `R`, `Q` | No key REJECTED, token flood THROTTLED, injection still LEAKED |
| 9 · Step 3: Guardrails on | `3`, `Q` | BLOCKED by F5 AI Guardrails; per-hop latency in the timing table (includes the round trip to the hosted service) |
| 10 · The twist | `T` | EXFILTRATED: a clean request, every ingress layer let it through, the agent emailed the customer list |
| 11 · Step 4: Tool-result guardrail on | `4`, `T` | BLOCKED on the tool result; `send_email` never runs |
| Between sessions | `/` | Visitors try to beat the guardrail. Phrasings that slip past the prompt scan still get caught by the response check if they leak the secret. The real Guardrails may catch them on the prompt instead. |

Run `make reset` (or press `0`) before each session.

If anyone asks about the AI gateway: it's about 60 lines of Python (`app/demo/aigw.py`) doing API keys,
token limits and model routing, the same kinds of checks a real AI gateway does.

### Keyboard reference

The keys are the same in every overlay; layer names vary (the F5 stack says "F5 WAF for NGINX" where the
mock stack says "Edge WAF"). Keys, labels and scenario prompts are set in `board-config.yaml`, so update
this section if you change them there.

**Layer toggles.** Each press changes the cluster: turning a layer off points its slot Service at the
next layer that's still on, so traffic skips it.

| Key | Layer | What it does when on |
|---|---|---|
| `1` | WAF | Attack signatures and protocol checks at the edge |
| `2` | AI gateway | Checks the API key, enforces token limits, routes the model name |
| `3` | Guardrail: prompts | Scans the prompt on the way in and the model's reply on the way out |
| `4` | Guardrail: tool results | Scans what a tool hands back (such as a fetched web page) before the agent reads it |

**Scenarios.** Each sends one request through the stack.

| Key | Scenario | What it sends | What it shows |
|---|---|---|---|
| `Q` | Direct injection | A prompt telling the agent to ignore its instructions and print its system prompt | The main attack. Only the prompt guardrail (`3`) stops it; otherwise the secret `STAFF-40` leaks. |
| `W` | SQL injection | An order lookup with `' OR 1=1; DROP TABLE users; --` in it | A classic web attack. The WAF (`1`) blocks it: what WAFs are good at. |
| `E` | No API key | A harmless question with no API key | The AI gateway (`2`) rejects it (401). |
| `R` | Token flood | "Summarize this: " repeated 2,200 times | Runaway spend. The AI gateway (`2`) throttles it (429). |
| `T` | Indirect via tool | A harmless request to summarize a vendor's setup guide | The twist. Every inbound layer lets it through, but the fetched page hides an instruction and the agent emails the customer list. Only the tool-result guardrail (`4`) stops it. |

**Presets and input.**

| Key | Does |
|---|---|
| `0` | Every layer off: the starting state for each session (same as `make reset`) |
| `9` | Every layer on |
| `/` | Focus the text box to type your own prompt; Enter sends, Esc leaves the box |

Keys are ignored while you type in the text box, or while Cmd, Ctrl or Alt is held, so browser shortcuts
still work.

## The F5 booth stack

`deploy/overlays/f5-waf-guardrails` keeps the same slots, board and scenarios, and puts real F5 products
in two layers: **F5 WAF for NGINX** and **F5 AI Guardrails** (both guardrail hops). The AI gateway stays
the mock. It runs on kind and passes the booth tests, apart from one flaky Guardrails test.
Setup, gotchas and test notes are in [docs/f5-overlay-setup.md](docs/f5-overlay-setup.md).

```bash
make waf-image waf-load                    # build and load the WAF images (amd64, runs under emulation)
kubectl -n prompt-demo create secret generic nginx-license --from-file=license.jwt=<your NGINX One .jwt>
# fill in deploy/components/f5-guardrails/guardrails.env (from guardrails.env.example)
OVERLAY=deploy/overlays/f5-waf-guardrails make deploy
```

- **The demo needs internet.** F5 AI Guardrails is a hosted service, and NGINX Plus reports usage to F5.
  Both fail closed: without a connection the guardrail blocks everything, so the twist won't show
  EXFILTRATED.
- **The F5 products are wired in through the relay** (see "What's in the box") and the guardrail hop's
  `external_scan`. The WAF lives in `deploy/components/f5-waf`, the Guardrails in
  `deploy/components/f5-guardrails`; add either to any overlay.
- `deploy/overlays/f5-guardrails` is real Guardrails only (mock WAF). `deploy/overlays/f5` adds a real
  F5 AI Gateway and won't deploy until `AIGW_URL` in its `endpoints.env` is filled in.

## A real model

Add `components: [../../components/vllm]` to an overlay. It serves `Qwen/Qwen2.5-1.5B-Instruct` with tool
calling and needs one NVIDIA GPU. A real model doesn't behave the same way every time. Small models usually
leak and follow the poisoned page, but rehearse with the model you'll actually run and keep the backup
recordings on the hidden slides. The agent fetches the page itself if the model answers without calling the
tool, so the tool-poisoning step still gets its content.

## Changing the demo

Everything the audience sees is in the board config (`deploy/*/board-config.yaml`): layer names, colors,
keyboard keys, pipeline labels, scenario prompts and the secret that counts as a leak. Overlays replace the
whole file.

A layer can also use `mechanism: manifest`. Turning it on applies a Kubernetes object (a WAF policy, a route
filter) and turning it off deletes it. Give the board's Role permission for that kind if you use it.

## Security notes

- The mock guardrail is deliberately simple and readable. It is beatable, which makes for good booth
  conversations. It is not a guardrail product.
- The board's ServiceAccount can only read and patch Services in its namespace and read Pods and
  EndpointSlices.
- The poisoned page's "exfiltration" only records an email in the tool server's memory. Nothing leaves
  the cluster. The service exit door (egress policy for agent actions) is on the slides but not toggled in
  this demo.

## Layout

```
app/demo/            all hops (waf, aigw, guardrail, agent, model, tools, relay) + board/
app/demo/board/      toggle board API, drivers (kubernetes, local), static UI
deploy/base/         mock stack + NGINX Gateway Fabric Gateway and routes
deploy/overlays/     no-gateway, f5
deploy/components/   vllm
scripts/dev.sh       laptop mode
tests/               the booth script as tests
```
