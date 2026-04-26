# oxenClaw Security Model

This document describes what oxenClaw protects against, what it does
not, and how the layered defenses fit together. **Read it before
deploying with non-trivial tools or accepting third-party skills.**

## Threat model

### Adversary capabilities considered

- **Malicious skill author** publishes a SKILL.md that tries to social-engineer
  the user (or the agent) into running dangerous commands.
- **Compromised ClawHub account** ships a tampered archive.
- **Unauthorised RPC client** connects to the gateway WebSocket and tries
  to escalate privileges, exfiltrate state, or remote-execute code.
- **Malicious tool** (whether bundled, third-party, or modeled by the
  agent itself in pseudo-code) tries to read host files, exfiltrate
  secrets, or escape into the host shell.
- **Misbehaving tool** consumes unbounded CPU, memory, or wall-clock,
  hanging the gateway.

### Out of scope

- Side-channel attacks (timing, power, electromagnetic, Spectre-class).
- Kernel zero-days that let a sandboxed process break out.
- Compromise of the underlying host OS, hypervisor, or LLM provider.
- Authentication / network-level access controls on the gateway WS port —
  bind to localhost or put a TLS-terminating reverse proxy + auth in front.

## Defenses in depth

### 1. Skill scanner (static)

Implementation: `oxenclaw/security/skill_scanner.py`.

Every SKILL.md fetched from ClawHub is statically scanned **before** the
files are copied into the install directory. The scanner emits findings
with severity `info` / `warn` / `critical`.

A non-empty `critical` set blocks installation by default. Pass
`allow_critical_findings=True` (RPC) or `--force` (CLI, future) to
proceed anyway.

Caught patterns include `curl … | bash`, `rm -rf /`, env-var
exfiltration, dynamic-eval constructs, base64/hex blobs, references to
SSH/AWS/Docker credential files, reverse-shell shapes, and install
specs with non-HTTPS URLs or raw IPs.

**This is a regex-driven static check**: it catches the most common red
flags but does not "understand" the SKILL.md. A determined adversary can
evade it. It is a first line of defense, not the last.

### 2. Archive safety

Implementation: `oxenclaw/clawhub/installer.py`.

- **SHA256 integrity** is recomputed locally and compared to what
  ClawHub reported.
- ZIP entries with absolute paths or `..` segments are rejected
  pre-extract.
- The install target path is resolved and refused if it would land
  outside `~/.oxenclaw/skills/`.
- Slugs must match `^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$` — no slashes,
  no shell metacharacters.

### 3. Tool execution isolation

Implementation: `oxenclaw/security/isolation/`.

Tools that wrap shell commands (`ShellTool`) or invoke a Python callable
in a separate process (`IsolatedFunctionTool`) run under one of four
backends, picked **strongest-first** by availability:

| Backend     | What it gives you                                   | When it's picked |
|-------------|-----------------------------------------------------|-----------------|
| `container` | docker/podman: full namespace+cgroup isolation, network=none, read-only fs, dropped caps | docker or podman on `$PATH` |
| `bwrap`     | bubblewrap: mount + user namespace, tmpfs `/tmp`, read-only host bind, optional `--share-net` | `bwrap` on `$PATH` |
| `subprocess`| fresh process + `RLIMIT_AS/CPU/FSIZE/NOFILE`, scrubbed env, wall-clock timeout | always on POSIX |
| `inprocess` | wall-clock timeout + output truncation only         | fallback / pinned |

The strongest available backend is picked unless the policy pins one
explicitly.

#### What each backend protects against

- **container** — file system writes, network exfiltration, fork-bomb,
  memory exhaustion, capability abuse.
- **bwrap** — same as container but with no kernel-level cgroup limits
  and lighter cold-start cost; still blocks file writes outside `/tmp`
  and (by default) network.
- **subprocess** — runaway CPU/memory/file size + wall-clock time;
  scrubs `$HOME`, `$USER`, secrets from env (only allowlisted vars
  pass through).
- **inprocess** — only wall-clock + output truncation. **Use only for
  trusted built-in code.**

#### What no backend protects against

- The agent (LLM) instructing the user to run a dangerous command
  outside any tool — that's what the skill scanner is for.
- Tools that the operator explicitly registered as `FunctionTool`
  (in-process Python). Those bypass isolation by design — register them
  only for code you wrote.

### 4. Approval gate

Implementation: `oxenclaw/approvals/`.

Wrap any tool with `gated_tool(tool, manager=approvals)` to require a
human approve every invocation. The agent gets a "denied" string back if
the operator declines or the request times out. Use this for any tool
that touches the network or the local filesystem in a way you can't
fully bound with policy.

