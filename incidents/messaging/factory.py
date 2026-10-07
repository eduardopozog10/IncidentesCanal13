from .base import MessagingProvider
from .telegram_provider import TelegramProvider


def get_messaging_provider(
    provider_name: str,
) -> MessagingProvider:
    normalized_name = provider_name.strip().lower()

    available_providers = {
        "telegram": TelegramProvider,
    }

    provider_class = available_providers.get(
        normalized_name,
    )

    if provider_class is None:
        available_names = ", ".join(
            available_providers.keys()
        )

        raise ValueError(
            f"Proveedor de mensajería no válido: "
            f"{normalized_name}. "
            f"Proveedores disponibles: {available_names}."
        )

    return provider_class()