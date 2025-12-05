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
        # Future: Anthropic Claude
        raise NotImplementedError("Claude provider not yet implemented")
    elif provider == "gemini":
        # Future: Google Gemini
        raise NotImplementedError("Gemini provider not yet implemented")
    else:
        raise ValueError(f"Unknown AI provider: {provider}")


# Convenience exports
__all__ = ['get_ai_provider', 'AIProvider']
