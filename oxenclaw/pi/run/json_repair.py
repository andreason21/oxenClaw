"""Best-effort repair for tool-argument JSON emitted by sloppy models.

Small local models (gemma2/3, qwen2.5:3b, llama3.1:8b) often emit
near-JSON with classic flaws — trailing commas, single quotes, missing
closing braces, unescaped newlines. Rather than punting back to the
model with `_parse_error`, try a small ladder of conservative repairs
first; only if every repair fails do we fall through to the original
"Re-emit with a JSON object literal." path.

Mirrors openclaw `attempt.tool-call-argument-repair.ts` in spirit
(simpler — TS version handles a wider schema-aware repair set).
"""

from __future__ import annotations

import json
import re
from typing import Any

# Order matters: cheaper / safer repairs first.
_REPAIR_STEPS: list[tuple[str, str]] = [
    # 1. Trailing commas before } or ]
    (r",(\s*[}\]])", r"\1"),
    # 2. Smart quotes → ascii double quotes
    (r"[“”]", '"'),
    (r"[‘’]", "'"),
]


def _strip_code_fence(s: str) -> str:
    """Some models wrap JSON in ```json ... ``` fences. Strip them."""
    s = s.strip()
    if s.startswith("```"):
        # Drop the opening fence (and optional language tag).
        first_newline = s.find("\n")
        if first_newline != -1:
            s = s[first_newline + 1 :]
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()


def _balance_braces(s: str) -> str:
    """If the model truncated mid-object, add the missing closers.

    Conservative — only adds at the END, never inside, and only for
    `{`/`[`. Quote-balance is left alone (too easy to corrupt valid
    strings containing braces)."""
    if not s:
        return s
    depth_curly = 0
    depth_square = 0
    in_string = False
    escape = False
    for ch in s:
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth_curly += 1
        elif ch == "}":
            depth_curly -= 1
        elif ch == "[":
            depth_square += 1
        elif ch == "]":
            depth_square -= 1
    suffix = ""
    if depth_square > 0:
        suffix += "]" * depth_square
    if depth_curly > 0:
        suffix += "}" * depth_curly
    return s + suffix


def _single_to_double_quotes(s: str) -> str:
    """Replace `'key': 'value'` with `"key": "value"` heuristically.

    Cheap regex pass — won't survive embedded apostrophes in values, but
    covers the common gemma/qwen single-quote case."""
    # Replace `'word'` with `"word"` only when surrounded by JSON
    # punctuation or whitespace. Avoids touching legit apostrophes in
    # natural-language values.
    return re.sub(r"(?<=[\s\{\[,:])'([^'\n]*?)'(?=[\s\}\],:])", r'"\1"', s)


def repair_and_parse(raw: str) -> tuple[Any | None, str]:
    """Try to parse `raw` as JSON, repairing common defects.

    Returns `(parsed, repair_summary)` on success; `(None, "")` on
    total failure. `repair_summary` is a short human label of the
    repair that succeeded (empty when no repair was needed) — gets
    surfaced in logs so operators can see how often models emit
    broken JSON in production.
    """
    if not raw or not raw.strip():
        return None, ""
    # Direct parse (the happy path — no repair needed).
    try:
        return json.loads(raw), ""
    except json.JSONDecodeError:
        pass

    # 1. Strip code fences.
    candidate = _strip_code_fence(raw)
    if candidate != raw:
        try:
            return json.loads(candidate), "code-fence"
        except json.JSONDecodeError:
            pass

    # 2. Apply regex-based repairs in sequence.
    repaired = candidate
    for pattern, replacement in _REPAIR_STEPS:
        repaired = re.sub(pattern, replacement, repaired)
    if repaired != candidate:
        try:
            return json.loads(repaired), "regex-cleanup"
        except json.JSONDecodeError:
            pass

    # 3. Single-quote → double-quote.
    sq_fixed = _single_to_double_quotes(repaired)
    if sq_fixed != repaired:
        try:
            return json.loads(sq_fixed), "single-quotes"
        except json.JSONDecodeError:
            pass
        repaired = sq_fixed

    # 4. Brace-balance for truncated payloads.
    balanced = _balance_braces(repaired)
    if balanced != repaired:
        try:
            return json.loads(balanced), "balance-braces"
        except json.JSONDecodeError:
            pass

    return None, ""


__all__ = ["repair_and_parse"]
