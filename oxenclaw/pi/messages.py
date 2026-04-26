"""AgentMessage union + content blocks.

Mirrors `@mariozechner/pi-ai` + `pi-agent-core` message types. The union
follows Anthropic's role/content-block shape because it's the most general:
OpenAI/Google/Gemini messages can be lossy-converted to this and back.

Roles:
- `system` (treated as a top-level field by some providers, an inline
  message by others — runtime adapters choose).
- `user`        — content is `str` or `list[UserContentBlock]`.
- `assistant`   — content is `list[AssistantContentBlock]`.
- `tool_result` — emitted as `role="user"` on the wire for OpenAI/Anthropic
  but kept as a discriminated case here for clean iteration in the runner.

Discriminator: the `role` field. Pydantic v2 handles the union via the
`Field(discriminator="role")` annotation.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ─── Content blocks ───────────────────────────────────────────────────


class TextContent(BaseModel):
    """A text segment inside a multi-block message."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["text"] = "text"
    text: str


class ImageContent(BaseModel):
    """Image block. `data` is base64 (no `data:` prefix); `media_type` is the
    MIME type. URL-mode images become base64 at the channel boundary so the
    runtime doesn't have to fetch them."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["image"] = "image"
    media_type: str
    data: str


class ToolUseBlock(BaseModel):
    """Assistant requests a tool. `id` correlates with the matching
    ToolResultMessage that the loop appends after the tool runs."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ThinkingBlock(BaseModel):
    """Anthropic / Gemini extended-reasoning block. `signature` is the opaque
    blob the provider returns and that we must echo back unchanged on the
    next turn for the cache to hit."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["thinking"] = "thinking"
    thinking: str
    signature: str | None = None


UserContentBlock = Annotated[TextContent | ImageContent, Field(discriminator="type")]
AssistantContentBlock = Annotated[
    TextContent | ToolUseBlock | ThinkingBlock, Field(discriminator="type")
]


# ─── Tool result wrapper ──────────────────────────────────────────────


class ToolResultBlock(BaseModel):
    """Inside a `tool_result` message, the actual tool output. May be text
    or a structured (JSON-serialisable) value."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str | list[TextContent | ImageContent]
    is_error: bool = False


# ─── Top-level messages ──────────────────────────────────────────────


class SystemMessage(BaseModel):
    """Some providers treat system as a separate field; we keep it as a
    message for storage/replay, then runtime adapters lift it out."""

    model_config = ConfigDict(extra="forbid")
    role: Literal["system"] = "system"
    content: str


class UserMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user"] = "user"
    content: str | list[UserContentBlock]


class AssistantMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["assistant"] = "assistant"
    content: list[AssistantContentBlock]
    # Provider stop reason: end_turn, max_tokens, tool_use, stop_sequence...
    stop_reason: str | None = None
    # Provider-specific cache token reporting; runtime fills these in.
    usage: dict[str, Any] | None = None


class ToolResultMessage(BaseModel):
    """Wire format follows Anthropic — emitted as a user-role message with
    one or more tool_result blocks. The role is kept distinct here for
    typed iteration; serializers re-tag as needed."""

    model_config = ConfigDict(extra="forbid")
    role: Literal["tool_result"] = "tool_result"
    results: list[ToolResultBlock]


AgentMessage = Annotated[
    SystemMessage | UserMessage | AssistantMessage | ToolResultMessage,
    Field(discriminator="role"),
]


# ─── Helpers ─────────────────────────────────────────────────────────


def text_block(s: str) -> TextContent:
    return TextContent(text=s)


def text_message(s: str, *, role: Literal["user", "system"] = "user") -> AgentMessage:
    if role == "system":
        return SystemMessage(content=s)
    return UserMessage(content=s)


def assistant_text(s: str, *, stop_reason: str | None = "end_turn") -> AssistantMessage:
    return AssistantMessage(content=[TextContent(text=s)], stop_reason=stop_reason)


__all__ = [
    "AgentMessage",
    "AssistantContentBlock",
    "AssistantMessage",
    "ImageContent",
    "SystemMessage",
    "TextContent",
    "ThinkingBlock",
    "ToolResultBlock",
    "ToolResultMessage",
    "ToolUseBlock",
    "UserContentBlock",
    "UserMessage",
    "assistant_text",
    "text_block",
    "text_message",
]
