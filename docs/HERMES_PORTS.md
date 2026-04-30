# hermes-agent → oxenClaw porting log

Single-session port of high-ROI patterns from
[`hermes-agent`](https://github.com/NousResearch/hermes-agent) (~567K LOC)
into oxenClaw (~17K LOC). The selection target was **adoptable wins**, not
feature parity — anything that demanded heavy infra (full OAuth refresh,
provider-side stream consumers, Bearer/x-api-key/OAuth multi-shape auth
branching) was deferred. Items #17 (token-delta `GatewayStreamConsumer`),
#18 (persistent OAuth credential pool with single-use refresh-token cross-
process sync) and #23 (Anthropic 5-way auth-shape branching) are
explicitly out-of-scope for this pass.

The session shipped 4 phases with ~234 new tests on top of the existing
1528 → final tally **1762 passed, 0 fail** (one pre-existing `bwrap`
WSL2 sandbox failure deselected — unrelated to these ports). On top of
that, two live integration scripts (`/tmp/live_smoke.py` 36/36 pass,
`/tmp/multiturn_smoke.py` 18/18 pass) exercise the new code end-to-end
through stub providers.

---

## Phase 0 — quick wins (1–2 days each, no schema change)

| # | Item | Files | Why it matters |
|---|------|-------|---|
| 1 | **Frozen recall snapshot** | `oxenclaw/agents/pi_agent.py:_ensure_recall_snapshot` / `invalidate_recall_snapshot` + `oxenclaw/pi/system_prompt.py` priority-25 cacheable contribution | Prefix-cache stays warm across an entire session; mid-session `memory_save` no longer mutates the cacheable system-prompt prefix. Hermes pattern at `tools/memory_tool.py:111-140`. |
| 2 | **Decorrelated jitter + Retry-After** | `oxenclaw/pi/run/run.py:_backoff_delay` + `_resolve_retry_delay`, `oxenclaw/pi/streaming.py:ErrorEvent.{retry_after_seconds,status_code}`, `oxenclaw/pi/providers/anthropic.py:_parse_retry_after` | Per-call `random.Random` seeded by monotonic counter prevents thundering-herd when N sessions hit the same 429 wall. RFC 7231 + provider `x-ratelimit-reset-*` honoured (epoch vs relative auto-detect). Cap 1 h. |
| 3 | **Pool selection strategies** | `oxenclaw/pi/auth_pool.py:PoolStrategy` (`round_robin` default, `fill_first`, `least_used`, `random`) + `report_failure(retry_after_seconds=…)` | `fill_first` keeps the prompt-cache prefix warm on Anthropic. `least_used` spreads metered keys. Per-key cooldown can now use the provider's hint instead of a fixed 60 s ladder. |
| 4 | **Inbound recall-fence sanitize + memory-write threat scan** | `oxenclaw/memory/privacy.py:sanitize_recall_fence` + `scan_memory_threats` (10 patterns + invisible-unicode) wired into `oxenclaw/memory/tools.py:memory_save_tool` and the user-text path in `oxenclaw/agents/pi_agent.py` | Stops a pasted `<recalled_memories>` block from being re-ingested as authoritative; refuses to persist prompt-injection / role-hijack / curl-pipe-bash / RLO-unicode payloads to the long-term store. |
| 5 | **Mid-stream silent retry** | `oxenclaw/pi/run/attempt.py:AttemptResult.text_emitted` + the silent-retry budget in `run.py` | Transport drops mid-stream are silently re-issued only when no user-visible text reached the channel yet. After text streamed → defer to the normal retry path so the user doesn't see duplicate output. |

## Phase 1 — reliability hardening (~234 new tests)

### 1A — error classifier + rate-limit + anti-thrashing

