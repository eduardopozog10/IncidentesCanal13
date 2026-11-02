from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class IncidentExtractionResult:
    intent: str = "incident_report"
    conversation_reply: str | None = None

    unit: str | None = None
    area: str | None = None
    location_context: str | None = None
    asset_category: str | None = None
    equipment: str | None = None
    failure_type: str | None = None
    description: str | None = None
    operational_impact: str | None = None
    priority: str | None = None

    # Evidencia literal del mensaje actual, no del borrador anterior. None
    # conserva compatibilidad con proveedores de prueba anteriores.
    field_evidence: dict[str, str] | None = None
    update_kind: str | None = None

    # Acto conversacional independiente de los hechos extraídos. Django
    # valida evidencia, contexto y permisos antes de efectuar cualquier acción.
    dialogue_action: str | None = None
    action_evidence: str | None = None
    requested_fields: list[str] = field(default_factory=list)
    help_field: str | None = None
    rewrite_clauses: list[str] = field(default_factory=list)
    dialogue_actions: list[str] = field(default_factory=list)
    authorization: dict | None = None
    operational_updates: list[dict] = field(default_factory=list)

    missing_fields: list[str] = field(default_factory=list)
    clarification_question: str | None = None

    raw_response: dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)
