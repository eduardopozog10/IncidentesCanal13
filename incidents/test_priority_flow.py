from django.test import TestCase, override_settings

from incidents.models import (
    Incident,
    IncidentDraft,
    Technician,
    TechnicianChannel,
)
from incidents.services.incoming_message_service import (
    IncomingMessageService,
)


@override_settings(AI_PROVIDER="mock")
class PriorityFlowTests(TestCase):
    def setUp(self):
        self.technician = Technician.objects.create(
            full_name="Técnico Prioridad",
            is_active=True,
        )

        TechnicianChannel.objects.create(
            technician=self.technician,
            provider=TechnicianChannel.Provider.TELEGRAM,
            external_user_id="priority-test-user",
            external_chat_id="priority-test-chat",
            is_active=True,
        )

        self.service = IncomingMessageService()

    def test_keeps_draft_open_when_priority_is_unknown(self):
        first_result = self.service.process_text(
            provider="telegram",
            external_user_id="priority-test-user",
            external_chat_id="priority-test-chat",
            external_message_id="priority-test-1",
            text=(
                "El router de la unidad móvil 7 no funciona"
            ),
            raw_payload={
                "test": True,
            },
        )

        first_processing = first_result["processing"]

        self.assertEqual(
            first_processing["action"],
            "ask_clarification",
        )
        self.assertEqual(
            first_processing["missing_fields"],
            ["priority"],
        )
        self.assertEqual(
            Incident.objects.count(),
            0,
        )

        draft_id = first_processing["draft_id"]

        second_result = self.service.process_text(
            provider="telegram",
            external_user_id="priority-test-user",
            external_chat_id="priority-test-chat",
            external_message_id="priority-test-2",
            text="No sé",
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
            Incident.objects.count(),
            0,
        )

        draft = IncidentDraft.objects.get(pk=draft_id)

        self.assertEqual(
            draft.status,
            IncidentDraft.Status.COLLECTING,
        )
        self.assertEqual(
            draft.missing_fields,
            ["priority"],
        )

        third_result = self.service.process_text(
            provider="telegram",
            external_user_id="priority-test-user",
            external_chat_id="priority-test-chat",
            external_message_id="priority-test-3",
            text="Media entonces",
            raw_payload={
                "test": True,
            },
        )

        third_processing = third_result["processing"]

        self.assertEqual(
            third_processing["action"],
            "incident_created",
        )
        self.assertTrue(
            third_processing["incident_created"],
        )

        incident = Incident.objects.get()

        self.assertEqual(
            incident.priority,
            Incident.Priority.MEDIUM,
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