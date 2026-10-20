from abc import ABC, abstractmethod

from .schemas import IncidentExtractionResult


class AITemporaryError(TimeoutError):
    """Fallo recuperable del proveedor, con pausa antes de otra solicitud."""

    def __init__(self, message, *, retry_after=30):
        super().__init__(message)
        self.retry_after = retry_after


class AIUnavailableError(AITemporaryError):
    """Proveedor sin disponibilidad o cuota temporalmente agotada."""


class AIConfigurationError(ValueError):
    """Solicitud/configuración rechazada; no se resuelve repitiéndola."""


class AIProvider(ABC):
    @abstractmethod
    def extract_incident(
        self,
        message: str,
        previous_data: dict | None = None,
    ) -> IncidentExtractionResult:
        """
        Extrae información estructurada desde el mensaje de un reportante.

        Cada proveedor de IA debe implementar este método y devolver
        siempre un IncidentExtractionResult.
        """
        raise NotImplementedError
