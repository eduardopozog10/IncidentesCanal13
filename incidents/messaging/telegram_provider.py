from typing import Any

import httpx
from django.conf import settings

from .base import MessagingProvider


class TelegramProvider(MessagingProvider):
    provider_name = "telegram"

    def __init__(
        self,
        token: str | None = None,
        timeout: float = 15.0,
    ):
        self.token = (
            token
            or settings.TELEGRAM_BOT_TOKEN
        ).strip()
        self.timeout = timeout

        if not self.token:
            raise ValueError(
                "El token de Telegram no está configurado."
            )

        self.api_url = (
            f"https://api.telegram.org/bot{self.token}"
        )

    def send_text(
        self,
        recipient_id: str,
        text: str,
    ) -> dict[str, Any]:
        recipient_id = str(recipient_id).strip()
        text = text.strip()

        if not recipient_id:
            raise ValueError(
                "El identificador del destinatario es obligatorio."
            )

        if not text:
            raise ValueError(
                "El mensaje de Telegram no puede estar vacío."
            )

        if len(text) > 4096:
            raise ValueError(
                "El mensaje supera el máximo de 4096 caracteres."
            )

        try:
            response = httpx.post(
                f"{self.api_url}/sendMessage",
                json={
                    "chat_id": recipient_id,
                    "text": text,
                },
                timeout=self.timeout,
            )
        except httpx.RequestError:
            raise RuntimeError(
                "No fue posible conectar con Telegram."
            ) from None

        try:
            response_data = response.json()
        except ValueError:
            raise RuntimeError(
                "Telegram devolvió una respuesta inválida."
            ) from None

        if response.status_code >= 400 or not response_data.get("ok"):
            error_description = response_data.get(
                "description",
                "Error desconocido de Telegram.",
            )

            raise RuntimeError(
                f"Telegram rechazó el mensaje: {error_description}"
            )

        return response_data