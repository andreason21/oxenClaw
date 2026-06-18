"""Shared hosted-provider configuration flow.

One place that turns "I want to use OpenAI / Gemini / Azure" into a saved
credential: prompt for the API key (and a resource-specific base URL where
there's no bundled default, e.g. Azure), persist `<PROVIDER>_API_KEY` to
`~/.oxenclaw/env` (mode 0600) so the gateway reads it via `EnvAuthStorage`
on the next start, and print the matching `gateway start` command.

Both `oxenclaw setup provider <id>` and the one-shot `oxenclaw setup`
bootstrap delegate here so the two entry points stay in lock-step. The
prompt I/O is injected (`Prompter` + an `emit` callback) so the flow is
unit-testable headless.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from oxenclaw.flows.model_picker import Prompter


@dataclass
class HostedProviderSetup:
    """Outcome of `configure_hosted_provider` — what was chosen + saved."""

    provider: str
    model: str
    base_url: str | None
    env_var: str
    api_key_saved: bool
    env_path: Path | None


def configure_hosted_provider(
    provider_id: str,
    *,
    prompter: Prompter,
    emit: Callable[[str], None],
    ask_model: bool = True,
) -> HostedProviderSetup:
    """Prompt for + persist the credentials a hosted provider needs.

    `ask_model=False` skips the model prompt and keeps the catalog default
    (used by the bootstrap, which keeps the flow short).
    """
    from oxenclaw.agents.factory import PROVIDER_DEFAULT_MODELS
    from oxenclaw.config.env_loader import persist_env_var
    from oxenclaw.pi.auth import _HOSTED_DEFAULT_BASE_URL  # type: ignore[attr-defined]
    from oxenclaw.pi.registry import EnvAuthStorage

    default_model = PROVIDER_DEFAULT_MODELS.get(provider_id, "")
    model = default_model
    if ask_model:
        model = prompter.text(f"Model id for {provider_id}", default=default_model) or default_model

    # Resource-specific providers (Azure OpenAI) have no bundled endpoint and
    # must be told their base URL; providers with a default may override it
    # later via config / --base-url but we don't prompt here.
    base_url: str | None = None
    if provider_id not in _HOSTED_DEFAULT_BASE_URL:
        base_url = (
            prompter.text(
                f"{provider_id} base URL (required — e.g. https://<resource>.openai.azure.com)",
                default="",
            )
            or None
        )

    env_var = EnvAuthStorage._env_key(provider_id)  # type: ignore[attr-defined]
    key = prompter.text(
        f"{provider_id} API key (saved to ~/.oxenclaw/env as {env_var}; "
        "leave empty to set it yourself later)",
        default="",
        secret=True,
    )
    env_path: Path | None = None
    if key:
        env_path = persist_env_var(env_var, key)
        emit(f"  [OK]    saved {env_var} → {env_path} (mode 0600)")
    else:
        emit("  No key entered. Export it before starting, e.g.:")
        emit(f"    export {env_var}='<your key>'")

    start = f"oxenclaw gateway start --provider {provider_id} --model {model or '<model>'}"
    if base_url:
        start += f" --base-url {base_url}"
    emit(f"  Then start with: {start}")
    emit("  Verify with: oxenclaw doctor")
    return HostedProviderSetup(
        provider=provider_id,
        model=model,
        base_url=base_url,
        env_var=env_var,
        api_key_saved=bool(key),
        env_path=env_path,
    )


__all__ = ["HostedProviderSetup", "configure_hosted_provider"]
