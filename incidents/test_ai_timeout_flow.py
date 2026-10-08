from django.test import TestCase

from incidents.ai.schemas import IncidentExtractionResult
from incidents.models import (
    ChannelMessage,
    Incident,
    IncidentDraft,
    Technician,
    TechnicianChannel,
)
from incidents.services.incident_processor import IncidentProcessor
from incidents.services.incoming_message_service import (
    IncomingMessageService,
)


class TimeoutThenSuccessAIProvider:
    def __init__(self):
        self.call_count = 0
        self.received_messages = []

    def extract_incident(
        self,
        message: str,
        previous_data: dict | None = None,
    ) -> IncidentExtractionResult:
        self.call_count += 1
        self.received_messages.append(message)

        if self.call_count == 1:
            raise TimeoutError(
                "Timeout simulado para la prueba."
            )

        return IncidentExtractionResult(
            intent="incident_followup",
            unit="OB7",
            area="Redes",
            location_context="OB7",
            equipment="Router",
            failure_type="No tiene conexión",
            description=(
                "El router de OB7 no tiene conexión."
            ),
            operational_impact=(
                "Obliga a usar un respaldo"
            ),
            priority=None,
            missing_fields=["priority"],
            clarification_question=(
                "Para completar el reporte, ¿qué prioridad "
                "le asignas: Baja, Media, Alta o Crítica?"
            ),
            raw_response={
                "provider": "fake-timeout",
            },
        )


class AITimeoutFlowTests(TestCase):
    def setUp(self):
        self.technician = Technician.objects.create(
            full_name="Técnico Timeout",
            is_active=True,
        )

        TechnicianChannel.objects.create(
            technician=self.technician,
            provider=TechnicianChannel.Provider.TELEGRAM,
            external_user_id="timeout-test-user",
            external_chat_id="timeout-test-chat",
            is_active=True,
        )

        self.ai_provider = TimeoutThenSuccessAIProvider()

        self.service = IncomingMessageService(
            incident_processor=IncidentProcessor(
                ai_provider=self.ai_provider,
            )
        )

    def test_initial_timeout_does_not_create_draft_and_allows_retry(self):
        original_text = (
            "Área Redes, el router de OB7 no tiene conexión "
            "y obliga a usar un respaldo"
        )

        first_result = self.service.process_text(
            provider="telegram",
            external_user_id="timeout-test-user",
            external_chat_id="timeout-test-chat",
            external_message_id="timeout-test-1",
            text=original_text,
            raw_payload={
                "test": True,
            },
        )

        first_processing = first_result["processing"]

        self.assertEqual(
            first_processing["action"],
            "ai_timeout",
        )
        self.assertFalse(
            first_processing["incident_created"],
        )
        self.assertIsNone(
            first_processing["draft_id"],
        )
        self.assertEqual(
            first_processing["missing_fields"],
            [],
        )
        self.assertEqual(
            Incident.objects.count(),
            0,
        )
        self.assertEqual(
            IncidentDraft.objects.count(),
            0,
        )

        first_message = ChannelMessage.objects.get(
            external_message_id="timeout-test-1",
        )

        self.assertEqual(
            first_message.status,
            ChannelMessage.Status.PROCESSED,
        )
        self.assertIsNone(
            first_message.draft,
        )

        second_result = self.service.process_text(
            provider="telegram",
            external_user_id="timeout-test-user",
            external_chat_id="timeout-test-chat",
            external_message_id="timeout-test-2",
            text=original_text,
            raw_payload={
                "test": True,
            },
        )

        second_processing = second_result["processing"]

        self.assertEqual(
            second_processing["action"],
            "ask_clarification",
        )
        self.assertFalse(
            second_processing["incident_created"],
        )
        self.assertEqual(
            second_processing["missing_fields"],
            ["priority"],
        )
        self.assertEqual(
            self.ai_provider.call_count,
            2,
        )
        self.assertEqual(
            self.ai_provider.received_messages[1],
            original_text,
        )
        self.assertEqual(
            Incident.objects.count(),
            0,
        )
        self.assertEqual(
            IncidentDraft.objects.count(),
            1,
        )

        draft = IncidentDraft.objects.get(
            pk=second_processing["draft_id"],
        )

        self.assertEqual(
            draft.status,
            IncidentDraft.Status.COLLECTING,
        )

        third_result = self.service.process_text(
            provider="telegram",
            external_user_id="timeout-test-user",
            external_chat_id="timeout-test-chat",
            external_message_id="timeout-test-3",
            text="Alta",
            raw_payload={
                "test": True,
            },
        )

        third_processing = third_result["processing"]

        self.assertEqual(
            third_processing["action"],
            "ask_clarification",
        )
        self.assertFalse(
            third_processing["incident_created"],
        )
        self.assertEqual(
            third_processing["missing_fields"],
            [],
        )
        self.assertEqual(
            third_processing["extracted_data"]["priority"],
            Incident.Priority.HIGH,
        )
        self.assertIn(
            "¿Confirmas la creación del incidente?",
            third_processing["reply"],
        )
        self.assertEqual(
            Incident.objects.count(),
            0,
        )

        draft.refresh_from_db()

        self.assertEqual(
            draft.status,
            IncidentDraft.Status.READY,
        )

        fourth_result = self.service.process_text(
            provider="telegram",
            external_user_id="timeout-test-user",
            external_chat_id="timeout-test-chat",
            external_message_id="timeout-test-4",
            text="Sí",
            raw_payload={
                "test": True,
            },
        )

        fourth_processing = fourth_result["processing"]

        self.assertEqual(
            fourth_processing["action"],
            "incident_created",
        )
        self.assertTrue(
            fourth_processing["incident_created"],
        )

        incident = Incident.objects.get()

        self.assertEqual(
            incident.priority,
            Incident.Priority.HIGH,
        )

        self.assertEqual(
            incident.area,
            "Redes",
        )
        self.assertEqual(
            incident.location_context,
            "OB7",
        )

        self.assertEqual(
            incident.operational_impact,
            "Obliga a usar un respaldo",
        )

        self.assertEqual(
            self.ai_provider.call_count,
            2,
        )

        draft.refresh_from_db()

        self.assertEqual(
            draft.status,
            IncidentDraft.Status.COMPLETED,
        )
        self.assertEqual(
            draft.incident,
            incident,
        )