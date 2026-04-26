---
name: weather
description: "Look up current weather and a short forecast by city or coordinates. Uses wttr.in (no auth) with open-meteo as fallback."
homepage: https://github.com/oxenclaw
openclaw:
  emoji: "☀️"
---

# weather

Two providers, both free / no-key:

1. `wttr.in` — colourful one-liner format (`?format=3`).
2. `open-meteo` — JSON forecast (used when wttr is down or coords given).

The tool tries wttr first for cities, open-meteo for coords.
