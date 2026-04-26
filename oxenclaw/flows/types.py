"""Shared types for flow contributions.

1:1 port of openclaw `src/flows/types.ts`. Channels and providers can
attach `FlowContribution` records to wizard surfaces (model-picker,
auth-choice, setup, health) so the host wizard renders without
hard-coding the option list. Plugins build these in their own setup
hook and the wizard merges + sorts them at render time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeVar

FlowContributionKind = Literal["channel", "core", "provider", "search"]
FlowContributionSurface = Literal["auth-choice", "health", "model-picker", "setup"]
AssistantVisibility = Literal["visible", "manual-only"]


@dataclass
class FlowDocsLink:
    """Link to a doc surface that explains the option."""

    path: str
    label: str | None = None


@dataclass
class FlowOptionGroup:
    """Visual grouping label for a list of options.

    The wizard renders entries inside the same group together with the
    group's `label` as a section header.
    """

    id: str
    label: str
    hint: str | None = None


@dataclass
class FlowOption:
    """One selectable entry on a wizard surface."""

    value: str
    label: str
    hint: str | None = None
    group: FlowOptionGroup | None = None
    docs: FlowDocsLink | None = None
    # Higher = sorted first when wizards group by assistant priority.
    assistant_priority: int = 0
    # `manual-only` options are hidden unless the user explicitly opts
    # into the manual flow (parity with openclaw's wizard semantics).
    assistant_visibility: AssistantVisibility = "visible"


@dataclass
class FlowContribution:
    """A wizard option contributed by a plugin or core module."""

    id: str
    kind: FlowContributionKind
    surface: FlowContributionSurface
    option: FlowOption
    source: str | None = None
    # Optional bag of plugin-specific metadata — e.g. provider id,
    # channel id, doc references that the wizard renders alongside.
    metadata: dict = field(default_factory=dict)


T = TypeVar("T", bound=FlowContribution)


def sort_flow_contributions_by_label(contributions: list[T]) -> list[T]:
    """Stable sort of a contribution list by `option.label`, then `option.value`.

    Mirrors openclaw `sortFlowContributionsByLabel` so the wizard's
    rendered order matches what TypeScript callers see.
    """
    return sorted(
        contributions,
        key=lambda c: (c.option.label.casefold(), c.option.value.casefold()),
    )


__all__ = [
    "AssistantVisibility",
    "FlowContribution",
    "FlowContributionKind",
    "FlowContributionSurface",
    "FlowDocsLink",
    "FlowOption",
    "FlowOptionGroup",
    "sort_flow_contributions_by_label",
]
