"""System-prompt section catalog + composer."""

from __future__ import annotations

from collections.abc import Iterable

# ── Section catalog ─────────────────────────────────────────────────


DEFAULT_IDENTITY = (
    "You are oxenClaw, a helpful assistant reached via chat channels. "
    "Be concise. Use tools when helpful."
)


TOOL_CALL_BASIC = (
    "Tool calls: emit a real `tool_use` block — do NOT write the tool "
    "call as JSON in your reply text (the runtime auto-fires such "
    "pseudo-calls as a best-effort safety net, but rely on real "
    "tool_use blocks)."
)


# Stronger "act, don't describe" enforcement, ported from
# hermes-agent's TOOL_USE_ENFORCEMENT_GUIDANCE. Injected only for
# non-thinking small models — frontier models (claude, gpt-5,
# gemini-2.x) already follow this without prompting and the extra
# tokens are wasted.
TOOL_USE_ENFORCEMENT = (
    "Tool-use enforcement. You MUST use your tools to take action — do "
    "not describe what you would do without doing it. When you say you "
    "will perform an action ('I will check the file', 'Let me run the "
    "tests'), you MUST emit the corresponding tool_use block in the "
    "SAME response. Never end a turn with a promise of future action — "
    "execute it now. Every response should either (a) contain tool "
    "calls that make progress, or (b) deliver a final result. Pure "
    "intent statements without a tool_use block are not acceptable."
)


# Model-name substrings that get TOOL_USE_ENFORCEMENT injected. Add
# new patterns here when a small-model family needs explicit steering.
# Frontier / thinking models (claude, gpt-5, o1/o3, gemini-2.x,
# qwen-thinking, qwq, deepseek-r1) are NOT in this list — they handle
# tool-use discipline natively.
TOOL_USE_ENFORCEMENT_MODELS: tuple[str, ...] = (
    "qwen3.5",
    "qwen2.5",
    "llama3",
    "llama4",
    "gemma3",
    "gemma4",
    "mistral",
    "phi3",
    "phi4",
    "deepseek-coder",
)


TIME_GUIDANCE = (
    "Time + freshness. You do NOT know the current date or time without "
    "calling a tool. If the user asks about \"now\", \"today\", \"이번 주\", "
    "\"지금\", or any question whose answer depends on the current "
    "date/time, call `get_time` first. Never guess the date."
)


# Memory guidance — declarative-only phrasing rule (lifted from
# hermes-agent: imperative phrasing in saved memories gets re-read as
# directives in later sessions and overrides the user's current
# request). Keep memories as facts about the world / user, never as
# instructions to the agent.
MEMORY_GUIDANCE = (
    "Memory playbook. Long-term facts about the user / project / past "
    "decisions live in a vector-indexed memory store with two tiers: "
    "the raw `inbox` (everything you save) and the curated "
    "`short_term` tier (durable facts you've explicitly promoted).\n"
    "  - `memory_save(text=\"...\", tags=[\"...\"])` — append a stable "
    "fact to the inbox whenever the user asks you to remember "
    "something OR you learn a durable preference (their name, role, "
    "deadline, tooling preference).\n"
    "  - Write memories as DECLARATIVE FACTS, never as instructions "
    "to yourself. \"User prefers concise responses\" ✓; \"Always "
    "respond concisely\" ✗. \"User lives in Suwon\" ✓; \"When asked "
    "for weather, use Suwon\" ✗. Imperative phrasing gets re-read as "
    "a directive in later sessions and can override the user's "
    "current request.\n"
    "  - Write COMPLETE natural-language sentences, not `key:value` "
    "lines. When the user wrote in a non-English language, include "
    "BOTH that language and English so the embedding store hits "
    "cross-language queries.\n"
    "  - Skip ephemeral chat-transcript details. Save WHAT IS, not "
    "WHAT TO DO.\n"
    "  - `memory_search(query, k?)` — explicit recall when the auto-"
    "injected memory block at prompt-time didn't surface what you "
    "need.\n"
    "  - When the user explicitly says \"this is important, don't "
    "forget\" or you've verified a fact across multiple turns, use "
    "`memory.promote` (RPC) to lift the inbox chunk into "
    "`short_term`."
)


