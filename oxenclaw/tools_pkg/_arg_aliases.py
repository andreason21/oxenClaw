"""Shared LLM tool-arg drift absorber.

Small local models (gemma/qwen/llama-3.2) routinely emit drifted
argument keys — `{location: ...}` instead of `{city: ...}`,
`{cmd: ...}` instead of `{command: ...}`, `{file: ...}` instead of
`{path: ...}`. The strict pydantic field rejects with
ValidationError, the model rarely retries with the right shape, and
the tool effectively never fires.

`fold_aliases(data, mapping)` is the building block each tool's
input model can call from a `model_validator(mode="before")`. It
folds the first truthy alias onto the canonical field name and
strips all alias keys so `model_config = {"extra": "forbid"}`-style
configs don't trip on the leftovers.

The reference call sites are `tools_pkg/web._SearchArgs` and
`tools_pkg/weather._WeatherArgs` — both inline the absorber for
historical reasons; new tools should use this helper.
"""

from __future__ import annotations

from typing import Any


def fold_aliases(data: Any, mapping: dict[str, tuple[str, ...]]) -> Any:
    """Fold drift-aliased keys onto canonical field names.

    Parameters
    ----------
    data:
        Whatever pydantic passed to a `model_validator(mode="before")`.
        We only transform `dict`s; anything else is returned unchanged
        so explicit-construction paths (`Model(field=value)`) still
        work.
    mapping:
        `{canonical_field: (alias_1, alias_2, ...)}`. For each
        canonical field whose value is missing/falsy, the FIRST alias
        with a truthy value is moved over. All alias keys are popped
        afterwards (whether or not they contributed) so the leftover
        dict only contains canonical names.
    """
    if not isinstance(data, dict):
        return data
    out = dict(data)
    for canonical, aliases in mapping.items():
        # Only fold if the canonical isn't already populated.
        if not _is_truthy(out.get(canonical)):
            for alias in aliases:
                v = out.get(alias)
                if _is_truthy(v):
                    out[canonical] = v
                    break
        # Strip alias keys regardless — keeps extra=forbid happy.
        for alias in aliases:
            out.pop(alias, None)
    return out


def _is_truthy(v: Any) -> bool:
    """Truthy for fold purposes: non-empty string, non-None, non-zero.

    `False` and `0` are treated as truthy so callers can still pass
    booleans/ints through aliases if they ever need to (none of the
    current tools do, but it preserves intent)."""
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    return True


__all__ = ["fold_aliases"]
