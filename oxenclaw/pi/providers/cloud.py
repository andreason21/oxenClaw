"""Hosted cloud providers: ``openai``, ``gemini``, ``azure-openai``.

oxenClaw is local-first — the default catalog is on-host. These three hosted
providers are opt-in: they require an API key (resolved from
``<PROVIDER>_API_KEY`` via ``AuthStorage`` / env) and only fire when an agent
is explicitly configured with the matching ``provider:``.

All three speak the OpenAI chat-completions SSE shape, so they reuse
``stream_openai_compatible``:

- ``openai``       — ``https://api.openai.com/v1``; ``Authorization: Bearer``.
- ``gemini``       — Google's OpenAI-compatibility endpoint
                     ``https://generativelanguage.googleapis.com/v1beta/openai``;
                     ``Authorization: Bearer`` with a Gemini API key.
- ``azure-openai`` — an Azure OpenAI resource. The deployment name and
                     ``api-version`` live in the URL, and auth is the
                     ``api-key`` header (not Bearer). The resource endpoint
                     (``https://<resource>.openai.azure.com``) is supplied via
                     ``model.extra['base_url']`` / ``--base-url``; the
                     deployment + api-version come from ``model.extra`` or the
                     ``OXENCLAW_AZURE_DEPLOYMENT`` / ``OXENCLAW_AZURE_API_VERSION``
                     env vars.
"""

from __future__ import annotations

import os

from oxenclaw.pi.providers._openai_shared import stream_openai_compatible
from oxenclaw.pi.streaming import register_provider_stream

# Azure changes its REST contract per api-version; this is a recent stable GA
# version that supports streaming + tool calls. Override per deployment.
_DEFAULT_AZURE_API_VERSION = "2024-10-21"


def _bearer_streamfn():  # type: ignore[no-untyped-def]
    """OpenAI / Gemini: standard Bearer-auth OpenAI-compatible stream."""

    async def _fn(ctx, opts):  # type: ignore[no-untyped-def]
        async for ev in stream_openai_compatible(ctx, opts):
            yield ev

    return _fn


def _azure_streamfn():  # type: ignore[no-untyped-def]
    """Azure OpenAI: deployment-in-URL + ``api-key`` header.

    Builds ``/openai/deployments/<deployment>/chat/completions?api-version=<ver>``
    on top of the resource endpoint base URL. The deployment defaults to the
    model id when not given explicitly (Azure deployments are often named after
    the model).
    """

    async def _fn(ctx, opts):  # type: ignore[no-untyped-def]
        extra = getattr(ctx.model, "extra", None) or {}
        deployment = (
            extra.get("azure_deployment")
            or extra.get("deployment")
            or os.environ.get("OXENCLAW_AZURE_DEPLOYMENT")
            or ctx.model.id
        )
        api_version = (
            extra.get("api_version")
            or os.environ.get("OXENCLAW_AZURE_API_VERSION")
            or _DEFAULT_AZURE_API_VERSION
        )
        path = f"/openai/deployments/{deployment}/chat/completions?api-version={api_version}"
        async for ev in stream_openai_compatible(
            ctx, opts, path=path, auth_scheme="azure"
        ):
            yield ev

    return _fn


register_provider_stream("openai", _bearer_streamfn())
register_provider_stream("gemini", _bearer_streamfn())
register_provider_stream("azure-openai", _azure_streamfn())