### 5. ClawHub auth

If a skill's archive endpoint requires a token, set it via
`$CLAWHUB_TOKEN` or `~/.config/clawhub/config.json`. Never hard-code in
configs you check in.

### 6. Outbound network egress (`security/net/`)

The shared `NetPolicy` is the single chokepoint for *every* outbound
HTTP/WS request — `aiohttp` (web tool, MCP HTTP transport), Playwright
(BR-1 browser tool), and any future tool. Layers:

- **L1 SSRF preflight** (`assert_url_allowed`): scheme / port / host
  pattern + IP-literal classification (loopback, RFC1918, link-local,
  CGNAT, IPv6 ULA, mapped/sixtofour/teredo).
- **L2 DNS pinning** (`PinnedResolver` for aiohttp, `HostPinCache` for
  the browser route handler): hostname resolves to its first-seen IPs;
  later disjoint resolutions raise `RebindBlockedError`.
- **L3 audit** (`OutboundAuditStore`, opt-in via
  `OXENCLAW_AUDIT_OUTBOUND=1`): every request → WAL sqlite at
  `~/.oxenclaw/outbound-audit.db`.
- **L4 webhook ingress guards** (`webhook_guards`): body-size limiter,
  fixed-window rate limiter, constant-time HMAC verifier.

### 7. Browser sandbox (BR-1)

`oxenclaw/browser/` adds a fifth layer specific to Chromium:
`--proxy-server=http://0.0.0.0:1` is set at launch so any request that
escapes Playwright's `context.route("**/*", …)` interception still dies
at the OS network layer. `BrowserPolicy.closed()` is fully closed —
`https`-only, no loopback, no private network, empty hostname allowlist
— and operators must opt in per skill. Full surface in
[`BROWSER.md`](./BROWSER.md).

### 8. Canvas iframe sandbox (CV-1)

Canvas HTML lands inline (`srcdoc=`) inside an iframe declared as
`sandbox="allow-scripts allow-pointer-lock allow-forms"` — explicitly
**without** `allow-same-origin`. Even though the iframe lives on the
dashboard origin, agent JS cannot read parent cookies, localStorage, or
`document.domain`. `canvas.navigate` only accepts `data:` URIs and
`about:blank`; any `http(s)://` is refused server-side. HTML payloads
are capped at 256 KiB tool-side / 1 MiB at the RPC edge. Full surface
in [`CANVAS.md`](./CANVAS.md).

## Choosing a policy for a new tool

When you add a new tool, pick the *minimum* privileges it needs.

| Tool kind | Recommended start |
|---|---|
| Read-only computation (math, time, format) | `FunctionTool` (in-process). No isolation needed. |
| Reads filesystem | `IsolatedFunctionTool` with `bwrap` (`filesystem="readonly"`). |
| Writes filesystem | `IsolatedFunctionTool` or `ShellTool` with `bwrap` + scratch tmpfs only. |
| Calls external HTTP | `ShellTool` or `IsolatedFunctionTool` with `network=True`, `timeout_seconds≤10`, behind `gated_tool` if it sends data. |
| Runs arbitrary user code (e.g., `python_snippet_tool`) | `container` backend. Always behind `gated_tool` for production. |
| Anything else network-or-fs touching | Default to `container` + approval gate. |

Default `IsolationPolicy()`:

```
timeout_seconds = 30
max_output_bytes = 1 MiB
max_memory_mb = 512
max_cpu_seconds = 30
max_file_size_mb = 64
max_open_files = 64
network = False           ← deny by default
filesystem = "none"        ← scratch only
backend = None             ← strongest available
```

## Operator checklist

Before exposing the gateway port beyond `localhost`:

1. Front it with TLS + an auth proxy. The gateway speaks plain WS.
2. Set `CLAWHUB_TOKEN` if you publish private skills.
3. Install `bwrap` on the host (`apt install bubblewrap` or distro equivalent).
4. Install `docker` or `podman` if you intend to register tools that
   handle untrusted input (e.g., snippet evaluators).
5. Audit `agents.list` and `channels.list` over the WS RPC — make sure
   only the agents and channels you expect are loaded.
6. Wrap any tool that you wouldn't run yourself with `gated_tool()`.
7. Periodically review installed skills with `skills.list_installed`.
   Uninstall anything you don't recognise.

## Reporting vulnerabilities

This is a personal-project port; there is no formal disclosure pipeline.
File an issue or contact the maintainer directly. Include reproduction
steps and which backends are affected.
