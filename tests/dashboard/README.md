# Dashboard E2E tests

Headless-browser tests for the bundled SPA at `oxenclaw/static/`. Each
test boots an in-process gateway on a unique port + token and drives a
real Chromium tab against it via Playwright. The shared `page` fixture
listens for `pageerror` and `console.error` events and fails the test if
either fires — so a passing test means **no JS exceptions reached the
console** during the interaction.

## What's covered

The single `test_dashboard_e2e.py` module groups assertions by surface:

| Surface | Tests |
|---|---|
| Sidebar navigation | every `data-route` clickable, view title updates, `g+<letter>` chord |
| Theme toggle | 🌓 cycles `system → light → dark`, choice persists across reload |
| Command palette | Ctrl+K opens, fuzzy filter narrows, Enter runs top match, Esc closes, topbar `⌘` button |
| Keyboard help | Ctrl+/ toggles, close button works |
| Chat view | compose elements present, 📎 file picker → thumbnail rendered |
| Sessions | friendly empty-state when `sessions.*` RPC isn't wired |
| Cron / Approvals | empty-state copy renders |
| Skills | Browse / Installed tab switching |
| Memory | search bar present, Enter triggers a search, empty-state on miss |
| Config | YAML/JSON dump renders |
| RPC log | accumulates frames during navigation |
| Responsive | < 900 px collapses sidebar, hamburger opens drawer, backdrop closes it |

23 tests total. Each owns its own gateway boot — fast to add more
without per-file setup.

## Running

```bash
# 1. Install Playwright + the browser bundle
pip install -e .[dev]
playwright install chromium

# 2. Install the system libraries the bundle needs (one-time, sudo).
#    On Ubuntu 24.04 the missing libs are libnss3 / libnspr4 / libasound2t64.
sudo playwright install-deps chromium

# 3. Run the suite
pytest tests/dashboard/ -v
```

## Auto-skip behaviour

`conftest.py` probes the bundled Chromium binary at collection time
(`<binary> --version`). When the binary is missing or fails to launch
(missing `.so` files), every test in the directory is marked `skip`
with a message naming the exact command to run:

```
SKIPPED [23] tests/dashboard/test_dashboard_e2e.py: dashboard E2E
unavailable: chromium can't launch (… libnspr4.so …). Install system
deps once: `sudo playwright install-deps chromium`
```

This means CI without graphical libs (and dev machines that haven't run
the one-time `apt install`) report a clean skip rather than red errors.

## Adding a new test

Two-line pattern:

```python
async def test_my_button_does_X(page) -> None:
    await page.locator("#some-button").click()
    await page.wait_for_function("...predicate...", timeout=3000)
```

The `page` fixture has already navigated to `?token=<unique>` against a
clean in-process gateway — every test starts in the same blank state.
