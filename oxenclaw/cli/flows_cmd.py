"""CLI surface for the `flows` subsystem: `oxenclaw doctor` + `oxenclaw setup ...`."""

from __future__ import annotations

import sys

import typer

from oxenclaw.flows import (
    DoctorReport,
    DoctorSeverity,
    pick_model_interactively,
    run_doctor,
)

doctor_app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    help="Aggregated health check across config / providers / channels / etc.",
)
setup_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Interactive setup wizards (provider, model, channel).",
)


_SEVERITY_GLYPH: dict[DoctorSeverity, str] = {
    "ok": "OK   ",
    "warn": "WARN ",
    "error": "ERROR",
}


def _render_doctor_text(report: DoctorReport) -> str:
    """Plain-text rendering for `oxenclaw doctor` (no color deps)."""
    lines: list[str] = []
    width = max((len(f.area) for f in report.findings), default=10)
    for f in report.findings:
        glyph = _SEVERITY_GLYPH.get(f.severity, f.severity.upper())
        lines.append(f"  [{glyph}] {f.area.ljust(width)}  {f.message}")
        if f.detail:
            lines.append(f"           {' ' * width}    ↳ {f.detail}")
    summary = (
        f"{len(report.findings)} checks — {len(report.errors)} error / {len(report.warnings)} warn"
    )
    lines.append("")
    lines.append(summary)
    return "\n".join(lines)


@doctor_app.callback(invoke_without_command=True)
def doctor(
    skip_embeddings: bool = typer.Option(
        False,
        "--skip-embeddings",
        help="Skip the 5-second embedding endpoint probe (offline diagnostics).",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON instead of the text table.",
    ),
) -> None:
    """Run every health probe and print a human-readable report.

    Exit code: 0 if no errors, 1 otherwise. Warnings do not affect the
    exit code so existing automation that calls `oxenclaw doctor` only
    fails on hard errors.
    """
    report = run_doctor(probe_embeddings=not skip_embeddings)
    if json_output:
        import json

        data = {
            "ok": report.ok,
            "findings": [
                {
                    "area": f.area,
                    "severity": f.severity,
                    "message": f.message,
                    "detail": f.detail,
                }
                for f in report.findings
            ],
        }
        typer.echo(json.dumps(data, indent=2))
    else:
        typer.echo(_render_doctor_text(report))
    raise typer.Exit(code=0 if report.ok else 1)


class _TyperPrompter:
    """Adapts `typer.prompt` / `typer.confirm` to the `Prompter` Protocol."""

    def select(self, message: str, choices: list[str], *, default: str | None = None) -> str:
        typer.echo(message)
        for i, c in enumerate(choices, 1):
            typer.echo(f"  {i:>2}. {c}")
        while True:
            raw = typer.prompt(
                "  pick (number or name)",
                default=default or "",
                show_default=bool(default),
            )
            raw = (raw or "").strip()
            if raw in choices:
                return raw
            if raw.isdigit():
                idx = int(raw)
                if 1 <= idx <= len(choices):
                    return choices[idx - 1]
            typer.echo("  not in list; try again")

    def text(self, message: str, *, default: str | None = None, secret: bool = False) -> str:
        return typer.prompt(
            message,
            default=default if default is not None else "",
            show_default=bool(default),
            hide_input=secret,
        )

    def confirm(self, message: str, *, default: bool = True) -> bool:
        return typer.confirm(message, default=default)


@setup_app.command("model")
def setup_model() -> None:
    """Pick a default provider + model interactively."""
    choice = pick_model_interactively(_TyperPrompter())
    typer.echo("")
    typer.echo("Selection:")
    typer.echo(f"  provider: {choice.provider}")
    typer.echo(f"  model:    {choice.model}")
    if choice.base_url:
        typer.echo(f"  base_url: {choice.base_url}")
    if choice.api_key:
        typer.echo("  api_key:  (set, will be persisted via auth storage)")
    typer.echo("")
    typer.echo("To apply: pass these to `oxenclaw gateway start`:")
    bits = [f"--provider {choice.provider}", f"--model {choice.model}"]
    if choice.base_url:
        bits.append(f"--base-url {choice.base_url}")
    if choice.api_key:
        bits.append("--api-key '<paste your key>'")
    typer.echo(f"  oxenclaw gateway start {' '.join(bits)}")


@setup_app.command("provider")
def setup_provider(
    provider_id: str = typer.Argument(..., help="Catalog provider id (e.g. anthropic, openai)."),
) -> None:
    """Show what oxenClaw needs to use a given catalog provider.

    For inline providers (Ollama / vLLM / lmstudio / etc.) reports the
    default base URL. For hosted providers reports the env var name
    that EnvAuthStorage reads on startup.
    """
    from oxenclaw.agents.factory import CATALOG_PROVIDERS, PROVIDER_DEFAULT_MODELS
    from oxenclaw.pi.auth import _HOSTED_DEFAULT_BASE_URL  # type: ignore[attr-defined]
    from oxenclaw.pi.registry import EnvAuthStorage, is_inline_provider

    if provider_id not in CATALOG_PROVIDERS:
        typer.echo(f"unknown provider: {provider_id}", err=True)
        typer.echo(f"known: {', '.join(sorted(CATALOG_PROVIDERS))}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Provider: {provider_id}")
    default_model = PROVIDER_DEFAULT_MODELS.get(provider_id)
    if default_model:
        typer.echo(f"  default model: {default_model}")
    if is_inline_provider(provider_id):
        typer.echo("  kind: inline (no API key required)")
        typer.echo(f"  default base_url: {_INLINE_BASE_URL_HINT.get(provider_id, '(per-model)')}")
    else:
        typer.echo("  kind: hosted (API key required)")
        env_var = EnvAuthStorage._env_key(provider_id)  # type: ignore[attr-defined]
        typer.echo(f"  env var: {env_var}")
        typer.echo(
            f"  default base_url: {_HOSTED_DEFAULT_BASE_URL.get(provider_id, '(plugin-defined)')}"
        )


_INLINE_BASE_URL_HINT: dict[str, str] = {
    "ollama": "http://127.0.0.1:11434/v1",
    "vllm": "http://127.0.0.1:8000/v1",
    "lmstudio": "http://127.0.0.1:1234/v1",
    "llamacpp": "http://127.0.0.1:8080/v1",
    "litellm": "http://127.0.0.1:4000/v1",
}


__all__ = ["doctor", "doctor_app", "setup_app", "setup_model", "setup_provider"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(0)
