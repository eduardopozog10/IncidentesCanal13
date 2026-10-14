
from django.test import TestCase

from incidents.ai.schemas import IncidentExtractionResult
from incidents.models import IncidentDraft, Technician, TechnicianChannel
from incidents.services.incident_processor import IncidentProcessor
from incidents.services.incoming_message_service import IncomingMessageService


class SequentialAIProvider:
    """Simula respuestas de Gemini sin consumir su API."""

    def __init__(self, responses):
        self.responses = list(responses)

    def extract_incident(self, message, previous_data=None):
        if not self.responses:
            raise AssertionError(
                "El bot consultó la IA más veces de lo previsto."
            )

        return self.responses.pop(0)


class ConversationDataQualityTests(TestCase):
    def setUp(self):
        self.reporter = Technician.objects.create(
            full_name="Reportante de prueba",
            is_active=True,
        )

        TechnicianChannel.objects.create(
            technician=self.reporter,
            provider=TechnicianChannel.Provider.TELEGRAM,
            external_user_id="conversation-qa",
            external_chat_id="conversation-qa",
            is_active=True,
        )

        self.message_number = 0

    def send(self, service, text):
        self.message_number += 1

        return service.process_text(
            provider="telegram",
            external_user_id="conversation-qa",
            external_chat_id="conversation-qa",
            external_message_id=f"qa-{self.message_number}",
            text=text,
            raw_payload={"test": True},
        )

    def test_separates_equipment_and_location_in_natural_reply(self):
        fake_ai = SequentialAIProvider(
            [
                IncidentExtractionResult(
                    intent="incident_report",
                    equipment="Cámara",
                    asset_category="video_cameras",
                    clarification_question=(
                        "¿Podrías indicarme en qué ubicación o contexto "
                        "se encuentra la cámara y qué comportamiento "
                        "anormal está presentando?"
                    ),
                ),
                IncidentExtractionResult(
                    intent="incident_followup",
                    equipment="Cámara 3",
                    location_context="Estudio 2",
                    unit="Estudio 2",
                    asset_category="video_cameras",
                    clarification_question=(
                        "¿A qué área corresponde el incidente?"
                    ),
                ),
            ]
        )

        service = IncomingMessageService(
            incident_processor=IncidentProcessor(
                ai_provider=fake_ai
            )
        )

        self.send(
            service,
            "Hola, tenemos un problema con una cámara.",
        )

        self.send(
            service,
            "Es la cámara 3, está en el Estudio 2.",
        )

        draft = IncidentDraft.objects.get(
            technician=self.reporter
        )

        self.assertEqual(
            draft.extracted_data["equipment"],
            "Cámara 3",
        )

        self.assertEqual(
            draft.extracted_data["location_context"],
            "Estudio 2",
        )

    def test_refined_ai_values_replace_less_precise_previous_values(self):
        IncidentDraft.objects.create(
            technician=self.reporter,
            status=IncidentDraft.Status.COLLECTING,
            last_question="¿Puedes dar más detalles del reporte?",
            extracted_data={
                "unit": "Cámara 3, está en el Estudio 2",
                "area": None,
                "location_context": "Cámara 3, está en el Estudio 2",
                "asset_category": "video_cameras",
                "equipment": "Cámara",
                "failure_type": None,
                "description": None,
                "operational_impact": None,
                "priority": None,
            },
            missing_fields=[
                "area",
                "failure_type",
                "description",
                "operational_impact",
                "priority",
            ],
        )

        fake_ai = SequentialAIProvider(
            [
                IncidentExtractionResult(
                    intent="incident_followup",
                    unit="Estudio 2",
                    location_context="Estudio 2",
                    equipment="Cámara 3",
                    asset_category="video_cameras",
                    failure_type="Imagen intermitente",
                    description=(
                        "La Cámara 3 pierde imagen por unos segundos."
                    ),
                    clarification_question=(
                        "¿A qué área corresponde el incidente?"
                    ),
                ),
            ]
        )

        service = IncomingMessageService(
            incident_processor=IncidentProcessor(
                ai_provider=fake_ai
            )
        )

        self.send(
            service,
            "La cámara 3 pierde imagen por unos segundos.",
        )

        draft = IncidentDraft.objects.get(
            technician=self.reporter
        )

        self.assertEqual(
            draft.extracted_data["equipment"],
            "Cámara 3",
        )

        self.assertEqual(
            draft.extracted_data["location_context"],
            "Estudio 2",
        )

        self.assertEqual(
            draft.extracted_data["unit"],
            "Estudio 2",
        )
