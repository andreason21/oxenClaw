"""Interactive model-picker wizard.

Mirrors the *select-default-model* flow in openclaw
`src/flows/model-picker.ts`, scoped to what oxenClaw exposes today:
provider catalog → catalog default model OR free-form override → optional
API key. The actual prompt I/O is injected (a `Prompter` Protocol) so the
flow itself is testable headless and the CLI command in
`oxenclaw.cli.flows_cmd` plugs in a typer-backed implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from oxenclaw.agents.factory import CATALOG_PROVIDERS, PROVIDER_DEFAULT_MODELS


@dataclass
class ModelPickerChoice:
    """The user's resolved selection."""

    provider: str
    model: str
    api_key: str | None = None
    base_url: str | None = None


class Prompter(Protocol):
    """Headless prompt surface — injected so unit tests can drive it."""

    def select(self, message: str, choices: list[str], *, default: str | None = None) -> str: ...

    def text(self, message: str, *, default: str | None = None, secret: bool = False) -> str: ...

    def confirm(self, message: str, *, default: bool = True) -> bool: ...


def pick_model_interactively(prompter: Prompter) -> ModelPickerChoice:
    """Walk the user through provider → model → optional credentials.

    Returns a `ModelPickerChoice` suitable for passing through to the
    factory or persisting in `config.yaml`.
    """
    sorted_providers = sorted(CATALOG_PROVIDERS)
    provider = prompter.select(
        "Which provider?",
        choices=sorted_providers,
        default="ollama" if "ollama" in sorted_providers else sorted_providers[0],
    )

    suggested_model = PROVIDER_DEFAULT_MODELS.get(provider, "")
    if suggested_model:
        accept = prompter.confirm(
            f"Use the catalog default model `{suggested_model}` for {provider}?",
            default=True,
        )
        model = (
            suggested_model
            if accept
            else prompter.text(f"Model id for {provider}", default=suggested_model)
        )
    else:
        # Hosted providers without a registered default require an
        # explicit answer (e.g. groq → user picks llama-3.1-70b-versatile).
        model = prompter.text(
            f"Model id for {provider}",
            default="",
        )

    base_url: str | None = None
    if provider in {
        "ollama",
        "vllm",
        "lmstudio",
        "llamacpp",
        "openai-compatible",
        "proxy",
        "litellm",
    }:
        change_url = prompter.confirm(
            f"Override the {provider} base URL? (default endpoint will be used otherwise)",
            default=False,
        )
        if change_url:
            base_url = prompter.text(f"{provider} base URL", default="")

    api_key: str | None = None
    if provider not in {
        "ollama",
        "vllm",
        "lmstudio",
        "llamacpp",
        "openai-compatible",
        "proxy",
        "litellm",
    }:
        # Hosted providers — ask for a key. Empty answer means "leave
        # the env var to provide it later" (EnvAuthStorage path).
        secret = prompter.text(
            f"{provider} API key (leave empty to read from env at run time)",
            default="",
            secret=True,
        )
        api_key = secret or None

    return ModelPickerChoice(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
    )


__all__ = ["ModelPickerChoice", "Prompter", "pick_model_interactively"]