| Module | Surface |
|---|---|
| `oxenclaw/pi/run/error_classifier.py` (new) | `FailoverReason` enum (14 values), `ClassifiedError` dataclass, `classify_api_error(...)`. Distinguishes 413 / 429 / 400-context / 401 / 5xx / `thinking_signature` / 404 / 402 credit / content-policy / connection-with-large-session heuristic / empty stream. |
| `oxenclaw/pi/run/run.py` | Inline retry block replaced by classifier dispatch. `should_rotate_credential` → best-effort `report_failure` to the auth pool. `should_compress` → break to outer iteration so preemptive compaction runs. `should_fallback` → forces the failover chain. `retry_after_seconds` from classifier feeds `_resolve_retry_delay`. |
| `oxenclaw/pi/rate_limit_tracker.py` (new) | `RateLimitState` (frozen), `parse_rate_limit_headers` (anthropic-ratelimit-* + x-ratelimit-* + 1 h variants, epoch vs relative auto-detect), thread-safe `RateLimitTracker.record/peek`, `is_quota_exhausted` heuristic. Hooked into `pi/providers/anthropic.py` via `RuntimeConfig.rate_limit_tracker`. |
| `oxenclaw/pi/compaction.py:CompactionGuard` | Tracks last 3 (before, after) pairs; `decide_compaction` returns `CompactionPlan(needed=False)` when last 2 attempts each saved <10 %. |

### 1B — tools, file safety, command gate

