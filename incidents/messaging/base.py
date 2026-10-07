from abc import ABC, abstractmethod
from typing import Any


class MessagingProvider(ABC):
    """
    Contrato común para todos los canales de mensajería.

    Telegram, WhatsApp u otro proveedor deberán implementar
    los mismos métodos para que el resto del sistema no dependa
    de una plataforma específica.
    """

    provider_name: str

    @abstractmethod
    def send_text(
        self,
        recipient_id: str,
        text: str,
    ) -> dict[str, Any]:
        """
        Envía un mensaje de texto al destinatario indicado.

        Debe devolver la respuesta original del proveedor.
        """
        raise NotImplementedError