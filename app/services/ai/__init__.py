from app.core.config import settings
from app.services.ai.base import AIProvider
from app.services.ai.openai_provider import OpenAIProvider


def get_ai_provider() -> AIProvider:
    """
    Get AI provider based on configuration
    Allows easy switching between providers
    """
    provider = settings.AI_PROVIDER.lower()
    
    if provider == "openai":
        return OpenAIProvider()
    elif provider == "claude":
        from app.services.ai.claude_provider import ClaudeProvider
        return ClaudeProvider()
    elif provider == "gemini":
        from app.services.ai.gemini_provider import GeminiProvider
        return GeminiProvider()
    else:
        raise ValueError(f"Unknown AI provider: {provider}")


def get_ai_provider_named(provider: str, model: str | None = None) -> AIProvider:
    """A specific provider, optionally overriding its model.

    `get_ai_provider()` reads the single global setting and is what everything
    used before routing existed. The router needs to ask for a *particular*
    model — a cheap one first, a stronger one on fallback — which is what this
    adds. The global function stays, so nothing that was working has to change.
    """
    provider = (provider or "").lower()
    if provider == "openai":
        instance = OpenAIProvider()
    elif provider == "claude":
        from app.services.ai.claude_provider import ClaudeProvider
        instance = ClaudeProvider()
    elif provider == "gemini":
        from app.services.ai.gemini_provider import GeminiProvider
        instance = GeminiProvider()
    else:
        raise ValueError(f"Unknown AI provider: {provider}")

    if model:
        # Every provider stores its model on `self.model`; overriding it is how
        # one provider serves both tiers of the routing table.
        instance.model = model
    return instance


# Convenience exports
__all__ = ['get_ai_provider', 'get_ai_provider_named', 'AIProvider']
