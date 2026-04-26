"""cron tool — let the LLM register/list/remove cron jobs.

Mirrors openclaw `cron-tool.ts`. Wraps the existing `CronScheduler` so an
agent can directly create scheduled tasks during a conversation, e.g.
"remind me at 9am to check deploy logs". The tool is approval-gateable
via `gated_tool(...)` so the operator approves the schedule before it
lands.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from oxenclaw.agents.tools import FunctionTool, Tool
from oxenclaw.cron.models import NewCronJob
from oxenclaw.cron.scheduler import CronScheduler


class _CronArgs(BaseModel):
    """Action verb dispatch — one tool, multiple operations.

    Single-tool design with an `action` discriminator keeps the tool-
    catalogue small (LLMs do better with few tools and clear actions).
    """

    action: Literal["add", "list", "remove", "toggle"] = Field(..., description="What to do.")

    # add / toggle args
    schedule: str | None = Field(
        None, description="5-field crontab expression. Required for `add`."
    )
    agent_id: str | None = Field(None, description="Target agent id (default: caller).")
    channel: str | None = Field(None, description="Channel id (e.g. 'telegram').")
    account_id: str | None = Field(None, description="Account id within the channel.")
    chat_id: str | None = Field(None, description="Chat id (where to dispatch).")
    thread_id: str | None = Field(None)
    prompt: str | None = Field(
        None, description="Synthetic user prompt the schedule emits when it fires."
    )
    description: str | None = Field(None, description="Optional human-readable label.")
    enabled: bool = Field(True)

    # remove / toggle args
    job_id: str | None = Field(None, description="Job id (required for remove/toggle).")


def cron_tool(
    scheduler: CronScheduler,
    *,
    default_agent_id: str | None = None,
    default_channel: str | None = None,
    default_account_id: str | None = None,
    default_chat_id: str | None = None,
) -> Tool:
    """Build a `cron` tool bound to `scheduler`.

    `default_*` values are used when the LLM omits the corresponding field
    so the model doesn't need to thread the boilerplate every time.
    """

    async def _h(args: _CronArgs) -> str:
        if args.action == "add":
            missing = [
                k
                for k, v in (
                    ("schedule", args.schedule),
                    ("prompt", args.prompt),
                )
                if not v
            ]
            if missing:
                return f"cron error: missing {missing} for action=add"
            agent_id = args.agent_id or default_agent_id
            channel = args.channel or default_channel
            account_id = args.account_id or default_account_id
            chat_id = args.chat_id or default_chat_id
            if not (agent_id and channel and account_id and chat_id):
                return (
                    "cron error: agent_id/channel/account_id/chat_id required "
                    "(no defaults configured)"
                )
            try:
                new = NewCronJob(
                    schedule=args.schedule,  # type: ignore[arg-type]
                    agent_id=agent_id,
                    channel=channel,
                    account_id=account_id,
                    chat_id=chat_id,
                    thread_id=args.thread_id,
                    prompt=args.prompt,  # type: ignore[arg-type]
                    description=args.description,
                    enabled=args.enabled,
                )
            except Exception as exc:
                return f"cron error: invalid job: {exc}"
            job = scheduler.add(new)
            return (
                f"cron added id={job.id} schedule={job.schedule!r} "
                f"channel={job.channel}:{job.account_id}:{job.chat_id} "
                f"agent={job.agent_id}"
            )

        if args.action == "list":
            jobs = scheduler.list()
            if not jobs:
                return "no cron jobs"
            lines = []
            for j in jobs:
                state = "enabled" if j.enabled else "DISABLED"
                lines.append(
                    f"{j.id[:8]}  {j.schedule:<14}  {state:<8}  "
                    f"{j.channel}:{j.account_id}:{j.chat_id} → {j.agent_id}  "
                    f"{j.description or ''}"
                )
            return "\n".join(lines)

        if args.action == "remove":
            if not args.job_id:
                return "cron error: job_id required for action=remove"
            ok = scheduler.remove(args.job_id)
            return "removed" if ok else f"no job with id={args.job_id!r}"

        if args.action == "toggle":
            if not args.job_id:
                return "cron error: job_id required for action=toggle"
            updated = scheduler.toggle(args.job_id, args.enabled)
            if updated is None:
                return f"no job with id={args.job_id!r}"
            state = "enabled" if updated.enabled else "disabled"
            return f"job {updated.id[:8]} now {state}"

        return f"cron error: unknown action {args.action!r}"

    return FunctionTool(
        name="cron",
        description=(
            "Manage scheduled tasks. action=add registers a new job; "
            "action=list shows all jobs; action=remove deletes by id; "
            "action=toggle enables/disables. The prompt fires as a "
            "synthetic user message on the configured channel."
        ),
        input_model=_CronArgs,
        handler=_h,
    )


__all__ = ["cron_tool"]
