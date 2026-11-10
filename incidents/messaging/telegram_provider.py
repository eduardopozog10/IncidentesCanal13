from typing import Any

import httpx
from django.conf import settings

from .base import MessagingProvider
from .presentation import telegram_html, utf16_length, display_text


class TelegramDeliveryUncertainError(RuntimeError):
    """No hay acuse suficiente para reenviar sin riesgo de duplicado."""


class TelegramRejectedError(RuntimeError):
    def __init__(self, retryable=True):
        super().__init__("Telegram rechazó el envío.")
        self.retryable = retryable


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

        if utf16_length(text) > 4096:
            raise ValueError(
                "El mensaje supera el máximo de 4096 caracteres."
            )

        formatted = telegram_html(text)
        payload = {'chat_id': recipient_id, 'text': formatted, 'parse_mode': 'HTML'}
        # La longitud de texto se mide tras interpretar etiquetas; si el payload
        # escapado es enorme se usa directamente la alternativa canónica.
        if len(formatted) > 12000:
            payload = {'chat_id': recipient_id, 'text': display_text(text)}
        return self._send(payload, plain_text=display_text(text))

    def _send(self, payload, *, plain_text):
        try:
            response = httpx.post(
                f"{self.api_url}/sendMessage",
                json=payload,
                timeout=self.timeout,
            )
        except httpx.RequestError:
            raise TelegramDeliveryUncertainError(
                "No fue posible conectar con Telegram."
            ) from None

        try:
            response_data = response.json()
        except ValueError:
            raise TelegramDeliveryUncertainError(
                "Telegram devolvió una respuesta inválida."
            ) from None

        if not isinstance(response_data, dict):
            raise TelegramDeliveryUncertainError('Telegram devolvió un acuse inválido.')

        if response_data.get("ok") is False:
            code = response_data.get("error_code", response.status_code)
            description = str(response_data.get('description', '')).lower()
            if (code == 400 and payload.get('parse_mode') and
                    any(term in description for term in ('parse entities', 'unsupported start tag', 'entity', 'text is too long', 'parse mode', 'parse_mode', 'end tag'))):
                # Solo un rechazo explícito de formato permite este segundo envío.
                # Nunca reenviar ante timeout, 429, 5xx ni una entrega incierta.
                return self._send({'chat_id':payload['chat_id'], 'text':plain_text}, plain_text=plain_text)
            raise TelegramRejectedError(retryable=code == 429 or code >= 500)
        if response.status_code >= 400 or not response_data.get("ok"):
            raise TelegramDeliveryUncertainError("Respuesta de envío no confirmada.")

        return response_data
