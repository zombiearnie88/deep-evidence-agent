"""Provider catalog and environment mapping for LiteLLM usage."""

from __future__ import annotations

from evidence_compiler.models import ProviderOption

PROVIDER_CATALOG: dict[str, ProviderOption] = {
    "openai": ProviderOption(
        provider_id="openai",
        label="OpenAI",
        description="Direct OpenAI API models",
        model_examples=["gpt-5.4-mini", "gpt-5.4"],
    ),
    "anthropic": ProviderOption(
        provider_id="anthropic",
        label="Anthropic",
        description="Claude models via LiteLLM anthropic/* format",
        model_examples=["anthropic/claude-sonnet-4-6", "anthropic/claude-opus-4-6"],
    ),
    "gemini": ProviderOption(
        provider_id="gemini",
        label="Google Gemini",
        description="Gemini models via LiteLLM gemini/* format",
        model_examples=[
            "gemini/gemini-3.1-pro-preview",
            "gemini/gemini-3-flash-preview",
        ],
    ),
    "xai": ProviderOption(
        provider_id="xai",
        label="xAI",
        description="xAI Grok models via LiteLLM xai/* format",
        model_examples=["xai/grok-4", "xai/grok-3-mini"],
    ),
    "vercel_ai_gateway": ProviderOption(
        provider_id="vercel_ai_gateway",
        label="Vercel AI Gateway",
        description="Unified gateway models via LiteLLM vercel_ai_gateway/* format",
        model_examples=[
            "vercel_ai_gateway/openai/gpt-5.4-mini",
            "vercel_ai_gateway/anthropic/claude-sonnet-4-6",
        ],
    ),
}

PROVIDER_ENV_VARS: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "xai": ("XAI_API_KEY",),
    "vercel_ai_gateway": ("VERCEL_AI_GATEWAY_API_KEY",),
}


def normalize_provider(provider: str | None) -> str:
    """Return normalized provider id with openai fallback."""
    value = (provider or "openai").strip().lower()
    if value not in PROVIDER_CATALOG:
        raise ValueError(f"Unsupported provider: {provider}")
    return value


def list_provider_options() -> list[ProviderOption]:
    """Return sorted provider options for API/UI consumption."""
    return [PROVIDER_CATALOG[key] for key in sorted(PROVIDER_CATALOG.keys())]
