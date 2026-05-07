# Worked example: Yahoo Finance via MCP

A 5-minute walkthrough that connects oxenClaw to the third-party
[`yfmcp`](https://pypi.org/project/yfmcp/) Yahoo Finance MCP server
through stdio. Verified end-to-end against a fresh
`pip install -e .` venv on 2026-04-26.

The point: you already have a working escape hatch for "someone else
wrote the integration as an MCP server" without reimplementing it as a
oxenClaw skill. Configure once, the tools appear in every agent.

## 1. Install the MCP server

`yfmcp` is a Python MCP server that wraps `yfinance`. Install it in
its own venv so its `pandas`/`matplotlib`/`yfinance` tree doesn't
mingle with oxenClaw's runtime:

```bash
python3 -m venv ~/yfmcp-venv
~/yfmcp-venv/bin/pip install yfmcp
```

This puts a `yfmcp` console script at `~/yfmcp-venv/bin/yfmcp`. The
binary speaks MCP over stdio when launched with no args — that's all
oxenClaw needs.

## 2. Register the server

Add an entry to `~/.oxenclaw/mcp.json` using the standard
Claude-Desktop / `mcp-cli`-compatible shape:

```json
{
  "mcpServers": {
    "yfinance": {
      "command": "/home/you/yfmcp-venv/bin/yfmcp",
      "args": [],
      "transport": "stdio"
    }
  }
}
```

`transport: "stdio"` is implied by the absence of `url`, but writing
it explicitly keeps the file readable.

## 3. Confirm oxenClaw connects

```bash
python -c "
import asyncio
from oxenclaw.pi.mcp.loader import build_pool_from_config
from oxenclaw.pi.mcp.adapter import materialize_mcp_tools

async def main():
    pool = build_pool_from_config()
    tools = await materialize_mcp_tools(pool)
    print(f'{len(tools)} tools, failures={dict(pool.failures)}')
    for t in tools:
        print(' ', t.name)
    await pool.close()

asyncio.run(main())"
```

Expected output:

```
6 tools, failures={}
  yfinance__yfinance_get_financials
  yfinance__yfinance_get_price_history
  yfinance__yfinance_get_ticker_info
  yfinance__yfinance_get_ticker_news
  yfinance__yfinance_get_top
  yfinance__yfinance_search
```

The `<safe_server>__<safe_tool>` mangling is the de-collision rule
documented in `docs/AUTHORING_SKILLS.md`. It guarantees `yfinance__*`
names never clash with oxenClaw's built-in `web_fetch`, `weather`, etc.

## 4. Direct call (no LLM)

`yfmcp.get_ticker_info(symbol)` returns rich JSON for any Yahoo
symbol. Run it directly through the MCP client to confirm the round
trip works:

```python
import asyncio, json
from oxenclaw.pi.mcp.loader import build_pool_from_config
from oxenclaw.pi.mcp.adapter import materialize_mcp_tools

async def main():
    pool = build_pool_from_config()
    tools = {t.name: t for t in await materialize_mcp_tools(pool)}
    out = await tools["yfinance__yfinance_get_ticker_info"].execute(
        {"symbol": "005930.KS"}
    )
    data = json.loads(out)
    print(data["regularMarketPrice"], data["currency"], data["previousClose"])
    await pool.close()

asyncio.run(main())
```

Verified output (Samsung Electronics, 2026-04-26):

```
219500.0 KRW 224500.0
```

Cross-checked against `web_fetch` on Yahoo's chart endpoint — same
numbers, same upstream source.

## 5. Wire into an agent

```python
from oxenclaw.agents.factory import build_agent, load_mcp_tools

mcp_tools, pool = await load_mcp_tools()
agent = build_agent(
    agent_id="default",
    provider="local",        # or "pi", "anthropic", "vllm"
    model="gemma4-fc",       # see docs/OLLAMA.md for the Modelfile build;
                             # plain "gemma4:latest" never emits tool_call
                             # blocks so MCP tools end up unused on it.
    mcp_tools=mcp_tools,
)
# ... agent.handle(...) ...
if pool is not None:
    await pool.close()
```

The 6 yfinance tools are now in the agent's registry alongside
`web_fetch`, `weather`, etc. No code change to the gateway required —
restart `oxenclaw gateway start` after editing `mcp.json` and the
tools materialize on boot.

## Caveats from this run

- **`yfmcp.get_ticker_info` returns 30+ KB of JSON per call.** Small
  open models (gemma4:e4b @ 4B params, qwen3.5:9b) loop on extraction
  rather than synthesise — they keep re-calling the same tool instead
  of picking out `regularMarketPrice`. Use a larger model (≥12 B
  params, or a frontier hosted model) when chaining MCP tools that
  return wide payloads, or write a thin oxenClaw wrapper tool that
  returns only the 3–4 fields the model needs.
- **Stdio servers spawn one subprocess per gateway boot.** `yfmcp`
  startup is ~1 s; oxenClaw connects lazily on first use. Heavy MCP
  servers benefit from `connection_timeout_ms` overrides in
  `mcp.json`.
- **`yfinance` itself rate-limits.** Repeat calls during a soak test
  start returning 429s after a few hundred requests. This is upstream;
  oxenClaw's MCP client surfaces the error string and the next call
  succeeds when the rate window rolls.
- **No credentials needed.** `yfmcp` uses public Yahoo endpoints. If
  you switch to a paid market-data MCP server, set the API key via
  `env` in `mcp.json` — keys are passed to the subprocess after the
  dangerous-env strip pass (`LD_PRELOAD`, `PATH`, `PYTHONPATH`, …).

## Where to go next

- **More servers**: PyPI / npm have MCP servers for GitHub, Linear,
  Slack, Notion, Postgres, filesystem sandboxes, web scraping. The
  `mcp.json` file accepts as many entries as you want; oxenClaw
  connects them all and reports per-server failures via
  `pool.failures` so a broken server never blocks the others.
- **Wrap with `gated_tool`**: if an MCP tool can mutate state (file
  writes, posting to Slack), wrap the proxy returned by
  `materialize_mcp_tools` in `gated_tool(...)` to add approval prompts
  before execution.
- **Server-side**: exposing oxenClaw's own tools to other MCP clients
  is the M2 phase (not yet implemented — see `docs/SUBSYSTEM_MAP.md`).