SKILLS_GUIDANCE = (
    "Skill discovery + execution. The system prompt's "
    "`<available_skills>` block lists installed skills with their "
    "SKILL.md `<usage>` excerpt. To run an installed skill's "
    "documented script, call the `skill_run` tool — e.g.:\n"
    "  `skill_run(skill=\"stock-analysis\", script=\"analyze_stock.py\","
    " args=[\"005930.KS\"])`\n"
    "(Korean tickers use the `<6-digit>.KS` (KOSPI) / `.KQ` (KOSDAQ) "
    "Yahoo suffix.) Pick the right script + args from the `<usage>` "
    "excerpt; do NOT emit a tool_use block named after the skill — "
    "the registry has no such function and the call will fail.\n"
    "If the user's request implies a domain no installed skill "
    "covers, call `skill_resolver(query=\"...\")` — it searches "
    "ClawHub, installs the best match, then call `skill_run` "
    "afterwards."
)


# Compressed anti-refusal — the auto-injected
# [INSTALLED SKILL DETECTED] prelude on the user message already names
# the matching skill + sample call shape, so the long version of this
# rule is redundant.
ANTI_REFUSAL = (
    "Anti-refusal. When an installed skill in `<available_skills>` "
    "covers the user's domain (stocks → stock-analysis, weather → "
    "weather, etc.) DO NOT reply 'I can't access real-time / live "
    "data'. The skill IS your access. Call `skill_run`; if you don't "
    "know the right script, call it with your best guess and read the "
    "error message — the tool lists every available script."
)


WEATHER_PLAYBOOK = (
    "Weather playbook. For weather / temperature / forecast questions "
    "(\"날씨\", \"weather\", \"forecast\") prefer the dedicated `weather` "
    "tool — do NOT use web_search. Required arg: `city` (string) OR "
    "`lat`+`lon` (numbers). Call shape: `weather(city=\"<city>\")`. If "
    "the user didn't name a city, check the recalled-memories block "
    "first for a location fact. Only ask (\"어느 도시 날씨를 "
    "알려드릴까요?\") when neither the question nor recall reveals one."
)


WEB_RESEARCH_PLAYBOOK = (
    "Web research playbook. For factual / current-events / market-"
    "research questions:\n"
    "  1. Try `web_search` first — ranked URL list.\n"
    "  2. If 0 hits OR snippets aren't enough, do NOT give up — pick "
    "the best URL (or a known authoritative source) and call "
    "`web_fetch` to load the actual page body. A 404 from web_fetch "
    "is data, not a stopping signal.\n"
    "  3. Try alternate query phrasings (English / Korean / `site:` "
    "filters) before reporting nothing was found.\n"
    "  4. Cite the URLs you fetched.\n"
    "  5. NEVER use web_search when a dedicated tool exists "
    "(weather → `weather`, current time → `get_time`)."
)


WIKI_PLAYBOOK = (
    "Wiki playbook. The wiki vault stores durable knowledge that "
    "survives across many sessions (decisions, entities, concepts).\n"
    "  - When the user asks 'what do you know about X?' or 'remember "
    "our decision on Y', call `wiki_search` first.\n"
    "  - If nothing matches AND the user is sharing a new "
    "authoritative claim or decision, propose `wiki_save` — explain "
    "the page (kind, title, body) before calling it."
)


