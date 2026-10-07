from abc import ABC, abstractmethod

from .schemas import IncidentExtractionResult


class AIProvider(ABC):
    @abstractmethod
    def extract_incident(
        self,
        message: str,
        previous_data: dict | None = None,
    ) -> IncidentExtractionResult:
        """
        Extrae información estructurada desde el mensaje de un técnico.

        Cada proveedor de IA debe implementar este método y devolver
        siempre un IncidentExtractionResult.
        """
        raise NotImplementedError