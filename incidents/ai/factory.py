from django.conf import settings

from .base import AIProvider
from .gemini_provider import GeminiAIProvider
from .mock_provider import MockAIProvider


def get_ai_provider() -> AIProvider:
    provider_name = settings.AI_PROVIDER.strip().lower()

    available_providers = {
        "mock": MockAIProvider,
        "gemini": GeminiAIProvider,
    }

    provider_class = available_providers.get(provider_name)

    if provider_class is None:
        available_names = ", ".join(
            available_providers.keys()
        )

        raise ValueError(
            f"Proveedor de IA no válido: {provider_name}. "
            f"Proveedores disponibles: {available_names}."
        )

    return provider_class() 