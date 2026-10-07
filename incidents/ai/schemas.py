from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class IncidentExtractionResult:
    intent: str = "incident_report"
    conversation_reply: str | None = None

    unit: str | None = None
    equipment: str | None = None
    failure_type: str | None = None
    description: str | None = None
    priority: str | None = None

    missing_fields: list[str] = field(default_factory=list)
    clarification_question: str | None = None

    raw_response: dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)