| Module | Surface |
|---|---|
| `oxenclaw/pi/tool_result_storage.py` (new) | `BudgetConfig` with pinned-tool list (`read_file` / `memory_get` / `memory_search` never persisted-redirected), `maybe_persist_tool_result` writes oversize output to `RuntimeConfig.tool_result_storage_dir/{tool_use_id}.txt` and replaces in-context content with a `<persisted-output>` preview + path. `enforce_turn_budget` walks all tool results and persists the largest non-pinned ones until total <200 K chars. |
| `oxenclaw/tools_pkg/fuzzy_match.py` (new) | 8-strategy chain: `exact` → `line_trimmed` → `whitespace_normalized` → `indentation_flexible` → `escape_normalized` → `unicode_normalized` (smart quotes / em-dash / NBSP) → `block_anchor` → `context_aware` (difflib ≥0.92). `detect_escape_drift` refuses writes when `\\'` / `\\"` literal-escapes appear in `old_str` but not the file (catches the JSON-tool-call serialisation drift bug). |
| `oxenclaw/tools_pkg/file_state.py` (new) | Process-wide `FileStateRegistry` keyed by `(task_id, abs_path)` + global `last_writer` map + per-path `threading.Lock`. Hooked into `read_file` / `write_file` / `edit`. `check_stale` returns one of `sibling_wrote` / `mtime_drift` / `write_without_read`; the warning is appended to the tool result so the model can self-correct. |
| `oxenclaw/security/command_gate.py` (new) | 3-tier shell command gate: HARDLINE (unconditional, even YOLO can't bypass — `rm -rf /`, `mkfs`, `dd of=/dev/sd*`, fork bomb, `kill -1`, shutdown/reboot, `>/dev/sda`, `chmod -R 777 /`, `curl…|sh`, `eval $(curl…)`) → DANGEROUS (per-session approval — `git push --force`, `git reset --hard`, broad `chmod -R`, `npm publish`, `rm -rf <user_path>`) → ok. `_CMDPOS` anchor matches command-position only so `echo "rm -rf /"` and `grep "shutdown" log` don't false-positive. |

## Phase 2 — quality leap

| Module | Surface |
|---|---|
| `oxenclaw/pi/compaction.py` (extended) | `llm_structured_summarizer(messages, *, summarizer_llm, prior_summary)` driving a 12-section template (Active Task / Goal / Constraints / Completed Actions / Active State / In Progress / Blocked / Key Decisions / Resolved Questions / Pending User Asks / Relevant Files / Remaining Work / Critical Context). Plus `_summarize_tool_result` (per-tool one-liner — covers 30+ oxenclaw tools), `_truncate_tool_call_args_json` (recursive string-leaf shrink, JSON-valid), `_dedup_tool_results_by_md5` (newest-wins), `_sanitize_tool_pairs` (orphan tool_result drop / stub for missing call), `_align_boundary_backward`, `_ensure_last_user_message_in_tail` (port of hermes #10896 active-task anchor). Wired into `pi_agent._build_summarizer` — `truncating_summarizer` stays as fallback when `RuntimeConfig.auxiliary_llm` is `None`. |
| `oxenclaw/pi/run/run.py` (compress-then-retry) | `compress_then_retry` branch fires only on `CONTEXT_OVERFLOW` / `PAYLOAD_TOO_LARGE`, capped at `RuntimeConfig.max_compression_self_heals` (default 3 — openclaw `compressionSelfHealMax`). `_maybe_record_long_context_tier` sets a session-scoped `force_200k_context` flag when the provider message hints at the 200 K tier. |
| `oxenclaw/security/checkpoint.py` (new) | Shadow-git `CheckpointManager` at `~/.oxenclaw/checkpoints/{sha256(abs_dir)[:16]}/` with `GIT_DIR` + `GIT_WORK_TREE` separation. **`GIT_CONFIG_GLOBAL=/dev/null` + `GIT_CONFIG_SYSTEM=/dev/null` + `GIT_CONFIG_NOSYSTEM=1`** isolation prevents the operator's `commit.gpgsign` / pinentry / credential-helper hooks from interrupting mid-session — direct port of the hermes operational scar. `_validate_commit_hash` rejects `-`-prefixed and shell-meta; `_validate_file_path` rejects traversal escape. |
| `oxenclaw/memory/provider.py` (new) | `MemoryProvider` ABC (`name`, `initialize`, `system_prompt_block`, `prefetch`, `sync_turn`, `get_tool_schemas`, `handle_tool_call`, `shutdown` + optional `on_pre_compress` / `on_session_end` / `on_memory_write` / `on_delegation`). `BuiltinMemoryProvider` wraps the existing `MemoryRetriever`. `MemoryProviderRegistry` enforces the single-external-provider rule. `on_pre_compress` hook fires inside the structured summariser pipeline so a cloud provider can extract insights from doomed turns into durable storage. |

## Phase 3 — ecosystem

| Module | Surface |
|---|---|
| `oxenclaw/pi/models_dev.py` (new) + `oxenclaw/data/models_dev_snapshot.json` | 4-step cascade: in-memory TTL → fresh disk cache → network → stale disk → bundled 16-model snapshot (Claude 4.7/4.6, GPT-5/4o, Gemini 2.5/2.0, DeepSeek, Qwen3). `get_model_capabilities` normalises `context_window`, `max_output`, `supports_tools`, `supports_attachments`, `supports_reasoning`, `family` across providers. `CONTEXT_PROBE_TIERS = (256_000, 128_000, 64_000, 32_000, 16_000, 8_000)` for unknown models. `RemoteModelRegistry(InMemoryModelRegistry)` opt-in via `OXENCLAW_USE_MODELS_DEV=1`. |
| `oxenclaw/pi/account_usage.py` (new) + `oxenclaw/gateway/usage_methods.py:usage.account` RPC | `AccountUsageWindow(label, used_percent, reset_at, detail)` + `AccountUsageSnapshot(provider, windows, extra_credit)`. `fetch_anthropic_usage`, `fetch_openrouter_usage`, dispatching `fetch_account_usage`. The gateway RPC walks the auth pool and returns one snapshot per configured provider for dashboard "Opus week: 47 % used, resets in 3 d 4 h" style displays. |
| `oxenclaw/clawhub/sources/{base,github,clawhub,index}.py` (new) + `parallel_search.py` | `SkillSource` ABC. `GitHubSource` ships default taps `openai/skills` + `anthropics/skills` + `VoltAgent/awesome-agent-skills`. `ClawHubSource` wraps the existing `MultiRegistryClient`. `IndexSource` reads a JSON catalog from `OXENCLAW_SKILL_INDEX_URL` with 6 h TTL + `~/.oxenclaw/skills/index-cache/.ignore` so ripgrep skips potentially-adversarial cached text. `parallel_search_sources` fans out via `ThreadPoolExecutor`, per-source 4 s timeout, dedup by trust rank. When the IndexSource is fresh it short-circuits remote fetches. |
| `oxenclaw/clawhub/activation.py` (new) + `preprocessing.py` (new) + `loader.py` (extended) | `detect_skill_slash_command("/weather seoul tomorrow") → ("weather", "seoul tomorrow")`. `build_skill_invocation_message(slug, user_instruction, paths)` loads SKILL.md, prepends an `[IMPORTANT: user invoked the "{slug}" skill]` activation note, appends absolute skill-dir + supporting-file paths, runs `preprocess_skill_body` to substitute `${OXENCLAW_SKILL_DIR}` / `${OXENCLAW_SESSION_ID}` (opt-in inline shell with 4 KB / timeout caps). `loader.py` reads `~/.oxenclaw/skills_config.{json,yaml}` for `disabled` / `platform_disabled` / `external_dirs` and `platforms:` frontmatter filtering. |
| `oxenclaw/gateway/restart.py` (new) + `gateway/server.py` | `GATEWAY_SERVICE_RESTART_EXIT_CODE = 75` (BSD `EX_TEMPFAIL`) so `systemd Restart=on-failure` auto-restarts after a clean drain. `gateway.restart` RPC triggers the drain + exit. |
| `oxenclaw/agents/lanes.py` + `dispatch.py` | `BusyPolicy = "block" | "queue" | "interrupt" | "steer"` (default `queue`). When a second `chat.send` arrives for the same `(agent_id, session_key)` while a turn is running, the dispatcher applies the policy. 30-second debounced ack messages surface "agent busy on iter N, current tool: X" to the user. |
| `oxenclaw/channels/router.py` | `_failed_channels` map + background reconnect watcher. Backoff `30 → 60 → 120 → 240 → 300 s` cap, max 20 attempts. `is_auth_error` heuristic (401 / "unauthor" / "forbidden") distinguishes retryable transient drops from auth failures. Surface state via `channels.health` RPC. |

---

## Live verification

`/tmp/live_smoke.py` exercises every Phase 0–3 surface end-to-end with stub
providers and reports 36/36 PASS:

- pool strategies + Retry-After cooldown rotation
- decorrelated jitter (5 distinct samples)
- threat scan multi-kind detection
- recall-fence sanitisation
- error classifier 6-way dispatch
- rate-limit header parsing + quota-exhausted heuristic
- anti-thrashing skip
- 3-layer tool-result spill + pinned-tool pass-through
- fuzzy whitespace + unicode strategies + escape-drift refusal
- file-state sibling-wrote warning
- command-gate hardline / dangerous / ok across 7 representative cases
- structured summariser JSON-safe shrink + per-tool one-liners
- shadow-git snapshot+restore (incl. broken `~/.gitconfig` isolation)
- MemoryProvider single-external rule
- models.dev capability lookup (Claude Opus 4.7 ctx=200 K, Sonnet 4.6 ctx=1 M)
- parallel skill search
- slash-command split + skill body template substitution
- gateway restart exit code = 75
- channel reconnect auth-error heuristic

`/tmp/multiturn_smoke.py` drives the full PiAgent dispatch path through 6
turns + a second session + injected 429 / 413 errors, reporting 18/18
PASS:

- recall snapshot byte-stable across turns 1→6 and survives a
  mid-session `memory_save`
- session 'bob' has its own snapshot, independent of session 'alice'
- `invalidate_recall_snapshot('alice')` is selective
- 429 retry path: 1 fail + 1 success = 2 provider calls, 1 reply visible
- 413 self-heal: compress-then-retry, 1 reply visible
- `ConversationHistory` shows 6 user + 6 assistant entries (internal
  retries do not pollute history)
- system-prompt size delta across 9 calls < 1 KB
- alice messages-count progression `[1, 3, 5, 7, 9, 11]` (monotone)

---

## Explicitly out of scope

- **#17 GatewayStreamConsumer** — token-delta in-place message edits,
  flood-strike backoff, oversize split, "fresh final" repost. Wants a
  per-channel `edit_message` capability that none of the current
  oxenclaw channel adapters expose.
- **#18 OAuth credential pool** — single-use refresh-token cross-process
  sync, per-provider refresh client, `~/.claude/.credentials.json` /
  `~/.qwen/oauth_creds.json` / `gh auth token` external-source seeding,
  `RemovalStep` registry. Adds disk schema + file-locks + a security
  review surface bigger than the rest of the work combined.
- **#23 Anthropic 5-way auth-shape branching** (Kimi-coding /
  Bearer-MiniMax / x-api-key 3rd-party / OAuth-Bearer / standard
  sk-ant-api*) — depends on #18 for the OAuth shape.

These three remain the highest-value expansion targets if and when the
ecosystem (free Anthropic via Claude Code / Codex / Nous Portal) becomes
a coverage requirement.
