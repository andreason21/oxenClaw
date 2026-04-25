"""Python equivalents of the @mariozechner/pi-* TypeScript packages.

openclaw's `src/agents/pi-embedded-runner/` is built on top of four upstream
npm packages:
- `@mariozechner/pi-agent-core` — agent message + tool primitives, StreamFn
- `@mariozechner/pi-ai`         — model/provider types, stream APIs
- `@mariozechner/pi-coding-agent` — session manager, model registry, auth
                                    storage, settings, token estimator,
                                    extension factory
- `@mariozechner/pi-tui`        — terminal UI (out of scope here)

This subpackage ports the *types and runtime primitives* sampyClaw needs
to build the same inference loop. Module map:

- `pi.messages`   → AgentMessage, UserMessage, AssistantMessage,
                    ToolResultMessage, content blocks (TextContent,
                    ImageContent, ToolUseBlock, ThinkingBlock).
- `pi.tools`      → AgentTool protocol, ToolUseRequest, ToolResult.
- `pi.models`     → Model, Context, Api, ModelRegistry, AuthStorage.
- `pi.streaming`  → StreamFn protocol, SimpleStreamOptions,
                    AssistantMessageEvent, createAssistantMessageEventStream,
                    streamSimple (concrete provider impls live in
                    `sampyclaw.pi.providers.*`).
- `pi.tokens`     → estimateTokens, ModelContextTokens.
- `pi.thinking`   → ThinkingLevel.
- `pi.session`    → AgentSession, SessionManager, SessionEntry,
                    CompactionEntry, CreateAgentSessionOptions,
                    SettingsManager, ExtensionFactory.
- `pi.registry`   → ModelRegistry + AuthStorage + provider id normalization.
- `pi.auth`       → resolve_api(model, auth) → Api.
- `pi.catalog`    → seed model catalog.

The naming mirrors the TS surface 1:1 so that porting the openclaw glue
code in `src/agents/pi-embedded-runner/` becomes a near-mechanical
translation rather than an architectural redesign.
"""

from sampyclaw.pi.auth import MissingCredential, resolve_api
from sampyclaw.pi.catalog import default_registry
from sampyclaw.pi.messages import (
    AgentMessage,
    AssistantMessage,
    ImageContent,
    SystemMessage,
    TextContent,
    ThinkingBlock,
    ToolResultBlock,
    ToolResultMessage,
    ToolUseBlock,
    UserMessage,
    assistant_text,
    text_block,
    text_message,
)
from sampyclaw.pi.models import Api, Context, Model, ProviderId
from sampyclaw.pi.registry import (
    AuthStorage,
    EnvAuthStorage,
    InMemoryAuthStorage,
    InMemoryModelRegistry,
    ModelRegistry,
    inline_api,
    is_inline_provider,
    normalize_provider_id,
)
from sampyclaw.pi.session import (
    AgentSession,
    CompactionEntry,
    CreateAgentSessionOptions,
    ExtensionFactory,
    InMemorySessionManager,
    SessionEntry,
    SessionManager,
    SettingsManager,
)
from sampyclaw.pi.streaming import (
    AssistantMessageEvent,
    AssistantMessageEventStream,
    ErrorEvent,
    SimpleStreamOptions,
    StopEvent,
    StreamFn,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolUseEndEvent,
    ToolUseInputDeltaEvent,
    ToolUseStartEvent,
    UsageEvent,
    create_assistant_message_event_stream,
    get_provider_stream,
    register_provider_stream,
    stream_simple,
)
from sampyclaw.pi.thinking import (
    ANTHROPIC_THINKING_BUDGETS,
    GEMINI_THINKING_BUDGETS,
    OPENAI_REASONING_EFFORT,
    ThinkingLevel,
)
from sampyclaw.pi.tokens import (
    MODEL_CONTEXT_TOKENS,
    estimate_tokens,
    estimate_tokens_for_text,
    model_context_window,
)
from sampyclaw.pi.tools import (
    AgentTool,
    ToolCallContext,
    ToolExecutionResult,
    ToolUseRequest,
)

__all__ = [
    "ANTHROPIC_THINKING_BUDGETS",
    "AgentMessage",
    "AgentSession",
    "AgentTool",
    "Api",
    "AssistantMessage",
    "AssistantMessageEvent",
    "AssistantMessageEventStream",
    "AuthStorage",
    "CompactionEntry",
    "Context",
    "CreateAgentSessionOptions",
    "EnvAuthStorage",
    "ErrorEvent",
    "ExtensionFactory",
    "GEMINI_THINKING_BUDGETS",
    "ImageContent",
    "InMemoryAuthStorage",
    "InMemoryModelRegistry",
    "InMemorySessionManager",
    "MODEL_CONTEXT_TOKENS",
    "MissingCredential",
    "Model",
    "ModelRegistry",
    "OPENAI_REASONING_EFFORT",
    "ProviderId",
    "SessionEntry",
    "SessionManager",
    "SettingsManager",
    "SimpleStreamOptions",
    "StopEvent",
    "StreamFn",
    "SystemMessage",
    "TextContent",
    "TextDeltaEvent",
    "ThinkingBlock",
    "ThinkingDeltaEvent",
    "ThinkingLevel",
    "ToolCallContext",
    "ToolExecutionResult",
    "ToolResultBlock",
    "ToolResultMessage",
    "ToolUseBlock",
    "ToolUseEndEvent",
    "ToolUseInputDeltaEvent",
    "ToolUseRequest",
    "ToolUseStartEvent",
    "UsageEvent",
    "UserMessage",
    "assistant_text",
    "create_assistant_message_event_stream",
    "default_registry",
    "get_provider_stream",
    "estimate_tokens",
    "estimate_tokens_for_text",
    "inline_api",
    "is_inline_provider",
    "model_context_window",
    "normalize_provider_id",
    "register_provider_stream",
    "resolve_api",
    "stream_simple",
    "text_block",
    "text_message",
]
