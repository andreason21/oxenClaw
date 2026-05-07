# Canvas tools (CV-1)

oxenClaw exposes a *dashboard-only* canvas surface so an agent can
render HTML, charts, mini-games, and ad-hoc visualizations on the
sidebar of the user's dashboard tab. There is no native node, no
external URL fetch, and no separate canvas server — all output lives
inside a sandboxed iframe on the same dashboard page the operator is
already authenticated to.

## Why dashboard-only

openclaw's `extensions/browser/canvas-host` ships an HTTP server on
port 18793 + a Tailscale-aware bridge so connected Mac/iOS/Android
apps can render canvas content in their WebView. oxenClaw has no
native node app, so that whole rig has nowhere to land. The natural
target is the SPA dashboard already served on port 7331.

Benefits of this collapsed model:

- **Zero new ports.** No firewall change, no tailnet config.
- **Zero external resources.** The `srcdoc=` iframe receives the HTML
  inline; no `http://` fetch leaves the host.
- **Single auth boundary.** The dashboard already gates on
  `OXENCLAW_GATEWAY_TOKEN`; canvas inherits the same gate.
- **No background watcher / live-reload.** State lives in
  `CanvasStore` in process memory — a new `canvas_present` IS the
  reload.

## Empirical model gate (gemma4:latest)

Before designing the port we ran a 25-call probe at temperature=0
against `gemma4:latest` over ollama:

| prompt | tool_call | right tool | args ok | html valid | avg latency |
|---|---|---|---|---|---|
| simple card | 5/5 | 5/5 | 5/5 | 5/5 | 17 s |
| SVG bar chart | 5/5 | 5/5 | 5/5 | 5/5 | 41 s |
| tic-tac-toe | 5/5 | 5/5 | 5/5 | 5/5 | 73 s |
| pricing table | 5/5 | 5/5 | 5/5 | 5/5 | 66 s |
| `hide` | 5/5 | 5/5 | 5/5 | n/a | 1.7 s |

25/25. The model picks the right tool, emits parseable JSON, and
generates well-formed self-contained HTML reliably.

`tests/integration/test_canvas_ollama.py` is the durable form of this
gate: run with `OLLAMA_INTEGRATION=1 pytest tests/integration/`.
Default suite skips it.

## Architecture

```
LLM ─tool→ canvas_present(html, title)
              │
              ▼
         CanvasStore  (last state per agent, LRU 16)
              │
              ▼
         CanvasEventBus  (asyncio fanout, bounded queues)
              │
              ▼
   GatewayServer.broadcast(EventFrame)
              │
              ▼
        Dashboard SPA
              │
              ▼
   <iframe sandbox="allow-scripts" srcdoc="…">
```

- `oxenclaw/canvas/store.py` — `CanvasStore`: per-agent latest
  state with LRU eviction. Bounded by `capacity` + an absolute 1 MiB
  per-payload ceiling enforced in the RPC layer.
- `oxenclaw/canvas/events.py` — `CanvasEventBus`: pub/sub with
  bounded subscriber queues; publisher never blocks on slow consumers.
  Also tracks `canvas.eval` request/response futures.
- `oxenclaw/gateway/canvas_methods.py` — six RPCs:
  `canvas.{present,navigate,hide,eval,eval_result,get_state}`.
- `oxenclaw/cli/gateway_cmd.py` — `_pump_canvas_events`: a
  background task that drains the bus and fans events out via
  `GatewayServer.broadcast` as `CanvasEventFrame`s.
- `oxenclaw/static/app.{html,css,js}` — right-side
  `canvas-panel` drawer + `bindCanvasPanel()` event handler that
  renders incoming `present` events into an `<iframe sandbox srcdoc>`.

## Security model

- The iframe is sandboxed with `allow-scripts allow-pointer-lock
  allow-forms` — explicitly **without** `allow-same-origin`. So even
  though the iframe loads from the dashboard origin, agent JS cannot
  read parent cookies / localStorage / `document.domain`.
- `canvas.navigate` only accepts `data:` URIs and `about:blank`.
  Any `http(s)://` is refused server-side, so the canvas can never be
  used to point the dashboard at an attacker URL.
- HTML is delivered inline via `srcdoc`, never fetched. No external
  CSS/JS/font loads happen unless the agent embedded them in the HTML
  itself, and the iframe sandbox blocks them anyway.
- `canvas_present` payloads are capped at 256 KiB by the tool +
  1 MiB by the RPC layer — well below context-window damage but more
  than enough for a real chart or mini-game.
- `canvas_eval` is opt-in (not in `default_canvas_tools`) and
  requires the skill author to wire a `message` listener in their
  presented HTML.

## Tools

`default_canvas_tools(agent_id, store, bus)` returns the bundled
always-safe set:

| Tool | What it does | Cap |
|---|---|---|
| `canvas_present(html, title?)` | Replace the panel with `html` | 256 KiB |
| `canvas_hide()` | Collapse the panel | n/a |

Opt-in (register manually):

| Tool | What it does | Cap |
|---|---|---|
| `canvas_eval(expression, timeout_seconds=5)` | Run JS in the open iframe | 8 KiB result |

## Enabling at the gateway

```bash
export OXENCLAW_ENABLE_CANVAS=1
oxenclaw gateway start --provider local --model gemma4-fc \
  --auth-token "$TOKEN"
```

> `gemma4-fc` is the documented default — a custom Modelfile built on
> top of `gemma4:latest` with a tool-calling chat template. Plain
> `gemma4:latest` works for canvas dispatch (the empirical 25/25 gate
> below was measured against the stock model) but never emits the
> `tool_call` blocks downstream skill / shell flows depend on. Build
> instructions: [`OLLAMA.md` → gemma3 / gemma4 function calling](./OLLAMA.md#gemma3--gemma4-function-calling--full-setup).

`agents.factory._maybe_canvas_tools()` reads `OXENCLAW_ENABLE_CANVAS`
on agent construction and registers the bundle when set. The gateway
itself always exposes `canvas.*` RPCs (the cost of the empty store +
bus is negligible), so even without the env-var the dashboard panel is
ready for any client that decides to drive it directly.

## What we deliberately did not port

- **Tailscale-aware bind / bridge** — single-port collapse made it
  unnecessary.
- **Live-reload watcher (chokidar)** — no disk root; `canvas_present`
  is the reload.
- **a2ui/** (15K LOC bundled UI framework) — the agent emits plain
  HTML; an external UI runtime is a separate concern.
- **`snapshot` action** — `browser_screenshot` from BR-1 covers the
  same use case if the agent loads its own canvas URL (roundtrip
  unnecessary for v1).
