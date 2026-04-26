---
name: canvas
version: 0.1.0
description: |
  Render an HTML page on the user's dashboard canvas panel. Use the
  canvas tool whenever the user asks to show, display, render, draw,
  visualize, or chart something. The HTML lands in a sandboxed iframe
  on the dashboard — no external URL, no node bridge.
gated: true
requires:
  env_marker: OXENCLAW_ENABLE_CANVAS
notes:
  - "Disabled by default. Set OXENCLAW_ENABLE_CANVAS=1 (or config.yaml canvas.enabled: true) to enable."
  - "HTML must be self-contained: do not link external stylesheets, fonts, or scripts."
  - "canvas_eval is NOT bundled by default. Add it explicitly when the skill author has wired a message handler in the presented HTML."
tools:
  - canvas_present
  - canvas_hide
---

# Canvas skill

Use the `canvas_*` tools to render visual output on the user's
dashboard. The dashboard's right-side panel will appear with the HTML
you provide rendered inside a sandboxed iframe.

## When to use

- "Show me a chart of …"
- "Display a card that says …"
- "Render a small game / interactive demo"
- "Draw / visualize / plot …"

## Rules

- Always pass a complete `<!DOCTYPE html>` document.
- Inline all CSS and JS. The iframe sandbox blocks cross-origin loads
  anyway, but inlining keeps the page self-contained and offline-ready.
- Keep the page under 256 KB (the tool refuses larger payloads).
- Do not request access to camera, microphone, geolocation, or
  notifications — the sandbox blocks them.

## When **not** to use

- For text-only answers (just reply normally).
- For images that already exist as URLs (use the message channel).
- To open external sites — the canvas only renders HTML you generated.
