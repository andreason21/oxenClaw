"""`oxenclaw gateway start` — full composition.

Discovers plugins via entry points, loads their accounts from config +
credentials, wires a generic ChannelRouter + per-binding ChannelRunner
supervisors, starts the cron scheduler and approval manager, and registers
the full RPC surface on the WebSocket gateway. All pieces compose through
the plugin/channel SDK — this CLI module contains no channel-specific
special cases.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal

import typer

from oxenclaw.agents import (
    Agent,
    AgentRegistry,
    Dispatcher,
    UnknownProvider,
)
from oxenclaw.agents.factory import build_agent as _build_agent
from oxenclaw.approvals import ApprovalManager
from oxenclaw.canvas import (
    get_default_canvas_bus,
    get_default_canvas_store,
)
from oxenclaw.channels import ChannelRouter, ChannelRunner
from oxenclaw.clawhub import ClawHubClient, MultiRegistryClient, SkillInstaller
from oxenclaw.clawhub.registries import ClawHubRegistries
from oxenclaw.config import default_paths, load_config
from oxenclaw.cron import CronJobStore, CronRunStore, CronScheduler
from oxenclaw.gateway import (
    ChatSendParams,
    ChatSendResult,
    GatewayServer,
    Router,
)
from oxenclaw.gateway.agents_methods import register_agents_methods
from oxenclaw.gateway.approval_methods import register_approval_methods
from oxenclaw.gateway.bind_policy import (
    RemoteBindRefused,
    validate_bind_host,
)
from oxenclaw.gateway.canvas_methods import register_canvas_methods
from oxenclaw.gateway.channels_methods import register_channels_methods
from oxenclaw.gateway.chat_methods import register_chat_methods
from oxenclaw.gateway.config_methods import register_config_methods
from oxenclaw.gateway.cron_methods import register_cron_methods
from oxenclaw.gateway.isolation_methods import register_isolation_methods
from oxenclaw.gateway.memory_methods import register_memory_methods
from oxenclaw.gateway.skills_methods import register_skills_methods
from oxenclaw.gateway.plan_methods import register_plan_methods
from oxenclaw.gateway.usage_methods import register_usage_methods
from oxenclaw.memory import MemoryRetriever, OpenAIEmbeddings
from oxenclaw.plugin_sdk.channel_contract import (
    ChannelTarget,
    InboundEnvelope,
    MonitorOpts,
)
from oxenclaw.plugin_sdk.runtime_env import get_logger
from oxenclaw.plugins import discover_plugins

app = typer.Typer(help="Run the gateway server.", no_args_is_help=True)

logger = get_logger("cli.gateway")


@app.command("start")
def start(
    host: str = typer.Option(
        "127.0.0.1",
        help=(
            "Bind host. Loopback only by default — oxenClaw refuses to "
            "expose the gateway beyond the local machine unless you also "
            "pass --allow-non-loopback (or set OXENCLAW_ALLOW_NON_LOOPBACK=1)."
        ),
    ),
    allow_non_loopback: bool = typer.Option(
        False,
        "--allow-non-loopback",
        help=(
            "Permit binding to non-loopback hosts (LAN IP, 0.0.0.0, ::). "
            "Off by default — token-based auth still applies but the "
            "agent's principal model widens beyond 'this OS user'. "
            "Logs a loud WARNING when used."
        ),
    ),
    port: int = typer.Option(7331, help="Bind port."),
    agent_id: str = typer.Option("assistant", help="Agent id."),
    provider: str = typer.Option(
        "ollama",
        "--provider",
        help=(
            "Catalog provider id (openclaw-style). One of: ollama (default), "
            "anthropic, openai, google, vllm, lmstudio, llamacpp, openrouter, "
            "groq, deepseek, mistral, together, fireworks, kilocode, moonshot, "
            "zai, minimax, bedrock, vertex-ai, openai-compatible, proxy, "
            "litellm, anthropic-vertex, or 'echo' (test). Provider determines "
            "the transport — model is picked separately via --model. "
            "Pre-rc.15 names ('local', 'pi') are accepted as legacy aliases."
        ),
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help=(
            "Model id from the pi catalog (e.g. gemma4:latest, "
            "claude-sonnet-4-6, gpt-4o-mini, gemini-2.0-flash). When omitted, "
            "the provider's catalog default is used. Custom models not in the "
            "catalog (e.g. fine-tuned vLLM weights) are accepted with a "
            "synthetic 128K-window registry entry."
        ),
    ),
    base_url: str | None = typer.Option(
        None,
        "--base-url",
        help=(
            "Override the model's transport URL. Useful when running an "
            "inline provider (Ollama / vLLM / LM Studio / llama.cpp / "
            "litellm / proxy / openai-compatible) on a non-default host. "
            "Defaults: http://127.0.0.1:11434/v1 (Ollama), "
            "http://127.0.0.1:8000/v1 (vLLM)."
        ),
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help=(
            "API key for hosted providers (anthropic / openai / google / "
            "groq / etc). Inline providers (Ollama / vLLM / lmstudio / …) "
            "ignore it. Falls back to the provider's env var when unset "
            "(see oxenclaw/pi/registry.py:EnvAuthStorage)."
        ),
    ),
    system_prompt: str | None = typer.Option(
        None, "--system-prompt", help="Override the agent's system prompt."
    ),
    auth_token: str | None = typer.Option(
        None,
        "--auth-token",
        help=(
            "Bearer token clients must present on WS connect. "
            "Falls back to OXENCLAW_GATEWAY_TOKEN env var if unset. "
            "If neither is set, auth is DISABLED with a warning."
        ),
    ),
    allowed_origins: str | None = typer.Option(
        None,
        "--allowed-origins",
        help=(
            "Comma-separated CSRF Origin allowlist for browser WS upgrades "
            "(e.g. 'http://localhost:7331,tauri://localhost'). "
            "Falls back to OXENCLAW_ALLOWED_ORIGINS env. When unset, no "
            "Origin check (back-compat)."
        ),
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
    skip_preflight: bool = typer.Option(
        False,
        "--skip-preflight",
        help="Skip startup config validation. Use only if you know what you're doing.",
    ),
) -> None:
    """Start the gateway server + every discovered channel + cron scheduler."""
    # Refuse non-loopback binds before we touch any subsystem so the
    # operator sees a fast, loud failure if they typo `--host 0.0.0.0`
    # without meaning to.
    try:
        validate_bind_host(host, allow_non_loopback=allow_non_loopback)
    except RemoteBindRefused as exc:
        raise typer.BadParameter(str(exc)) from exc

    from oxenclaw.config.auth_token import (
        format_startup_banner,
        resolve_or_generate_token,
    )
    from oxenclaw.observability import configure_logging
    from oxenclaw.plugin_sdk.runtime_env import describe_platform, is_wsl

    configure_logging(level=logging.DEBUG if verbose else logging.INFO)
    logger.info("oxenClaw starting on %s", describe_platform())
    if is_wsl():
        logger.info(
            "WSL2 detected — see docs/INSTALL_WSL.md for networking and Ollama configuration tips"
        )
    # Resolve / auto-generate the gateway token before the rest of the
    # boot. This way the operator sees the token (or its file location)
    # in the startup banner, which is the openclaw UX.
    resolved_token = resolve_or_generate_token(explicit=auth_token)
    auth_token = resolved_token.token
    logger.info("\n%s", format_startup_banner(resolved_token, host=host, port=port))
    if not skip_preflight:
        from oxenclaw.config.preflight import run_preflight

        report = run_preflight(
            chat_provider=provider,
            chat_model=model,
            chat_base_url=base_url,
            chat_api_key=api_key,
        )
        for finding in report.findings:
            (logger.error if finding.severity == "error" else logger.warning)(
                "preflight: %s", finding.format()
            )
        if not report.ok:
            logger.error(
                "preflight failed (%d errors). Aborting startup. "
                "Run `oxenclaw config validate` to see the full report, "
                "or pass --skip-preflight to bypass.",
                len(report.errors),
            )
            raise typer.Exit(code=1)
    asyncio.run(
        _run_gateway(
            host=host,
            port=port,
            agent_id=agent_id,
            provider=provider,
            model=model,
            base_url=base_url,
            api_key=api_key,
            system_prompt=system_prompt,
            auth_token=auth_token,
            allowed_origins=allowed_origins,
        )
    )


@app.command("token")
def token_cmd(
    rotate: bool = typer.Option(
        False, "--rotate", help="Discard the persisted token and generate a new one."
    ),
    show: bool = typer.Option(
        True, "--show/--no-show", help="Print the token value (off → just print the path)."
    ),
) -> None:
    """Show, generate, or rotate the persistent gateway token.

    The token is stored at `~/.oxenclaw/gateway-token` (mode 0600) and
    used as the default by `oxenclaw gateway start`. Override at runtime
    with `--auth-token` or `OXENCLAW_GATEWAY_TOKEN`.
    """
    from oxenclaw.config.auth_token import (
        resolve_or_generate_token,
        token_file_path,
    )

    resolved = resolve_or_generate_token(rotate=rotate)
    path = resolved.path or token_file_path()
    typer.echo(f"path:   {path}")
    typer.echo(f"source: {resolved.source}")
    if show:
        typer.echo(f"token:  {resolved.token}")
    else:
        typer.echo("token:  (suppressed; pass --show to print)")
    if resolved.source == "generated":
        typer.echo("\n  (the previous token, if any, is now invalid)")


def build_agent(
    *,
    agent_id: str,
    provider: str,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    system_prompt: str | None = None,
    memory: MemoryRetriever | None = None,
    tools: ToolRegistry | None = None,  # noqa: F821 — forward ref
) -> Agent:
    """Typer-friendly wrapper around the shared agent factory."""
    try:
        return _build_agent(
            agent_id=agent_id,
            provider=provider,
            model=model,
            base_url=base_url,
            api_key=api_key,
            system_prompt=system_prompt,
            memory=memory,
            tools=tools,
        )
    except UnknownProvider as exc:
        raise typer.BadParameter(str(exc)) from exc


def build_channel_router() -> ChannelRouter:
    """Discover plugins → load accounts → populate a channel router."""
    config = load_config()
    paths = default_paths()
    plugins = discover_plugins()
    router = ChannelRouter()
    for plugin_id in plugins.ids():
        entry = plugins.require(plugin_id)
        try:
            accounts = entry.load_accounts(config, paths)
        except Exception:
            logger.exception("plugin %s loader raised — skipping", plugin_id)
            continue
        for account_id, channel in accounts.items():
            router.register(plugin_id, account_id, channel)
            logger.info("loaded channel %s:%s", plugin_id, account_id)
    # Always register the built-in dashboard / desktop-client channel so
    # the web UI and native desktop apps work as a default chat surface
    # without any external service (Slack, …) being configured.
    from oxenclaw.extensions.dashboard.channel import CHANNEL_ID, DashboardChannel

    if router.get(CHANNEL_ID, "main") is None:
        router.register(CHANNEL_ID, "main", DashboardChannel())
        logger.info("loaded channel %s:main (built-in)", CHANNEL_ID)
    return router


async def _run_gateway(
    *,
    host: str,
    port: int,
    agent_id: str,
    provider: str,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    system_prompt: str | None = None,
    auth_token: str | None = None,
    allowed_origins: str | None = None,
) -> None:
    config = load_config()
    paths = default_paths()

    channel_router = build_channel_router()

    # Memory must exist before the agent so we can wire it in.
    # Embedding endpoint is configured separately from the chat agent
    # base_url because operators sometimes run chat against a remote
    # vLLM but keep embeddings local on Ollama (or vice versa).
    embed_kwargs: dict[str, str] = {}
    if env_base := os.environ.get("OXENCLAW_EMBED_BASE_URL"):
        embed_kwargs["base_url"] = env_base
    if env_model := os.environ.get("OXENCLAW_EMBED_MODEL"):
        embed_kwargs["model"] = env_model
    if env_key := os.environ.get("OXENCLAW_EMBED_API_KEY"):
        embed_kwargs["api_key"] = env_key
    memory_retriever = MemoryRetriever.for_root(paths, OpenAIEmbeddings(**embed_kwargs))

    # Cron scheduler is created early so the cron tool can be wired
    # into the agent's tool registry. Dispatcher gets a forward
    # reference; we patch the scheduler's dispatcher pointer after the
    # dispatcher is built.
    cron_store = CronJobStore(paths=paths)
    cron_run_store = CronRunStore(paths.home / "cron" / "runs.json")

    # Build the agent with bundled tools pre-registered, so the model
    # has weather / web fetch / web search / github / skill_creator
    # available out of the box without any config edits.
    from oxenclaw.agents.builtin_tools import default_tools as _builtin_tools
    from oxenclaw.agents.tools import ToolRegistry
    from oxenclaw.tools_pkg.bundle import (
        bundled_tools_with_deps,
        default_bundled_tools,
    )

    tool_registry = ToolRegistry()
    tool_registry.register_all(_builtin_tools())
    tool_registry.register_all(default_bundled_tools())
    # The dep-bound tools are added below once cron_scheduler exists.

    agents = AgentRegistry()
    agents.register(
        build_agent(
            agent_id=agent_id,
            provider=provider,
            model=model,
            base_url=base_url,
            api_key=api_key,
            system_prompt=system_prompt,
            memory=memory_retriever,
            tools=tool_registry,
        )
    )

    dispatcher = Dispatcher(agents=agents, config=config, send=channel_router.send)

    cron_scheduler = CronScheduler(
        store=cron_store, dispatcher=dispatcher, run_store=cron_run_store
    )

    # Now that scheduler + channel_router exist, register the
    # dependency-bound tools (cron, message, healthcheck, session_logs).
    tool_registry.register_all(
        bundled_tools_with_deps(
            channel_router=channel_router,
            cron_scheduler=cron_scheduler,
            memory=getattr(memory_retriever, "store", None),
        )
    )
    approvals = ApprovalManager(state_path=paths.home / "approvals.json")

    # ClawHub registries pulled from config.yaml `clawhub` section. Operators
    # who want to lock down to a private mirror declare it there.
    raw_clawhub = (config.clawhub or {}) if hasattr(config, "clawhub") else {}
    try:
        registries_cfg = ClawHubRegistries.model_validate(raw_clawhub or {})
    except Exception:
        logger.exception("invalid clawhub config; falling back to defaults")
        registries_cfg = ClawHubRegistries()
    clawhub_client = MultiRegistryClient(registries_cfg)
    skill_installer = SkillInstaller(clawhub_client, paths=paths)

    # Register the skill_resolver tool so the LLM can locate + install skills
    # by intent at runtime. Uses the same MultiRegistryClient + SkillInstaller
    # the gateway already has, so no extra credentials are needed.
    from oxenclaw.tools_pkg.skill_resolver_tool import skill_resolver_tool as _skill_resolver_tool

    tool_registry.register(_skill_resolver_tool(registries=clawhub_client, installer=skill_installer, paths=paths))

    router = _build_router(
        agents=agents,
        dispatcher=dispatcher,
        channel_router=channel_router,
        cron_scheduler=cron_scheduler,
        cron_run_store=cron_run_store,
        approvals=approvals,
        paths_home=paths,
        clawhub_client=clawhub_client,
        skill_installer=skill_installer,
        memory_retriever=memory_retriever,
    )
    readiness = build_default_readiness(
        channel_router=channel_router,
        cron_scheduler=cron_scheduler,
        memory=memory_retriever,
    )
    parsed_origins = (
        [o.strip() for o in allowed_origins.split(",") if o.strip()] if allowed_origins else None
    )
    server = GatewayServer(
        router,
        auth_token=auth_token,
        allowed_origins=parsed_origins,
        readiness=readiness,
    )

    install_signal_handlers(server)

    canvas_pump = asyncio.create_task(
        _pump_canvas_events(get_default_canvas_bus(), server),
        name="canvas-event-pump",
    )

    async with _supervise_monitors(channel_router, dispatcher):
        cron_scheduler.start()
        try:
            logger.info(
                "gateway up — channels=%s  agents=%s",
                channel_router.channels_by_id() or "{}",
                agents.ids(),
            )
            await server.serve(host=host, port=port)
        finally:
            logger.info("gateway shutting down — cleaning up subsystems")
            cron_scheduler.stop()
            approvals.cancel_all(reason="shutdown")
            canvas_pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await canvas_pump
            await channel_router.aclose()
            await clawhub_client.aclose()
            await memory_retriever.aclose()
            logger.info("gateway shutdown complete")


async def _pump_canvas_events(bus, server: GatewayServer) -> None:  # type: ignore[no-untyped-def]
    """Forward CanvasEvent fanout into GatewayServer.broadcast as EventFrames."""
    from oxenclaw.gateway.protocol import CanvasEventFrame, EventFrame

    async for evt in bus.stream():
        try:
            frame = EventFrame(
                body=CanvasEventFrame(
                    kind="canvas",
                    agent_id=evt.agent_id,
                    body=evt.to_dict(),
                ),
            )
            await server.broadcast(frame)
        except Exception:
            logger.exception("canvas event pump failed for kind=%s", evt.kind)


def build_default_readiness(
    *,
    channel_router: ChannelRouter | None = None,
    cron_scheduler: CronScheduler | None = None,
    memory: MemoryRetriever | None = None,
):  # type: ignore[no-untyped-def]
    """Wire the standard readiness probes for a CLI-launched gateway.

    Each probe is non-blocking and short — the readiness endpoint is
    polled by orchestrators (k8s readiness, systemd) at high frequency
    so probes must not drift toward seconds. Heavy diagnostics belong in
    the `healthcheck` skill instead.
    """
    from oxenclaw.observability import ReadinessChecker, ReadinessStatus
    from oxenclaw.observability.metrics import METRICS

    checker = ReadinessChecker()

    async def channels_probe():
        if channel_router is None:
            return ReadinessStatus.OK, "no router"
        bindings = list(channel_router.bindings())
        if not bindings:
            return (
                ReadinessStatus.DEGRADED,
                "no channels registered (gateway is RPC-only)",
            )
        return ReadinessStatus.OK, f"{len(bindings)} channel(s)"

    async def cron_probe():
        if cron_scheduler is None:
            return ReadinessStatus.OK, "no scheduler"
        # APScheduler exposes `running` if available.
        running = getattr(cron_scheduler, "running", True)
        if not running:
            return ReadinessStatus.DOWN, "scheduler stopped"
        return ReadinessStatus.OK, "scheduler running"

    async def memory_probe():
        if memory is None:
            return ReadinessStatus.OK, "no memory configured"
        # A trivial round-trip — just verify the store is reachable.
        try:
            getattr(memory, "store", None)
        except Exception as exc:
            return ReadinessStatus.DOWN, f"memory store: {exc}"
        return ReadinessStatus.OK, "memory ready"

    async def metrics_probe():
        # Always-OK probe — the registry must be initialised.
        return ReadinessStatus.OK, f"{len(METRICS.all_metrics())} metrics"

    checker.register_check("channels", channels_probe, critical=False)
    checker.register_check("cron", cron_probe, critical=True)
    checker.register_check("memory", memory_probe, critical=False)
    checker.register_check("metrics", metrics_probe, critical=False)
    return checker


def install_signal_handlers(server: GatewayServer) -> None:
    """Wire SIGTERM/SIGINT into `server.request_shutdown()`.

    The signal handler runs on the asyncio event loop, so it can't do work
    directly — it just sets the shutdown event. The actual cleanup happens
    in the `finally` block of `_run_gateway` after `server.serve()`
    returns.
    """
    loop = asyncio.get_running_loop()
    handled = False

    def _handler(signame: str) -> None:
        nonlocal handled
        if handled:
            logger.warning("received %s during shutdown — forcing exit", signame)
            # Second signal: bail hard so a stuck handler can't keep us up.
            loop.stop()
            return
        handled = True
        logger.info("received %s — beginning graceful shutdown", signame)
        server.request_shutdown()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handler, sig.name)
        except NotImplementedError:
            # Windows: add_signal_handler isn't supported. Fall back to a
            # plain signal.signal — the handler runs in the main thread, so
            # it must be safe to call from there.
            signal.signal(sig, lambda *_a, _name=sig.name: _handler(_name))


def _build_router(
    *,
    agents: AgentRegistry,
    dispatcher: Dispatcher,
    channel_router: ChannelRouter,
    cron_scheduler: CronScheduler,
    cron_run_store: CronRunStore | None = None,
    approvals: ApprovalManager,
    paths_home,  # type: ignore[no-untyped-def]
    clawhub_client: ClawHubClient | MultiRegistryClient | None = None,
    skill_installer: SkillInstaller | None = None,
    memory_retriever: MemoryRetriever | None = None,
) -> Router:
    router = Router()

    @router.method("chat.send", ChatSendParams)
    async def _chat_send(p: ChatSendParams) -> ChatSendResult:
        envelope = InboundEnvelope(
            channel=p.channel,
            account_id=p.account_id,
            target=ChannelTarget(
                channel=p.channel,
                account_id=p.account_id,
                chat_id=p.chat_id,
                thread_id=p.thread_id,
            ),
            sender_id="cli",
            text=p.text,
            media=list(p.media),
            received_at=0.0,
        )
        outcome = await dispatcher.dispatch_with_outcome(envelope)
        if outcome.results:
            return ChatSendResult(
                message_id=outcome.results[0].message_id,
                timestamp=outcome.results[0].timestamp,
                status="ok",
                agent_id=outcome.agent_id,
            )
        # Agent ran but no outbound was successfully delivered. This is
        # the dashboard's typical case: the user's `channel` value has no
        # plugin loaded (it's a fake routing key just for session
        # bookkeeping), so `channel_router.send` raises and we collect
        # delivery warnings. The agent's reply IS in conversation
        # history — the dashboard's chat.history poll renders it. So
        # we report status="ok" with the warning attached, not a drop.
        if outcome.agent_yielded > 0:
            warning = "; ".join(outcome.delivery_warnings) or None
            return ChatSendResult(
                message_id="local",
                timestamp=0.0,
                status="ok",
                reason=warning,
                agent_id=outcome.agent_id,
            )
        # Real drop: no agent ran. Surface why so dashboards can render
        # an informative error instead of a silent no-op.
        reason = outcome.drop_reason or "agent ran but produced no reply"
        return ChatSendResult(
            message_id="dropped",
            timestamp=0.0,
            status="dropped",
            reason=reason,
            agent_id=outcome.agent_id,
        )

    register_agents_methods(router, agents)
    register_channels_methods(router, channel_router)
    register_chat_methods(router, paths=paths_home)
    register_usage_methods(router, paths=paths_home)
    register_plan_methods(router, paths=paths_home)
    register_config_methods(router)
    register_cron_methods(router, cron_scheduler, run_store=cron_run_store)
    register_approval_methods(router, approvals)
    register_isolation_methods(router)
    register_canvas_methods(
        router,
        store=get_default_canvas_store(),
        bus=get_default_canvas_bus(),
    )
    if memory_retriever is not None:
        register_memory_methods(router, memory_retriever)
    if clawhub_client is not None:
        register_skills_methods(
            router,
            client=clawhub_client,
            installer=skill_installer,
            paths=paths_home,
        )
    return router


@contextlib.asynccontextmanager
async def _supervise_monitors(channel_router: ChannelRouter, dispatcher: Dispatcher):
    """Spawn one ChannelRunner task per (channel, account) binding.

    Plugins that set `outbound_only = True` (e.g. Slack notification
    channel) skip the supervisor entirely — they ship messages but
    don't listen for inbound. Their `monitor()` is allowed to raise
    `NotImplementedError` for clarity if anyone calls it directly.
    """
    runners: list[ChannelRunner] = []
    tasks: list[asyncio.Task] = []  # type: ignore[type-arg]
    for channel_id, account_id, plugin in channel_router.bindings():
        if getattr(plugin, "outbound_only", False):
            logger.info(
                "channel %s:%s is outbound-only — no monitor spawned",
                channel_id,
                account_id,
            )
            continue
        opts = MonitorOpts(account_id=account_id, on_inbound=dispatcher.dispatch)
        runner = ChannelRunner(plugin, opts)
        task = asyncio.create_task(runner.run_forever(), name=f"monitor:{channel_id}:{account_id}")
        runners.append(runner)
        tasks.append(task)
    try:
        yield runners
    finally:
        for runner in runners:
            await runner.stop()
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
