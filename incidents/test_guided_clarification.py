from django.test import TestCase

from incidents.ai.schemas import IncidentExtractionResult
from incidents.models import (
    Incident,
    IncidentDraft,
    Technician,
    TechnicianChannel,
)
from incidents.services.incident_processor import IncidentProcessor
from incidents.services.incoming_message_service import (
    IncomingMessageService,
)


class FakeAIProvider:
    """Simula respuestas de Gemini sin consumir la API."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])

    def extract_incident(self, message, previous_data=None):
        if not self.responses:
            raise AssertionError(
                "No se esperaba una consulta a Gemini."
            )

        return self.responses.pop(0)


class GuidedClarificationTests(TestCase):
    def setUp(self):
        self.reporter = Technician.objects.create(
            full_name="Reportante de prueba",
            is_active=True,
        )

        TechnicianChannel.objects.create(
            technician=self.reporter,
            provider="telegram",
            external_user_id="guided-test-user",
            external_chat_id="guided-test-chat",
            is_active=True,
        )

        self.message_number = 0

        self.data = {
            "unit": "Estudio 2",
            "area": "Producción",
            "location_context": "Estudio 2",
            "asset_category": "video_cameras",
            "equipment": "Cámara 3",
            "failure_type": "Imagen intermitente",
            "description": (
                "La cámara pierde imagen por segundos."
            ),
            "operational_impact": None,
            "priority": None,
        }

    def create_draft(
        self,
        question,
        missing_fields,
    ):
        return IncidentDraft.objects.create(
            technician=self.reporter,
            status=IncidentDraft.Status.COLLECTING,
            last_question=question,
            missing_fields=missing_fields,
            extracted_data=dict(self.data),
        )

    def send(
        self,
        service,
        text,
    ):
        self.message_number += 1

        return service.process_text(
            provider="telegram",
            external_user_id="guided-test-user",
            external_chat_id="guided-test-chat",
            external_message_id=(
                f"guided-{self.message_number}"
            ),
            text=text,
            raw_payload={
                "test": True,
            },
        )["processing"]

    def test_explains_when_user_does_not_understand(self):
        draft = self.create_draft(
            (
                "¿Qué impacto operacional está generando "
                "la falla?"
            ),
            [
                "operational_impact",
                "priority",
            ],
        )

        service = IncomingMessageService(
            incident_processor=IncidentProcessor(
                ai_provider=FakeAIProvider()
            )
        )

        result = self.send(
            service,
            "No entiendo",
        )

        self.assertEqual(
            result["action"],
            "ask_clarification",
        )

        self.assertIn(
            "Por ejemplo",
            result["reply"],
        )

        self.assertIn(
            "grabación",
            result["reply"],
        )

        draft.refresh_from_db()

        self.assertIsNone(
            draft.extracted_data[
                "operational_impact"
            ]
        )

        self.assertEqual(
            Incident.objects.count(),
            0,
        )

        next_result = self.send(
            service,
            "Interrumpe la grabación",
        )

        self.assertEqual(
            next_result[
                "extracted_data"
            ]["operational_impact"],
            "Interrumpe la grabación",
        )

    def test_guides_after_unhelpful_answer(self):
        self.create_draft(
            (
                "¿Qué impacto operacional está generando "
                "la falla?"
            ),
            [
                "operational_impact",
                "priority",
            ],
        )

        fake_ai = FakeAIProvider(
            [
                IncidentExtractionResult(
                    intent="incident_followup",
                    clarification_question=(
                        "¿Qué impacto operacional "
                        "tiene la falla?"
                    ),
                )
            ]
        )

        service = IncomingMessageService(
            incident_processor=IncidentProcessor(
                ai_provider=fake_ai
            )
        )

        result = self.send(
            service,
            "Tengo hambre",
        )

        self.assertIn(
            "Por ejemplo",
            result["reply"],
        )

        self.assertIsNone(
            result[
                "extracted_data"
            ]["operational_impact"]
        )

        self.assertEqual(
            Incident.objects.count(),
            0,
        )

    def test_keeps_useful_information_out_of_order(self):
        self.data["area"] = None
        self.data["unit"] = None
        self.data["location_context"] = None
        self.data["operational_impact"] = (
            "Interrumpe la grabación"
        )
        self.data["priority"] = (
            Incident.Priority.HIGH
        )

        self.create_draft(
            "¿A qué área corresponde el incidente?",
            [
                "area",
                "location_context",
            ],
        )

        fake_ai = FakeAIProvider(
            [
                IncidentExtractionResult(
                    intent="incident_followup",
                    location_context="Estudio 2",
                    unit="Estudio 2",
                    clarification_question=(
                        "¿A qué área corresponde "
                        "el incidente?"
                    ),
                )
            ]
        )

        service = IncomingMessageService(
            incident_processor=IncidentProcessor(
                ai_provider=fake_ai
            )
        )

        result = self.send(
            service,
            "Estudio 2",
        )

        self.assertEqual(
            result[
                "extracted_data"
            ]["location_context"],
            "Estudio 2",
        )

        self.assertIn(
            "area",
            result["missing_fields"],
        )

        self.assertNotEqual(
            result["reply"],
            IncidentProcessor.GUIDED_QUESTIONS[
                "area"
            ],
        )

    def test_explains_short_confusion_messages(self):
        draft = self.create_draft(
            (
                "¿Qué impacto operacional está generando "
                "la falla?"
            ),
            [
                "operational_impact",
                "priority",
            ],
        )

        service = IncomingMessageService(
            incident_processor=IncidentProcessor(
                ai_provider=FakeAIProvider()
            )
        )

        for text in (
            "¿Qué?",
            "¿Cómo?",
            "¿Cómo así?",
        ):
            with self.subTest(message=text):
                result = self.send(
                    service,
                    text,
                )

                self.assertEqual(
                    result["action"],
                    "ask_clarification",
                )

                self.assertIn(
                    "Por ejemplo",
                    result["reply"],
                )

                self.assertIn(
                    "impacto operacional",
                    result["reply"],
                )

                self.assertFalse(
                    result["incident_created"],
                )

                draft.refresh_from_db()

                self.assertIsNone(
                    draft.extracted_data[
                        "operational_impact"
                    ]
                )

        self.assertEqual(
            Incident.objects.count(),
            0,
        )

    def test_explains_priority_without_assigning_it(self):
        self.data["operational_impact"] = (
            "Interrumpe la grabación"
        )

        self.create_draft(
            (
                "¿Qué prioridad le asignas: "
                "Baja, Media, Alta o Crítica?"
            ),
            ["priority"],
        )

        service = IncomingMessageService(
            incident_processor=IncidentProcessor(
                ai_provider=FakeAIProvider()
            )
        )

        result = self.send(
            service,
            "No sé qué responder",
        )

        self.assertIn(
            "Baja, Media, Alta o Crítica",
            result["reply"],
        )

        self.assertIsNone(
            result[
                "extracted_data"
            ]["priority"]
        )

        self.assertEqual(
            Incident.objects.count(),
            0,
        )