"""Provider plugin → wizard flow contributions.

Mirrors openclaw `src/flows/provider-flow.ts`. Returns the catalog
provider list as `FlowContribution`s so the model-picker / setup
wizard can render them without hard-coding. Plugins that add a new
provider in `oxenclaw/pi/providers/` automatically show up here once
the corresponding entry is added to `factory.CATALOG_PROVIDERS`.

The catalog is on-host only (cloud providers were removed
2026-04-29); every entry below renders under the single "Local /
inline" group.
"""

from __future__ import annotations

from oxenclaw.agents.factory import (
    CATALOG_PROVIDERS,
    PROVIDER_DEFAULT_MODELS,
)
from oxenclaw.flows.types import (
    FlowContribution,
    FlowOption,
    FlowOptionGroup,
    sort_flow_contributions_by_label,
)

_LOCAL = FlowOptionGroup(id="local", label="Local / inline", hint="On-host inference servers")

_GROUP_BY_PROVIDER: dict[str, FlowOptionGroup] = {
    "ollama": _LOCAL,
    "llamacpp-direct": _LOCAL,
    "llamacpp": _LOCAL,
    "vllm": _LOCAL,
    "lmstudio": _LOCAL,
}


def list_provider_flow_contributions() -> list[FlowContribution]:
    """Build a `FlowContribution` per catalog provider.

    The `option.value` is the provider id; `option.hint` carries the
    default model the wizard would pre-fill if the user accepts the
    suggestion. Already sorted by label.
    """
    out: list[FlowContribution] = []
    for provider in CATALOG_PROVIDERS:
        default_model = PROVIDER_DEFAULT_MODELS.get(provider)
        hint = f"default model: {default_model}" if default_model else "model required"
        out.append(
            FlowContribution(
                id=f"provider:{provider}",
                kind="provider",
                surface="setup",
                source="catalog",
                option=FlowOption(
                    value=provider,
                    label=provider,
                    hint=hint,
                    group=_GROUP_BY_PROVIDER.get(provider, _LOCAL),
                ),
                metadata={"provider": provider, "default_model": default_model},
            )
        )
    return sort_flow_contributions_by_label(out)


__all__ = ["list_provider_flow_contributions"]