# Per-channel markdown / media hints, ported from hermes-agent's
# PLATFORM_HINTS dict. Keyed by channel id — the gateway passes the
# channel through `build_system_prompt(..., channel=...)`. Channels
# not in this dict (e.g. dashboard, ACP) get no hint and receive the
# default markdown-aware prompt.
PLATFORM_HINTS: dict[str, str] = {
    "slack": (
        "Channel: Slack. Standard markdown is partially supported "
        "(bold/italic/code work; complex tables don't render). Keep "
        "responses scannable — short paragraphs and bullet lists "
        "beat long prose. To deliver a file, include "
        "`MEDIA:/absolute/path/to/file` in your response — the "
        "gateway uploads it as an attachment."
    ),
    "discord": (
        "Channel: Discord. Markdown renders. Code blocks and inline "
        "code work natively. To deliver a file, include "
        "`MEDIA:/absolute/path/to/file` in your response."
    ),
    "telegram": (
        "Channel: Telegram. Standard markdown is converted to "
        "Telegram format: **bold**, *italic*, `code`, ```code "
        "blocks```, [links](url), and ## headers all work. To "
        "deliver media, include `MEDIA:/absolute/path/to/file` — "
        "images send as photos, .ogg as voice bubbles, .mp4 plays "
        "inline."
    ),
    "whatsapp": (
        "Channel: WhatsApp. Do NOT use markdown — it does not "
        "render. Use plain text with line breaks. To deliver media, "
        "include `MEDIA:/absolute/path/to/file` — images appear as "
        "photos, videos play inline, other files arrive as "
        "downloadable documents."
    ),
    "signal": (
        "Channel: Signal. Do NOT use markdown — it does not render. "
        "Plain text with line breaks. To deliver media, include "
        "`MEDIA:/absolute/path/to/file`."
    ),
    "email": (
        "Channel: Email. Use plain text formatting (no markdown). "
        "Keep responses concise but complete. To attach files, "
        "include `MEDIA:/absolute/path/to/file`. Do not include "
        "greetings or sign-offs unless contextually appropriate — "
        "the subject line is preserved for threading."
    ),
}


# ── Composition ─────────────────────────────────────────────────────


def needs_tool_use_enforcement(model_id: str) -> bool:
    """Return True if `model_id` matches a small-model substring that
    benefits from explicit tool-use enforcement.

    Frontier and thinking-capable models are excluded — they handle
    tool-use discipline natively and the extra ~150 tokens are wasted.
    """
    if not model_id:
        return False
    lower = model_id.lower()
    # Explicit deny: thinking/reasoning variants of small models that
    # would otherwise match the substring list (e.g. "qwen3-thinking").
    if any(tag in lower for tag in ("-thinking", "-reasoning", "qwq", "deepseek-r1")):
        return False
    return any(p in lower for p in TOOL_USE_ENFORCEMENT_MODELS)


def build_system_prompt(
    *,
    identity: str = DEFAULT_IDENTITY,
    model_id: str = "",
    tool_names: Iterable[str] = (),
    channel: str | None = None,
    include_skills_section: bool = True,
    include_memory_section: bool = True,
) -> str:
    """Assemble a system prompt from the section catalog.

    Sections are emitted in a stable order so prompt-cache prefixes
    stay aligned across turns. Sections whose backing tool isn't in
    `tool_names` are skipped (smaller context for trimmed deployments).
    Model-family overlays and channel hints are appended at the end so
    the cache-stable prefix stays intact.

    The caller (PiAgent) caches the result and only rebuilds after
    context-compaction events.
    """
    tools = {t for t in tool_names if t}
    parts: list[str] = [identity, TOOL_CALL_BASIC, TIME_GUIDANCE]

    if include_memory_section and "memory_save" in tools:
        parts.append(MEMORY_GUIDANCE)

    if include_skills_section and "skill_run" in tools:
        parts.append(SKILLS_GUIDANCE)
        parts.append(ANTI_REFUSAL)

    if "weather" in tools:
        parts.append(WEATHER_PLAYBOOK)
    if "web_search" in tools:
        parts.append(WEB_RESEARCH_PLAYBOOK)
    if "wiki_search" in tools:
        parts.append(WIKI_PLAYBOOK)

    # Tool-use enforcement only when (a) the model needs steering AND
    # (b) at least one tool is actually registered. With zero tools the
    # "you MUST emit a tool_use block" overlay drives the model to
    # invent fake tools and the step verifier gets stuck rejecting
    # every turn — measured 30+ extra turns and 2× wall time on the
    # 12-task bench (vowel_count / extract_emails / even_squares).
    if tools and needs_tool_use_enforcement(model_id):
        parts.append(TOOL_USE_ENFORCEMENT)

    if channel and channel in PLATFORM_HINTS:
        parts.append(PLATFORM_HINTS[channel])

    return "\n\n".join(parts)
