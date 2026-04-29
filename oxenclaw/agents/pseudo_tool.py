"""Detect tool calls the model wrote as TEXT instead of as a real
`tool_use` block, so the agent can auto-fire them.

Small local models (gemma/qwen/llama-3.2) routinely "describe" a tool
call by emitting a fenced JSON block in their reply text — e.g.

    수원 날씨를 확인하겠습니다.

    ```json
    {"tool": "weather", "location": "Suwon, South Korea"}
    ```

— instead of producing the real `tool_use` content block the runtime
expects. The result: no tool fires, no result lands in the
transcript, and the next user turn (often "진행해" / "yes" / "ok")
finds nothing to act on.

This module returns a normalized `PseudoToolCall(name, args)` when
the assistant text plausibly carries a textually-rendered tool call
that matches a registered tool. The caller (`pi_agent.handle()`)
then executes the tool and feeds the result back to the model.

Design notes
------------
- We accept several JSON shapes the small models actually produce:
    {"tool": "<name>", ...rest}                          ← most common
    {"tool_name": "<name>", "arguments": {...}}          ← OpenAI-ish
    {"name": "<name>", "input"|"parameters"|"args": {...}}
    {"function": {"name": "<name>", "arguments": {...}}}
- Both fenced blocks (```json…``` / ```…```) and bare top-level JSON
  objects in the text are scanned.
- High-precision filter: a parsed object is only treated as a pseudo
  call when its `name` resolves to a registered tool. That keeps
  documentation snippets and example JSON in the model's reply from
  being auto-fired.
- We never auto-fire if the same assistant message already contains
  a real `ToolUseBlock` — the runtime's normal path took care of it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

_FENCED_BLOCK = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)```", re.DOTALL)


@dataclass
class PseudoToolCall:
    """A tool call the model wrote into its reply text instead of
    issuing as a real tool_use block."""

    name: str
    args: dict[str, Any]
    raw_block: str  # for logging only


def extract_pseudo_tool_call(
    text: str,
    *,
    is_known_tool: Callable[[str], bool],
    tool_schemas: Mapping[str, dict[str, Any]] | None = None,
) -> PseudoToolCall | None:
    """Return the first parseable + tool-resolving pseudo call, or None.

    `is_known_tool(name)` should return True iff `name` (after the
    caller's own canonicalisation) maps to a registered tool. We
    delegate the lookup so this module stays free of agent imports.

    `tool_schemas` (name → JSON input_schema) opts into a second-stage
    fallback: when a candidate JSON object has no `tool`/`name`/etc.
    field but its key set uniquely matches one tool's input schema
    (every required key present, no unknown extras), we infer that
    tool. Models served by the prelude system frequently emit the
    arguments alone — `{"action":"add","schedule":"...","prompt":"..."}` —
    expecting the runtime to know which tool they meant. This rescues
    that case without losing the safety of a strict caller-side
    `is_known_tool` predicate (we still gate on it).
    """
    if not text or not text.strip():
        return None

    candidates: list[str] = []
    for m in _FENCED_BLOCK.finditer(text):
        block = m.group(1).strip()
        if block:
            candidates.append(block)

    # Also scan for top-level bare JSON objects when no fenced block
    # carried the call. Cheap brace-depth walk — robust enough for
    # the single-object emissions small models produce.
    if not candidates:
        for span in _bare_top_level_objects(text):
            candidates.append(span)

    for raw in candidates:
        parsed = _safe_load_json(raw)
        if parsed is None:
            continue
        # Some models nest the call inside `{"function": {...}}` or
        # emit a list; normalize to a dict.
        for obj in _flatten_call_candidates(parsed):
            call = _coerce_to_call(obj)
            if call is not None:
                name, args = call
                if is_known_tool(name):
                    return PseudoToolCall(name=name, args=args, raw_block=raw[:200])
                continue
            # No name field on this candidate. Fall back to
            # schema-shape matching when the caller opted in.
            if not isinstance(obj, dict) or not tool_schemas:
                continue
            inferred = _match_to_schema(obj, tool_schemas)
            if inferred is None or not is_known_tool(inferred):
                continue
            return PseudoToolCall(name=inferred, args=obj, raw_block=raw[:200])
    return None


def _match_to_schema(
    obj: dict[str, Any], schemas: Mapping[str, dict[str, Any]]
) -> str | None:
    """Return the unique tool name whose schema matches `obj`, or None.

    Match criteria:
      - every key in the schema's `required` list is present in `obj`;
      - every key in `obj` is either declared in `properties` or
        the schema's `additionalProperties` is truthy.
    Ambiguity (two or more schemas tie) returns None — we prefer to
    fall through silently than auto-fire the wrong tool.
    """
    if not obj:
        return None
    obj_keys = set(obj.keys())
    best: tuple[int, str] | None = None  # (specificity, name)
    for name, schema in schemas.items():
        if not isinstance(schema, dict):
            continue
        properties = set((schema.get("properties") or {}).keys())
        required = set(schema.get("required") or [])
        if not required:
            # Schemas without required fields would match almost any
            # blob — too permissive to anchor on.
            continue
        if not required.issubset(obj_keys):
            continue
        allow_extras = bool(schema.get("additionalProperties"))
        if not allow_extras and not obj_keys.issubset(properties):
            continue
        specificity = len(required) + len(obj_keys & properties)
        if best is None or specificity > best[0]:
            best = (specificity, name)
        elif specificity == best[0]:
            # Tie → ambiguous. Refuse to guess.
            best = (specificity, "")
    if best is None or not best[1]:
        return None
    return best[1]


def _safe_load_json(s: str) -> Any | None:
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None


def _bare_top_level_objects(text: str) -> list[str]:
    """Yield candidate JSON object substrings by scanning brace depth.

    Handles the common case where the model wrote `{...}` inline
    without a fenced block. Skips strings/escapes properly so JSON
    containing braces inside quoted values doesn't break the scan.
    """
    out: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    out.append(text[start : i + 1])
                    start = -1
    return out


def _flatten_call_candidates(parsed: Any) -> list[Any]:
    """Some shapes wrap the call: `[{tool: ...}]`, `{tool_calls: [...]}`,
    `{function: {...}}`. Yield every dict-shaped inner object that
    might actually be the call."""
    out: list[Any] = []
    if isinstance(parsed, dict):
        out.append(parsed)
        # Common nesting: {function: {name: ..., arguments: {...}}}
        fn = parsed.get("function")
        if isinstance(fn, dict):
            out.append(fn)
        # OpenAI-style streaming dump: {tool_calls: [{function: {...}}, ...]}
        tcs = parsed.get("tool_calls")
        if isinstance(tcs, list):
            for tc in tcs:
                if isinstance(tc, dict):
                    out.append(tc)
                    inner = tc.get("function")
                    if isinstance(inner, dict):
                        out.append(inner)
    elif isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict):
                out.extend(_flatten_call_candidates(item))
    return out


_NAME_KEYS = ("tool", "tool_name", "name", "function_name")
_ARGS_KEYS = ("arguments", "args", "input", "parameters", "params", "inputs")


def _coerce_to_call(obj: Any) -> tuple[str, dict[str, Any]] | None:
    """Pull (name, args) out of one candidate dict. Returns None when
    no reasonable name field is present."""
    if not isinstance(obj, dict):
        return None
    name = None
    for key in _NAME_KEYS:
        v = obj.get(key)
        if isinstance(v, str) and v.strip():
            name = v.strip()
            break
    if not name:
        return None
    # Args under one of the recognised keys, OR the rest of the object
    # minus the name field (the {tool: "weather", city: "Suwon"} flat
    # shape — by far the most common).
    for key in _ARGS_KEYS:
        v = obj.get(key)
        if isinstance(v, dict):
            # Some models stringify `arguments` — try a JSON parse.
            return name, v
        if isinstance(v, str):
            inner = _safe_load_json(v)
            if isinstance(inner, dict):
                return name, inner
    flat_args = {
        k: v for k, v in obj.items() if k not in _NAME_KEYS and k not in _ARGS_KEYS
    }
    return name, flat_args


__all__ = ["PseudoToolCall", "extract_pseudo_tool_call"]


# Exposed for unit tests of the schema-shape matcher.
_match_to_schema_for_tests = _match_to_schema
