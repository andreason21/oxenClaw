"""Interactive setup flows + `doctor` health-check infrastructure.

Mirrors openclaw `src/flows/`. Three top-level surfaces:

- **Doctor** — aggregated health check across config, channels,
  providers, embeddings, cron, sessions, memory, isolation backends,
  and registered context engines. CLI: `oxenclaw doctor`.
- **Setup wizards** — interactive prompts for first-time configuration
  of a provider's credentials or the default model. CLI:
  `oxenclaw setup model`, `oxenclaw setup provider <id>`.
- **Flow contributions** — `FlowOption` / `FlowContribution` types
  shared with channel & provider plugins so their setup options surface
  in the wizard's UI without the wizard hard-coding the list.
"""

from oxenclaw.flows.doctor import (
    DoctorFinding,
    DoctorReport,
    DoctorSeverity,
    run_doctor,
)
from oxenclaw.flows.model_picker import (
    ModelPickerChoice,
    pick_model_interactively,
)
from oxenclaw.flows.provider_flow import (
    list_provider_flow_contributions,
)
from oxenclaw.flows.types import (
    FlowContribution,
    FlowContributionKind,
    FlowContributionSurface,
    FlowDocsLink,
    FlowOption,
    FlowOptionGroup,
    sort_flow_contributions_by_label,
)

__all__ = [
    "DoctorFinding",
    "DoctorReport",
    "DoctorSeverity",
    "FlowContribution",
    "FlowContributionKind",
    "FlowContributionSurface",
    "FlowDocsLink",
    "FlowOption",
    "FlowOptionGroup",
    "ModelPickerChoice",
    "list_provider_flow_contributions",
    "pick_model_interactively",
    "run_doctor",
    "sort_flow_contributions_by_label",
]
