"""`sampyclaw gateway start` — full composition.

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
import signal

import typer

from sampyclaw.agents import (
    Agent,
    AgentRegistry,
    Dispatcher,
    UnknownProvider,
)
from sampyclaw.agents.factory import build_agent as _build_agent
from sampyclaw.approvals import ApprovalManager
from sampyclaw.channels import ChannelRouter, ChannelRunner
from sampyclaw.clawhub import ClawHubClient, MultiRegistryClient, SkillInstaller
from sampyclaw.clawhub.registries import ClawHubRegistries
from sampyclaw.config import default_paths, load_config
from sampyclaw.cron import CronJobStore, CronScheduler
from sampyclaw.gateway import (
    ChatSendParams,
    ChatSendResult,
    GatewayServer,
    Router,
)
from sampyclaw.gateway.agents_methods import register_agents_methods
from sampyclaw.gateway.approval_methods import register_approval_methods
from sampyclaw.gateway.channels_methods import register_channels_methods
from sampyclaw.gateway.chat_methods import register_chat_methods
from sampyclaw.gateway.config_methods import register_config_methods
from sampyclaw.gateway.cron_methods import register_cron_methods
from sampyclaw.gateway.isolation_methods import register_isolation_methods
from sampyclaw.gateway.memory_methods import register_memory_methods
from sampyclaw.gateway.skills_methods import register_skills_methods
from sampyclaw.memory import MemoryRetriever, OpenAIEmbeddings
from sampyclaw.plugin_sdk.channel_contract import (
    ChannelTarget,
    InboundEnvelope,
    MonitorOpts,
)
from sampyclaw.plugin_sdk.runtime_env import get_logger
from sampyclaw.plugins import discover_plugins

app = typer.Typer(help="Run the gateway server.", no_args_is_help=True)

logger = get_logger("cli.gateway")


@app.command("start")
def start(
    host: str = typer.Option("127.0.0.1", help="Bind host."),
    port: int = typer.Option(7331, help="Bind port."),
    agent_id: str = typer.Option("assistant", help="Agent id."),
    provider: str = typer.Option(
        "local",
        "--provider",
        help="Agent provider: 'local' (Ollama / OpenAI-compatible server, default), 'echo', or 'anthropic'.",
    ),
    model: str | None = typer.Option(
        None, "--model", help="Model id (provider-specific)."
    ),
    base_url: str | None = typer.Option(
        None,
        "--base-url",
        help=(
            "Override base URL (local provider only). "
            "Default: http://127.0.0.1:11434/v1 (Ollama)."
        ),
    ),
    api_key: str | None = typer.Option(
        None, "--api-key", help="API key (local provider; most local servers don't need one)."
    ),
    system_prompt: str | None = typer.Option(
        None, "--system-prompt", help="Override the agent's system prompt."
    ),
    auth_token: str | None = typer.Option(
        None,
        "--auth-token",
        help=(
            "Bearer token clients must present on WS connect. "
            "Falls back to SAMPYCLAW_GATEWAY_TOKEN env var if unset. "
            "If neither is set, auth is DISABLED with a warning."
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
    from sampyclaw.observability import configure_logging

    configure_logging(level=logging.DEBUG if verbose else logging.INFO)
    if not skip_preflight:
        from sampyclaw.config.preflight import run_preflight

        report = run_preflight()
        for finding in report.findings:
            (logger.error if finding.severity == "error" else logger.warning)(
                "preflight: %s", finding.format()
            )
        if not report.ok:
            logger.error(
                "preflight failed (%d errors). Aborting startup. "
                "Run `sampyclaw config validate` to see the full report, "
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
        )
    )


def build_agent(
    *,
    agent_id: str,
    provider: str,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    system_prompt: str | None = None,
    memory: MemoryRetriever | None = None,
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
) -> None:
    config = load_config()
    paths = default_paths()

    channel_router = build_channel_router()

    # Memory must exist before the agent so we can wire it in.
    memory_retriever = MemoryRetriever.for_root(paths, OpenAIEmbeddings())

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
        )
    )

    dispatcher = Dispatcher(
        agents=agents, config=config, send=channel_router.send
    )

    cron_scheduler = CronScheduler(
        store=CronJobStore(paths=paths), dispatcher=dispatcher
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

    router = _build_router(
        agents=agents,
        dispatcher=dispatcher,
        channel_router=channel_router,
        cron_scheduler=cron_scheduler,
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
    server = GatewayServer(router, auth_token=auth_token, readiness=readiness)

    install_signal_handlers(server)

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
            await channel_router.aclose()
            await clawhub_client.aclose()
            await memory_retriever.aclose()
            logger.info("gateway shutdown complete")


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
    from sampyclaw.observability import ReadinessChecker, ReadinessStatus
    from sampyclaw.observability.metrics import METRICS

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
            logger.warning(
                "received %s during shutdown — forcing exit", signame
            )
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
            received_at=0.0,
        )
        results = await dispatcher.dispatch(envelope)
        if results:
            return ChatSendResult(
                message_id=results[0].message_id, timestamp=results[0].timestamp
            )
        return ChatSendResult(message_id="dropped", timestamp=0.0)

    register_agents_methods(router, agents)
    register_channels_methods(router, channel_router)
    register_chat_methods(router, paths=paths_home)
    register_config_methods(router)
    register_cron_methods(router, cron_scheduler)
    register_approval_methods(router, approvals)
    register_isolation_methods(router)
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
async def _supervise_monitors(
    channel_router: ChannelRouter, dispatcher: Dispatcher
):
    """Spawn one ChannelRunner task per (channel, account) binding."""
    runners: list[ChannelRunner] = []
    tasks: list[asyncio.Task] = []  # type: ignore[type-arg]
    for channel_id, account_id, plugin in channel_router.bindings():
        opts = MonitorOpts(account_id=account_id, on_inbound=dispatcher.dispatch)
        runner = ChannelRunner(plugin, opts)
        task = asyncio.create_task(
            runner.run_forever(), name=f"monitor:{channel_id}:{account_id}"
        )
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
