# sampyClaw

Python port of [openclaw](https://github.com/openclaw/openclaw) — the personal, multi-channel AI assistant gateway.

**Status:** Early work in progress. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the reference architecture extracted from openclaw, and [`docs/PORTING_PLAN.md`](docs/PORTING_PLAN.md) for the phased roadmap (D → B → A).

## Scope

Ports the server/CLI side of openclaw: gateway (JSON-RPC over WebSocket), plugin SDK, plugin loader, channel extensions, agent harness, config/credentials.

Native mobile/desktop apps (`apps/ios`, `apps/android`, `apps/macos`, `Swabble/`) and the web UI (`ui/`) are **not** ported — they're platform-bound.

## Current phase

**Phase B** — porting the Telegram extension end-to-end as the pattern pilot. See `docs/PORTING_PLAN.md §Phase B`.

## Requirements

- Python 3.11+
- `uv` or `pip`

## Layout

```
sampyclaw/
├── plugin_sdk/        # public plugin contract (mirrors src/plugin-sdk/)
├── gateway/           # JSON-RPC/WebSocket server (mirrors src/gateway/)
├── plugins/           # plugin loader, registry
├── channels/          # channel abstraction
├── agents/            # agent harness
├── cli/               # `sampyclaw` command
├── config/            # config + credentials
└── extensions/
    └── telegram/      # pilot extension
docs/
├── ARCHITECTURE.md
└── PORTING_PLAN.md
tests/
```
