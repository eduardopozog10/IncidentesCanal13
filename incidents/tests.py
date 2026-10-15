from types import SimpleNamespace
from django.test import TestCase, override_settings

from django.utils import timezone

from incidents.services.incident_processor import IncidentProcessor



from incidents.models import (

    ChannelMessage,

    Incident,

    IncidentDraft,

    Technician,

    TechnicianChannel,

)

from incidents.services.incoming_message_service import (

    IncomingMessageService,

)





@override_settings(AI_PROVIDER="mock")
class IncomingMessageServiceTests(TestCase):
    def setUp(self):
        self.technician = Technician.objects.create(
            full_name="Técnico de prueba",
            is_active=True,
        )

        self.channel = TechnicianChannel.objects.create(
            technician=self.technician,
            provider=TechnicianChannel.Provider.TELEGRAM,
            external_user_id="123456789",
            external_chat_id="123456789",
            is_active=True,
        )

        self.service = IncomingMessageService()

    def test_creates_incident_from_complete_message_with_explicit_priority(self):
        result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:1",
            text=(
                "Área Producción, la cámara de la unidad móvil 9 "
                "no enciende y detiene la producción, prioridad alta"
            ),
            raw_payload={
                "test": True,
            },
        )

        self.assertFalse(result["duplicate"])

        processing = result["processing"]

        # "Cámara" es demasiado genérico, por lo que el bot
        # debe solicitar un identificador antes de confirmar.
        self.assertEqual(
            processing["action"],
            "ask_clarification",
        )
        self.assertFalse(
            processing["incident_created"],
        )
        self.assertIn(
            "equipment",
            processing["missing_fields"],
        )
        self.assertIsNone(
            processing["extracted_data"]["equipment"],
        )
        self.assertEqual(
            Incident.objects.count(),
            0,
        )

        draft = IncidentDraft.objects.get()

        self.assertEqual(
            draft.status,
            IncidentDraft.Status.COLLECTING,
        )

        # El reportante identifica la cámara específica.
        equipment_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:1-equipment",
            text="Cámara 3",
            raw_payload={
                "test": True,
            },
        )

        equipment_processing = equipment_result[
            "processing"
        ]

        self.assertEqual(
            equipment_processing["action"],
            "ask_clarification",
        )
        self.assertFalse(
            equipment_processing["incident_created"],
        )
        self.assertEqual(
            equipment_processing["missing_fields"],
            [],
        )
        self.assertEqual(
            equipment_processing[
                "extracted_data"
            ]["equipment"],
            "Cámara 3",
        )
        self.assertIn(
            "¿Confirmas la creación del incidente?",
            equipment_processing["reply"],
        )

        draft.refresh_from_db()

        self.assertEqual(
            draft.status,
            IncidentDraft.Status.READY,
        )

        confirmation_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:1-confirm",
            text="Sí",
            raw_payload={
                "test": True,
            },
        )

        confirmation_processing = confirmation_result[
            "processing"
        ]

        self.assertEqual(
            confirmation_processing["action"],
            "incident_created",
        )
        self.assertTrue(
            confirmation_processing["incident_created"],
        )

        incident = Incident.objects.get()

        self.assertEqual(
            incident.technician,
            self.technician,
        )
        self.assertEqual(
            incident.unit,
            "Unidad movil 9",
        )
        self.assertEqual(
            incident.area,
            "Producción",
        )
        self.assertEqual(
            incident.operational_impact,
            "Detiene la producción",
        )
        self.assertEqual(
            incident.asset_category,
            Incident.AssetCategory.VIDEO_CAMERAS,
        )
        self.assertEqual(
            incident.equipment,
            "Cámara 3",
        )
        self.assertEqual(
            incident.failure_type,
            "No enciende",
        )
        self.assertEqual(
            incident.priority,
            Incident.Priority.HIGH,
        )

        message = ChannelMessage.objects.get(
            provider="telegram",
            external_message_id="123456789:1-confirm",
        )

        self.assertEqual(
            message.status,
            ChannelMessage.Status.PROCESSED,
        )
        self.assertEqual(
            message.incident,
            incident,
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

    def test_requires_explicit_priority_before_creating_incident(self):
        first_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:priority-1",
            text=(
                "Área Producción, la cámara 3 de la unidad móvil 9 "
                "no enciende y detiene la producción"
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
        self.assertFalse(
            first_processing["incident_created"],
        )
        self.assertIn(
            "priority",
            first_processing["missing_fields"],
        )
        self.assertIsNone(
            first_processing["extracted_data"]["priority"],
        )
        self.assertEqual(
            Incident.objects.count(),
            0,
        )

        second_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:priority-2",
            text="Alta",
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
            [],
        )
        self.assertEqual(
            second_processing["extracted_data"]["priority"],
            Incident.Priority.HIGH,
        )
        self.assertIn(
            "¿Confirmas la creación del incidente?",
            second_processing["reply"],
        )
        self.assertEqual(
            Incident.objects.count(),
            0,
        )

        third_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:priority-3",
            text="Sí",
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
            Incident.Priority.HIGH,
        )

    def test_duplicate_message_is_not_processed_twice(self):
        first_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:2",
            text=(
                "Área Producción, la cámara 3 de la unidad móvil 10 "
                "no enciende y detiene la producción, prioridad alta"
            ),
            raw_payload={
                "test": True,
            },
        )

        second_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:2",
            text=(
                "La cámara 3 de la unidad móvil 10 no enciende, "
                "prioridad alta"
            ),
            raw_payload={
                "test": True,
            },
        )

        self.assertFalse(
            first_result["duplicate"],
        )
        self.assertEqual(
            first_result["processing"]["action"],
            "ask_clarification",
        )

        self.assertTrue(
            second_result["duplicate"],
        )
        self.assertIsNone(
            second_result["processing"],
        )

        self.assertEqual(
            Incident.objects.count(),
            0,
        )
        self.assertEqual(
            IncidentDraft.objects.count(),
            1,
        )

        self.assertEqual(
            ChannelMessage.objects.filter(
                provider="telegram",
                external_message_id="123456789:2",
            ).count(),
            1,
        )

        confirmation_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:2-confirm",
            text="Sí",
            raw_payload={
                "test": True,
            },
        )

        self.assertEqual(
            confirmation_result["processing"]["action"],
            "incident_created",
        )
        self.assertEqual(
            Incident.objects.count(),
            1,
        )

    def test_creates_draft_when_information_is_missing(self):
        result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:3",
            text="La consola no funciona",
            raw_payload={
                "test": True,
            },
        )

        processing = result["processing"]

        self.assertEqual(
            processing["action"],
            "ask_clarification",
        )
        self.assertFalse(
            processing["incident_created"],
        )
        self.assertIn(
            "area",
            processing["missing_fields"],
        )
        self.assertIn(
            "location_context",
            processing["missing_fields"],
        )
        self.assertIn(
            "operational_impact",
            processing["missing_fields"],
        )
        self.assertIn(
            "priority",
            processing["missing_fields"],
        )

        self.assertEqual(
            Incident.objects.count(),
            0,
        )

        draft = IncidentDraft.objects.get()

        self.assertEqual(
            draft.status,
            IncidentDraft.Status.COLLECTING,
        )
        self.assertIn(
            "area",
            draft.missing_fields,
        )
        self.assertIn(
            "location_context",
            draft.missing_fields,
        )
        self.assertIn(
            "priority",
            draft.missing_fields,
        )

    def test_completes_existing_draft_after_final_confirmation(self):
        first_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:4",
            text="La consola principal no funciona",
            raw_payload={
                "test": True,
            },
        )

        draft_id = first_result["processing"]["draft_id"]

        second_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:5",
            text=(
                "Área Audio, Unidad móvil 12, "
                "obliga a usar un respaldo"
            ),
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

        third_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:6",
            text="Media",
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
        self.assertIn(
            "¿Confirmas la creación del incidente?",
            third_processing["reply"],
        )
        self.assertEqual(
            Incident.objects.count(),
            0,
        )

        draft = IncidentDraft.objects.get(
            pk=draft_id,
        )

        self.assertEqual(
            draft.status,
            IncidentDraft.Status.READY,
        )

        fourth_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:7",
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
        self.assertEqual(
            Incident.objects.count(),
            1,
        )

        incident = Incident.objects.get()

        self.assertEqual(
            incident.unit,
            "Unidad movil 12",
        )
        self.assertEqual(
            incident.location_context,
            "Unidad movil 12",
        )
        self.assertEqual(
            incident.area,
            "Audio",
        )
        self.assertEqual(
            incident.operational_impact,
            "Obliga a usar un respaldo",
        )
        self.assertEqual(
            incident.equipment,
            "Consola principal",
        )
        self.assertEqual(
            incident.asset_category,
            Incident.AssetCategory.AUDIO,
        )
        self.assertEqual(
            incident.failure_type,
            "No funciona",
        )
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
        self.assertEqual(
            draft.missing_fields,
            [],
        )

    def test_urgent_message_does_not_assign_priority_automatically(self):
        first_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:urgent-1",
            text=(
                "Área Producción, la cámara 3 de la unidad móvil 7 "
                "no enciende y obliga a usar un respaldo"
            ),
            raw_payload={
                "test": True,
            },
        )

        self.assertEqual(
            first_result["processing"]["action"],
            "ask_clarification",
        )

        second_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:urgent-2",
            text="Es muy urgente",
            raw_payload={
                "test": True,
            },
        )

        processing = second_result["processing"]

        self.assertEqual(
            processing["action"],
            "ask_clarification",
        )
        self.assertFalse(
            processing["incident_created"],
        )
        self.assertEqual(
            processing["missing_fields"],
            ["priority"],
        )
        self.assertIsNone(
            processing["extracted_data"]["priority"],
        )
        self.assertEqual(
            Incident.objects.count(),
            0,
        )

    def test_valid_operational_impact_continues_draft(self):
        first_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:direct-impact-1",
            text=(
                "Área Audio. En el Estudio 4, el micrófono 2 "
                "presenta audio cortado."
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
        self.assertIn(
            "operational_impact",
            first_processing["missing_fields"],
        )

        second_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:direct-impact-2",
            text=(
                "La grabación puede continuar usando "
                "un micrófono de respaldo."
            ),
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
            second_processing["extracted_data"][
                "operational_impact"
            ],
            (
                "La grabación puede continuar usando "
                "un micrófono de respaldo"
            ),
        )
        self.assertNotIn(
            "operational_impact",
            second_processing["missing_fields"],
        )
        self.assertIn(
            "priority",
            second_processing["missing_fields"],
        )


    def test_unrelated_reply_is_not_saved_as_operational_impact(self):
        first_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:unrelated-1",
            text=(
                "Área Audio. En el Estudio 4, un micrófono "
                "presenta ruido constante."
            ),
            raw_payload={
                "test": True,
            },
        )

        draft_id = first_result["processing"]["draft_id"]

        draft = IncidentDraft.objects.get(
            pk=draft_id,
        )

        previous_question = draft.last_question

        second_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:unrelated-2",
            text="Tengo hambre",
            raw_payload={
                "test": True,
            },
        )

        self.assertFalse(
            second_result["processing"]["incident_created"],
        )

        draft.refresh_from_db()

        self.assertNotEqual(
            draft.extracted_data.get(
                "operational_impact"
            ),
            "Tengo hambre",
        )
        self.assertIn(
            "operational_impact",
            draft.missing_fields,
        )
        self.assertEqual(
            draft.status,
            IncidentDraft.Status.COLLECTING,
        )

        if second_result["processing"]["action"] == "conversation":
            self.assertEqual(
                draft.last_question,
                previous_question,
            )


    def test_equipment_correction_updates_description(self):
        first_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:equipment-correction-1",
            text="La consola no funciona",
            raw_payload={
                "test": True,
            },
        )

        draft = IncidentDraft.objects.get(
            pk=first_result["processing"]["draft_id"],
        )

        draft.extracted_data = {
            "unit": "Estudio 3",
            "area": "Audio",
            "location_context": "Estudio 3",
            "asset_category": Incident.AssetCategory.AUDIO,
            "equipment": "Micrófono 5",
            "failure_type": "Ruido constante",
            "description": (
                "El Micrófono 5 presenta ruido constante "
                "durante la grabación en el Estudio 3."
            ),
            "operational_impact": (
                "La grabación puede continuar usando "
                "un micrófono de respaldo."
            ),
            "priority": Incident.Priority.MEDIUM,
        }
        draft.missing_fields = []
        draft.last_question = (
            "¿Confirmas la creación del incidente?"
        )
        draft.status = IncidentDraft.Status.READY
        draft.save(
            update_fields=[
                "extracted_data",
                "missing_fields",
                "last_question",
                "status",
                "updated_at",
            ]
        )

        self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:equipment-correction-2",
            text="No",
            raw_payload={
                "test": True,
            },
        )

        correction_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:equipment-correction-3",
            text="El equipo correcto es Micrófono 6.",
            raw_payload={
                "test": True,
            },
        )

        processing = correction_result["processing"]

        self.assertEqual(
            processing["action"],
            "ask_clarification",
        )
        self.assertEqual(
            processing["missing_fields"],
            [],
        )
        self.assertEqual(
            processing["extracted_data"]["equipment"],
            "Micrófono 6",
        )
        self.assertIn(
            "Micrófono 6",
            processing["extracted_data"]["description"],
        )
        self.assertNotIn(
            "Micrófono 5",
            processing["extracted_data"]["description"],
        )


    def test_natural_location_correction_updates_description(self):
        first_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:location-correction-1",
            text="La consola no funciona",
            raw_payload={
                "test": True,
            },
        )

        draft = IncidentDraft.objects.get(
            pk=first_result["processing"]["draft_id"],
        )

        draft.extracted_data = {
            "unit": "Estudio 1",
            "area": "Audio",
            "location_context": "Estudio 1",
            "asset_category": Incident.AssetCategory.AUDIO,
            "equipment": "Micrófono 6",
            "failure_type": "Ruido constante",
            "description": (
                "El Micrófono 6 presenta ruido constante "
                "durante la grabación en el Estudio 1."
            ),
            "operational_impact": (
                "La grabación puede continuar usando "
                "un micrófono de respaldo."
            ),
            "priority": Incident.Priority.MEDIUM,
        }
        draft.missing_fields = []
        draft.last_question = (
            "¿Confirmas la creación del incidente?"
        )
        draft.status = IncidentDraft.Status.READY
        draft.save(
            update_fields=[
                "extracted_data",
                "missing_fields",
                "last_question",
                "status",
                "updated_at",
            ]
        )

        self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:location-correction-2",
            text="No",
            raw_payload={
                "test": True,
            },
        )

        correction_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:location-correction-3",
            text="En realidad es en el Estudio 2.",
            raw_payload={
                "test": True,
            },
        )

        processing = correction_result["processing"]

        self.assertEqual(
            processing["action"],
            "ask_clarification",
        )
        self.assertEqual(
            processing["missing_fields"],
            [],
        )
        self.assertEqual(
            processing["extracted_data"]["location_context"],
            "Estudio 2",
        )
        self.assertEqual(
            processing["extracted_data"]["unit"],
            "Estudio 2",
        )
        self.assertIn(
            "Estudio 2",
            processing["extracted_data"]["description"],
        )
        self.assertNotIn(
            "Estudio 1",
            processing["extracted_data"]["description"],
        )

    def test_normalizes_area_variant_in_direct_reply(self):
        first_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:area-normalization-1",
            text="La consola no funciona",
            raw_payload={
                "test": True,
            },
        )

        draft = IncidentDraft.objects.get(
            pk=first_result["processing"]["draft_id"],
        )

        draft.extracted_data = {
            "unit": "Estudio 3",
            "area": None,
            "location_context": "Estudio 3",
            "asset_category": Incident.AssetCategory.AUDIO,
            "equipment": "Micrófono 2",
            "failure_type": "Audio cortado",
            "description": (
                "El Micrófono 2 presenta audio cortado "
                "en el Estudio 3."
            ),
            "operational_impact": "Permite continuar operando",
            "priority": Incident.Priority.MEDIUM,
        }
        draft.missing_fields = ["area"]
        draft.last_question = (
            "¿A qué área corresponde el incidente?"
        )
        draft.status = IncidentDraft.Status.COLLECTING
        draft.save(
            update_fields=[
                "extracted_data",
                "missing_fields",
                "last_question",
                "status",
                "updated_at",
            ]
        )

        result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:area-normalization-2",
            text="Áudio",
            raw_payload={
                "test": True,
            },
        )

        processing = result["processing"]

        self.assertEqual(
            processing["extracted_data"]["area"],
            "Audio",
        )
        self.assertEqual(
            processing["missing_fields"],
            [],
        )
        self.assertIn(
            "¿Confirmas la creación del incidente?",
            processing["reply"],
        )

    def test_no_se_conecta_is_accepted_as_failure_type(self):
        first_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:connection-failure-1",
            text="La consola no funciona",
            raw_payload={
                "test": True,
            },
        )

        draft = IncidentDraft.objects.get(
            pk=first_result["processing"]["draft_id"],
        )

        draft.extracted_data = {
            "unit": "Estudio 2",
            "area": "Audio",
            "location_context": "Estudio 2",
            "asset_category": Incident.AssetCategory.AUDIO,
            "equipment": "Intercom 2",
            "failure_type": None,
            "description": (
                "El Intercom 2 presenta problemas "
                "de conexión con la consola."
            ),
            "operational_impact": "Interrumpe la grabación",
            "priority": Incident.Priority.HIGH,
        }
        draft.missing_fields = ["failure_type"]
        draft.last_question = (
            "¿Qué problema presenta exactamente el equipo?"
        )
        draft.status = IncidentDraft.Status.COLLECTING
        draft.save(
            update_fields=[
                "extracted_data",
                "missing_fields",
                "last_question",
                "status",
                "updated_at",
            ]
        )

        result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:connection-failure-2",
            text="No se conecta a la consola",
            raw_payload={
                "test": True,
            },
        )

        processing = result["processing"]

        self.assertEqual(
            processing["extracted_data"]["failure_type"],
            "No se conecta a la consola",
        )
        self.assertNotIn(
            "failure_type",
            processing["missing_fields"],
        )
        self.assertIn(
            "¿Confirmas la creación del incidente?",
            processing["reply"],
        )

    def test_failure_detail_during_impact_question_updates_description(self):
        first_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:impact-symptom-1",
            text="La consola no funciona",
            raw_payload={
                "test": True,
            },
        )

        draft = IncidentDraft.objects.get(
            pk=first_result["processing"]["draft_id"],
        )

        draft.extracted_data = {
            "unit": "Estudio 2",
            "area": "Audio",
            "location_context": "Estudio 2",
            "asset_category": Incident.AssetCategory.AUDIO,
            "equipment": "Intercom 2",
            "failure_type": "No se conecta a la consola",
            "description": (
                "El Intercom 2 no se conecta a la consola."
            ),
            "operational_impact": None,
            "priority": Incident.Priority.HIGH,
        }
        draft.missing_fields = ["operational_impact"]
        draft.last_question = (
            "¿Qué impacto operacional está generando la falla?"
        )
        draft.status = IncidentDraft.Status.COLLECTING
        draft.save(
            update_fields=[
                "extracted_data",
                "missing_fields",
                "last_question",
                "status",
                "updated_at",
            ]
        )

        result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:impact-symptom-2",
            text="No se puede escuchar el retorno",
            raw_payload={
                "test": True,
            },
        )

        processing = result["processing"]

        self.assertIsNone(
            processing["extracted_data"]["operational_impact"],
        )
        self.assertIn(
            "No se puede escuchar el retorno",
            processing["extracted_data"]["description"],
        )
        self.assertIn(
            "operational_impact",
            processing["missing_fields"],
        )
        self.assertIn(
            "Agregué ese síntoma al reporte",
            processing["reply"],
        )
        self.assertIn(
            "impacto operacional",
            processing["reply"],
        )

    def test_cleans_affirmation_from_operational_impact(self):
        first_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:clean-impact-1",
            text="La consola no funciona",
            raw_payload={
                "test": True,
            },
        )

        draft = IncidentDraft.objects.get(
            pk=first_result["processing"]["draft_id"],
        )

        draft.extracted_data = {
            "unit": "Estudio 2",
            "area": "Audio",
            "location_context": "Estudio 2",
            "asset_category": Incident.AssetCategory.AUDIO,
            "equipment": "Intercom 2",
            "failure_type": "Sin retorno",
            "description": (
                "El Intercom 2 del Estudio 2 está sin retorno."
            ),
            "operational_impact": None,
            "priority": Incident.Priority.HIGH,
        }
        draft.missing_fields = ["operational_impact"]
        draft.last_question = (
            "¿Qué impacto operacional está generando la falla?"
        )
        draft.status = IncidentDraft.Status.COLLECTING
        draft.save(
            update_fields=[
                "extracted_data",
                "missing_fields",
                "last_question",
                "status",
                "updated_at",
            ]
        )

        result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:clean-impact-2",
            text="Sí, interrumpe la grabación",
            raw_payload={
                "test": True,
            },
        )

        processing = result["processing"]

        self.assertEqual(
            processing["extracted_data"]["operational_impact"],
            "Interrumpe la grabación",
        )
        self.assertEqual(
            processing["missing_fields"],
            [],
        )
        self.assertNotIn(
            "Sí,",
            processing["extracted_data"]["operational_impact"],
        )
        self.assertIn(
            "¿Confirmas la creación del incidente?",
            processing["reply"],
        )

    def test_ambiguous_equipment_reply_does_not_invent_equipment(self):
        first_result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:ambiguous-equipment-1",
            text="La consola no funciona",
            raw_payload={
                "test": True,
            },
        )

        draft = IncidentDraft.objects.get(
            pk=first_result["processing"]["draft_id"],
        )

        draft.extracted_data = {
            "unit": "Estudio 2",
            "area": "Audio",
            "location_context": "Estudio 2",
            "asset_category": Incident.AssetCategory.AUDIO,
            "equipment": None,
            "failure_type": "No se conecta a la consola",
            "description": (
                "Un equipo de audio del Estudio 2 "
                "no se conecta a la consola."
            ),
            "operational_impact": "Interrumpe la grabación",
            "priority": Incident.Priority.HIGH,
        }
        draft.missing_fields = ["equipment"]
        draft.last_question = "¿Qué equipo presenta la falla?"
        draft.status = IncidentDraft.Status.COLLECTING
        draft.save(
            update_fields=[
                "extracted_data",
                "missing_fields",
                "last_question",
                "status",
                "updated_at",
            ]
        )

        result = self.service.process_text(
            provider="telegram",
            external_user_id="123456789",
            external_chat_id="123456789",
            external_message_id="123456789:ambiguous-equipment-2",
            text=(
                "Es el equipo de audio, "
                "no se pueden conectar a la consola"
            ),
            raw_payload={
                "test": True,
            },
        )

        processing = result["processing"]

        self.assertEqual(
            processing["action"],
            "ask_clarification",
        )
        self.assertFalse(
            processing["incident_created"],
        )

        self.assertIsNone(
            processing["extracted_data"]["equipment"],
        )

        self.assertIn(
            "equipment",
            processing["missing_fields"],
        )

        self.assertIn(
            "necesito identificar",
            processing["reply"],
        )

        self.assertIn(
            "equipo o servicio específico",
            processing["reply"],
        )

        self.assertNotIn(
            "¿Confirmas la creación del incidente?",
            processing["reply"],
        )

    def test_timeout_preserves_question_and_continues_pending_message(self):
        class TimeoutThenSuccessProvider:
            def __init__(self):
                self.calls = 0
                self.messages = []

            def extract_incident(
                self,
                message,
                previous_data=None,
            ):
                self.calls += 1
                self.messages.append(message)

                if self.calls == 1:
                    raise TimeoutError(
                        "Timeout simulado para la prueba"
                    )

                previous_data = previous_data or {}

                return SimpleNamespace(
                    intent="incident_followup",
                    conversation_reply=None,
                    unit=previous_data.get("unit"),
                    area=previous_data.get("area"),
                    location_context=previous_data.get(
                        "location_context"
                    ),
                    asset_category=previous_data.get(
                        "asset_category"
                    ),
                    equipment=previous_data.get("equipment"),
                    failure_type="Sin retorno",
                    description=(
                        "El Intercom 2 del Estudio 2 "
                        "quedó sin retorno."
                    ),
                    operational_impact=previous_data.get(
                        "operational_impact"
                    ),
                    priority=previous_data.get("priority"),
                    clarification_question=None,
                )

        provider = TimeoutThenSuccessProvider()

        processor = IncidentProcessor(
            ai_provider=provider,
        )

        original_question = (
            "¿Qué problema presenta exactamente el equipo?"
        )

        draft = IncidentDraft.objects.create(
            technician=self.technician,
            extracted_data={
                "unit": "Estudio 2",
                "area": "Audio",
                "location_context": "Estudio 2",
                "asset_category": Incident.AssetCategory.AUDIO,
                "equipment": "Intercom 2",
                "failure_type": None,
                "description": (
                    "El Intercom 2 del Estudio 2 "
                    "presenta una falla."
                ),
                "operational_impact": (
                    "Interrumpe la grabación"
                ),
                "priority": Incident.Priority.HIGH,
            },
            missing_fields=["failure_type"],
            last_question=original_question,
            status=IncidentDraft.Status.COLLECTING,
        )

        timeout_message = ChannelMessage.objects.create(
            provider=ChannelMessage.Provider.TELEGRAM,
            external_message_id="timeout-preserve-1",
            external_sender_id="123456789",
            external_chat_id="123456789",
            channel_account=self.channel,
            technician=self.technician,
            direction=ChannelMessage.Direction.INBOUND,
            message_type=ChannelMessage.MessageType.TEXT,
            content="El retorno desaparece por completo",
            raw_payload={
                "test": True,
            },
        )

        timeout_result = processor.process(
            timeout_message
        )

        self.assertEqual(
            timeout_result["action"],
            "ai_timeout",
        )

        self.assertFalse(
            timeout_result["incident_created"],
        )

        draft.refresh_from_db()

        self.assertEqual(
            draft.last_question,
            original_question,
        )

        self.assertEqual(
            draft.status,
            IncidentDraft.Status.COLLECTING,
        )

        self.assertIn(
            "failure_type",
            draft.missing_fields,
        )

        self.assertEqual(
            draft.extracted_data[
                IncidentProcessor.PENDING_MESSAGES_KEY
            ],
            [
                "El retorno desaparece por completo",
            ],
        )

        continue_message = ChannelMessage.objects.create(
            provider=ChannelMessage.Provider.TELEGRAM,
            external_message_id="timeout-preserve-2",
            external_sender_id="123456789",
            external_chat_id="123456789",
            channel_account=self.channel,
            technician=self.technician,
            direction=ChannelMessage.Direction.INBOUND,
            message_type=ChannelMessage.MessageType.TEXT,
            content="continuar",
            raw_payload={
                "test": True,
            },
        )

        continue_result = processor.process(
            continue_message
        )

        self.assertEqual(
            provider.calls,
            2,
        )

        self.assertEqual(
            provider.messages[1],
            "El retorno desaparece por completo",
        )

        self.assertEqual(
            continue_result["action"],
            "ask_clarification",
        )

        self.assertFalse(
            continue_result["incident_created"],
        )

        self.assertEqual(
            continue_result["extracted_data"]["failure_type"],
            "Sin retorno",
        )

        self.assertEqual(
            continue_result["missing_fields"],
            [],
        )

        self.assertIn(
            "¿Confirmas la creación del incidente?",
            continue_result["reply"],
        )

        draft.refresh_from_db()

        self.assertEqual(
            draft.extracted_data["failure_type"],
            "Sin retorno",
        )

        self.assertNotIn(
            IncidentProcessor.PENDING_MESSAGES_KEY,
            draft.extracted_data,
        )

    def test_natural_failure_correction_is_processed_without_ai(self):
        class FailIfCalledProvider:
            def extract_incident(
                self,
                message,
                previous_data=None,
            ):
                raise AssertionError(
                    "La IA no debe llamarse para esta corrección."
                )

        processor = IncidentProcessor(
            ai_provider=FailIfCalledProvider(),
        )

        draft = IncidentDraft.objects.create(
            technician=self.technician,
            extracted_data={
                "unit": "Estudio 4",
                "area": "Audio",
                "location_context": "Estudio 4",
                "asset_category": Incident.AssetCategory.AUDIO,
                "equipment": "Intercom 2",
                "failure_type": "Falla de conexión",
                "description": (
                    "El Intercom 2 no se conecta a la consola. "
                    "No se puede escuchar el retorno."
                ),
                "operational_impact": (
                    "Interrumpe la grabación"
                ),
                "priority": Incident.Priority.LOW,
            },
            missing_fields=[],
            last_question=(
                "De acuerdo. Indícame qué dato quieres corregir "
                "antes de crear el incidente."
            ),
            status=IncidentDraft.Status.COLLECTING,
        )

        message = ChannelMessage.objects.create(
            provider=ChannelMessage.Provider.TELEGRAM,
            external_message_id="natural-failure-correction-1",
            external_sender_id="123456789",
            external_chat_id="123456789",
            channel_account=self.channel,
            technician=self.technician,
            direction=ChannelMessage.Direction.INBOUND,
            message_type=ChannelMessage.MessageType.TEXT,
            content=(
                "era que no se conecta a la consola "
                "me equivoqué"
            ),
            raw_payload={
                "test": True,
            },
        )

        result = processor.process(
            message
        )

        self.assertEqual(
            result["action"],
            "ask_clarification",
        )

        self.assertFalse(
            result["incident_created"],
        )

        self.assertEqual(
            result["extracted_data"]["failure_type"],
            "No se conecta a la consola",
        )

        self.assertEqual(
            result["missing_fields"],
            [],
        )

        self.assertEqual(
            result["extracted_data"]["equipment"],
            "Intercom 2",
        )

        self.assertEqual(
            result["extracted_data"]["operational_impact"],
            "Interrumpe la grabación",
        )

        self.assertIn(
            "¿Confirmas la creación del incidente?",
            result["reply"],
        )

        draft.refresh_from_db()

        self.assertEqual(
            draft.extracted_data["failure_type"],
            "No se conecta a la consola",
        )

        self.assertEqual(
            draft.status,
            IncidentDraft.Status.READY,
        )

    def test_failure_correction_updates_description_without_duplication(self):
        class FailIfCalledProvider:
            def extract_incident(
                self,
                message,
                previous_data=None,
            ):
                raise AssertionError(
                    "La IA no debe llamarse para esta corrección."
                )

        processor = IncidentProcessor(
            ai_provider=FailIfCalledProvider(),
        )

        draft = IncidentDraft.objects.create(
            technician=self.technician,
            extracted_data={
                "unit": "Estudio 4",
                "area": "Audio",
                "location_context": "Estudio 4",
                "asset_category": Incident.AssetCategory.AUDIO,
                "equipment": "Intercom 2",
                "failure_type": "Falla de conexión",
                "description": (
                    "El Intercom 2 presenta falla de conexión "
                    "a la consola y no se puede escuchar el retorno."
                ),
                "operational_impact": (
                    "Interrumpe la grabación"
                ),
                "priority": Incident.Priority.LOW,
            },
            missing_fields=[],
            last_question=(
                "De acuerdo. Indícame qué dato quieres corregir "
                "antes de crear el incidente."
            ),
            status=IncidentDraft.Status.COLLECTING,
        )

        message = ChannelMessage.objects.create(
            provider=ChannelMessage.Provider.TELEGRAM,
            external_message_id="failure-description-correction-1",
            external_sender_id="123456789",
            external_chat_id="123456789",
            channel_account=self.channel,
            technician=self.technician,
            direction=ChannelMessage.Direction.INBOUND,
            message_type=ChannelMessage.MessageType.TEXT,
            content=(
                "era que no se conecta a la consola "
                "me equivoqué"
            ),
            raw_payload={
                "test": True,
            },
        )

        result = processor.process(
            message
        )

        expected_description = (
            "El Intercom 2 no se conecta a la consola. "
            "No se puede escuchar el retorno."
        )

        self.assertEqual(
            result["extracted_data"]["failure_type"],
            "No se conecta a la consola",
        )

        self.assertEqual(
            result["extracted_data"]["description"],
            expected_description,
        )

        self.assertNotIn(
            "a la consola a la consola",
            result["extracted_data"]["description"],
        )

        self.assertNotIn(
            "presenta No se conecta",
            result["extracted_data"]["description"],
        )

        self.assertEqual(
            result["missing_fields"],
            [],
        )

        self.assertIn(
            "¿Confirmas la creación del incidente?",
            result["reply"],
        )

        draft.refresh_from_db()

        self.assertEqual(
            draft.extracted_data["failure_type"],
            "No se conecta a la consola",
        )

        self.assertEqual(
            draft.extracted_data["description"],
            expected_description,
        )

        self.assertEqual(
            draft.status,
            IncidentDraft.Status.READY,
        )

class IncidentListViewTests(TestCase):

    def setUp(self):

        self.technician = Technician.objects.create(

            full_name="Técnico Dashboard",

            is_active=True,

        )



        self.high_incident = Incident.objects.create(

            technician=self.technician,

            unit="Unidad móvil 20",

            equipment="Cámara principal",

            failure_type="No enciende",

            description=(

                "La cámara principal de la unidad móvil 20 "

                "no enciende."

            ),

            priority=Incident.Priority.HIGH,

            status=Incident.Status.REPORTED,

            original_message=(

                "La cámara principal de la unidad móvil 20 "

                "no enciende."

            ),

        )



        self.low_incident = Incident.objects.create(

            technician=self.technician,

            unit="Unidad móvil 40",

            equipment="Cámara auxiliar",

            failure_type="Imagen intermitente",

            description=(

                "La cámara auxiliar de la unidad móvil 40 "

                "presenta una falla intermitente."

            ),

            priority=Incident.Priority.LOW,

            status=Incident.Status.REPORTED,

            original_message=(

                "La cámara auxiliar de la unidad móvil 40 "

                "presenta una falla intermitente."

            ),

        )



    def test_lists_incidents(self):

        response = self.client.get(

            "/api/v1/incidents/",

        )



        self.assertEqual(

            response.status_code,

            200,

        )



        data = response.json()



        self.assertEqual(

            data["count"],

            2,

        )

        self.assertIsNone(

            data["next"],

        )

        self.assertIsNone(

            data["previous"],

        )



        results = data["results"]



        self.assertEqual(

            len(results),

            2,

        )



        ids = {

            item["id"]

            for item in results

        }



        self.assertIn(

            self.high_incident.pk,

            ids,

        )

        self.assertIn(

            self.low_incident.pk,

            ids,

        )



    def test_filters_by_priority(self):

        response = self.client.get(

            "/api/v1/incidents/?priority=high",

        )



        self.assertEqual(

            response.status_code,

            200,

        )



        data = response.json()



        self.assertEqual(

            data["count"],

            1,

        )



        results = data["results"]



        self.assertEqual(

            len(results),

            1,

        )

        self.assertEqual(

            results[0]["id"],

            self.high_incident.pk,

        )



    def test_filters_by_status(self):

        response = self.client.get(

            "/api/v1/incidents/?status=reported",

        )



        self.assertEqual(

            response.status_code,

            200,

        )



        data = response.json()



        self.assertEqual(

            data["count"],

            2,

        )



        results = data["results"]



        self.assertEqual(

            len(results),

            2,

        )



    def test_filters_by_unit(self):

        response = self.client.get(

            "/api/v1/incidents/?unit=40",

        )



        self.assertEqual(

            response.status_code,

            200,

        )



        data = response.json()



        self.assertEqual(

            data["count"],

            1,

        )



        results = data["results"]



        self.assertEqual(

            len(results),

            1,

        )

        self.assertEqual(

            results[0]["id"],

            self.low_incident.pk,

        )



    def test_filters_by_equipment(self):

        response = self.client.get(

            "/api/v1/incidents/?equipment=auxiliar",

        )



        self.assertEqual(

            response.status_code,

            200,

        )



        data = response.json()



        self.assertEqual(

            data["count"],

            1,

        )



        results = data["results"]



        self.assertEqual(

            len(results),

            1,

        )

        self.assertEqual(

            results[0]["id"],

            self.low_incident.pk,

        )



    def test_combines_status_and_priority_filters(self):

        response = self.client.get(

            "/api/v1/incidents/"

            "?status=reported&priority=low",

        )



        self.assertEqual(

            response.status_code,

            200,

        )



        data = response.json()



        self.assertEqual(

            data["count"],

            1,

        )



        results = data["results"]



        self.assertEqual(

            len(results),

            1,

        )

        self.assertEqual(

            results[0]["id"],

            self.low_incident.pk,

        )



    def test_searches_incidents_by_text(self):

        response = self.client.get(

            "/api/v1/incidents/?search=intermitente",

        )



        self.assertEqual(

            response.status_code,

            200,

        )



        data = response.json()



        self.assertEqual(

            data["count"],

            1,

        )



        results = data["results"]



        self.assertEqual(

            len(results),

            1,

        )

        self.assertEqual(

            results[0]["id"],

            self.low_incident.pk,

        )

        self.assertEqual(

            results[0]["failure_type"],

            "Imagen intermitente",

        )



    def test_orders_incidents_by_created_at_ascending(self):

        response = self.client.get(

            "/api/v1/incidents/?ordering=created_at",

        )



        self.assertEqual(

            response.status_code,

            200,

        )



        data = response.json()

        results = data["results"]



        self.assertEqual(

            results[0]["id"],

            self.high_incident.pk,

        )

        self.assertEqual(

            results[1]["id"],

            self.low_incident.pk,

        )



    def test_orders_incidents_by_created_at_descending(self):

        response = self.client.get(

            "/api/v1/incidents/?ordering=-created_at",

        )



        self.assertEqual(

            response.status_code,

            200,

        )



        data = response.json()

        results = data["results"]



        self.assertEqual(

            results[0]["id"],

            self.low_incident.pk,

        )

        self.assertEqual(

            results[1]["id"],

            self.high_incident.pk,

        )





class IncidentDetailViewTests(TestCase):

    def setUp(self):

        self.technician = Technician.objects.create(

            full_name="Técnico Detalle",

            is_active=True,

        )



        self.incident = Incident.objects.create(

            technician=self.technician,

            unit="Unidad móvil 50",

            equipment="Router",

            failure_type="Sin conexión",

            description=(

                "El router de la unidad móvil 50 "

                "no tiene conexión."

            ),

            priority=Incident.Priority.HIGH,

            status=Incident.Status.REPORTED,

            original_message=(

                "El router de la unidad móvil 50 "

                "no tiene conexión."

            ),

        )

    def test_incident_code_uses_secuoya_prefix(self):
        self.assertEqual(
            self.incident.code,
            f"SC-{self.incident.pk:06d}",
        )

        self.assertTrue(
            self.incident.code.startswith("SC-"),
        )

        self.assertFalse(
            self.incident.code.startswith("C13-"),
        )

        pending_incident = Incident()

        self.assertEqual(
            pending_incident.code,
            "SC-PENDIENTE",
        )

    def test_retrieves_incident_detail(self):

        response = self.client.get(

            f"/api/v1/incidents/{self.incident.pk}/",

        )



        self.assertEqual(

            response.status_code,

            200,

        )



        data = response.json()



        self.assertEqual(

            data["id"],

            self.incident.pk,

        )

        self.assertEqual(

            data["code"],

            self.incident.code,

        )

        self.assertEqual(

            data["technician_name"],

            "Técnico Detalle",

        )

        self.assertEqual(

            data["unit"],

            "Unidad móvil 50",

        )

        self.assertEqual(

            data["equipment"],

            "Router",

        )

        self.assertEqual(

            data["failure_type"],

            "Sin conexión",

        )



    def test_returns_404_for_missing_incident(self):

        response = self.client.get(

            "/api/v1/incidents/999999/",

        )



        self.assertEqual(

            response.status_code,

            404,

        )



    def test_updates_incident_status(self):

        response = self.client.patch(

            f"/api/v1/incidents/{self.incident.pk}/",

            data={

                "status": Incident.Status.IN_PROGRESS,

            },

            content_type="application/json",

        )



        self.assertEqual(

            response.status_code,

            200,

        )



        self.incident.refresh_from_db()



        self.assertEqual(

            self.incident.status,

            Incident.Status.IN_PROGRESS,

        )

        self.assertIsNone(

            self.incident.resolved_at,

        )



        data = response.json()



        self.assertEqual(

            data["status"],

            Incident.Status.IN_PROGRESS,

        )

        self.assertEqual(
            data["status_display"],
            "En atención",
        )



    def test_sets_resolved_at_when_incident_is_resolved(self):

        response = self.client.patch(

            f"/api/v1/incidents/{self.incident.pk}/",

            data={

                "status": Incident.Status.RESOLVED,

            },

            content_type="application/json",

        )



        self.assertEqual(

            response.status_code,

            200,

        )



        self.incident.refresh_from_db()



        self.assertEqual(

            self.incident.status,

            Incident.Status.RESOLVED,

        )

        self.assertIsNotNone(

            self.incident.resolved_at,

        )



        data = response.json()



        self.assertEqual(

            data["status"],

            Incident.Status.RESOLVED,

        )

        self.assertIsNotNone(

            data["resolved_at"],

        )



    def test_clears_resolved_at_when_leaving_resolved_status(self):

        self.incident.status = Incident.Status.RESOLVED

        self.incident.resolved_at = timezone.now()

        self.incident.save(

            update_fields=[

                "status",

                "resolved_at",

                "updated_at",

            ]

        )



        response = self.client.patch(

            f"/api/v1/incidents/{self.incident.pk}/",

            data={

                "status": Incident.Status.UNDER_REVIEW,

            },

            content_type="application/json",

        )



        self.assertEqual(

            response.status_code,

            200,

        )



        self.incident.refresh_from_db()



        self.assertEqual(

            self.incident.status,

            Incident.Status.UNDER_REVIEW,

        )

        self.assertIsNone(

            self.incident.resolved_at,

        )



    def test_rejects_invalid_incident_status(self):

        response = self.client.patch(

            f"/api/v1/incidents/{self.incident.pk}/",

            data={

                "status": "estado_inventado",

            },

            content_type="application/json",

        )



        self.assertEqual(

            response.status_code,

            400,

        )



        self.incident.refresh_from_db()



        self.assertEqual(

            self.incident.status,

            Incident.Status.REPORTED,

        )

        self.assertIsNone(

            self.incident.resolved_at,

        )





class IncidentSummaryViewTests(TestCase):

    def setUp(self):

        self.technician = Technician.objects.create(

            full_name="Técnico Resumen",

            is_active=True,

        )



        Incident.objects.create(

            technician=self.technician,

            unit="Unidad móvil 1",

            equipment="Cámara",

            failure_type="No enciende",

            description="La cámara no enciende.",

            priority=Incident.Priority.HIGH,

            status=Incident.Status.REPORTED,

            original_message="La cámara no enciende.",

        )



        Incident.objects.create(

            technician=self.technician,

            unit="Unidad móvil 2",

            equipment="Router",

            failure_type="Sin conexión",

            description="El router no tiene conexión.",

            priority=Incident.Priority.LOW,

            status=Incident.Status.UNDER_REVIEW,

            original_message="El router no tiene conexión.",

        )



        Incident.objects.create(

            technician=self.technician,

            unit="Unidad móvil 3",

            equipment="Consola",

            failure_type="Sin audio",

            description="La consola no entrega audio.",

            priority=Incident.Priority.CRITICAL,

            status=Incident.Status.RESOLVED,

            original_message="La consola no entrega audio.",

            resolved_at=timezone.now(),

        )



        Incident.objects.create(

            technician=self.technician,

            unit="Unidad móvil 4",

            equipment="Transmisor",

            failure_type="Falla intermitente",

            description="El transmisor presenta una falla.",

            priority=Incident.Priority.MEDIUM,

            status=Incident.Status.CLOSED,

            original_message="El transmisor presenta una falla.",

        )



    def test_returns_incident_summary(self):

        response = self.client.get(

            "/api/v1/incidents/summary/",

        )



        self.assertEqual(

            response.status_code,

            200,

        )



        data = response.json()



        self.assertEqual(

            data["total"],

            4,

        )



        self.assertEqual(

            data["by_status"]["reported"],

            1,

        )

        self.assertEqual(

            data["by_status"]["under_review"],

            1,

        )

        self.assertEqual(

            data["by_status"]["in_progress"],

            0,

        )

        self.assertEqual(

            data["by_status"]["resolved"],

            1,

        )

        self.assertEqual(

            data["by_status"]["closed"],

            1,

        )

        self.assertEqual(

            data["by_status"]["cancelled"],

            0,

        )



        self.assertEqual(

            data["by_priority"]["low"],

            1,

        )

        self.assertEqual(

            data["by_priority"]["medium"],

            1,

        )

        self.assertEqual(

            data["by_priority"]["high"],

            1,

        )

        self.assertEqual(

            data["by_priority"]["critical"],

            1,

        )

class IncidentAnalyticsViewTests(TestCase):
    def setUp(self):
        self.technician = Technician.objects.create(
            full_name="Reportante Analytics",
            is_active=True,
        )

        Incident.objects.create(
            technician=self.technician,
            unit="Control Central",
            area="Redes",
            location_context="Control Central",
            asset_category=Incident.AssetCategory.NETWORKS,
            equipment="Router principal",
            failure_type="Sin conexión",
            description="El router principal quedó sin conexión.",
            operational_impact="Obliga a usar un respaldo",
            priority=Incident.Priority.HIGH,
            status=Incident.Status.REPORTED,
            original_message=(
                "Área Redes, el router principal de Control Central "
                "quedó sin conexión y obliga a usar un respaldo."
            ),
        )

        Incident.objects.create(
            technician=self.technician,
            unit="Control Central",
            area="Redes",
            location_context="Control Central",
            asset_category=Incident.AssetCategory.NETWORKS,
            equipment="Switch principal",
            failure_type="Pérdida intermitente de conexión",
            description=(
                "El switch principal presenta pérdida intermitente "
                "de conexión."
            ),
            operational_impact="Permite continuar operando",
            priority=Incident.Priority.MEDIUM,
            status=Incident.Status.UNDER_REVIEW,
            original_message=(
                "Área Redes, el switch principal de Control Central "
                "pierde conexión de forma intermitente."
            ),
        )

        Incident.objects.create(
            technician=self.technician,
            unit="Estudio 2",
            area="Producción",
            location_context="Estudio 2",
            asset_category=Incident.AssetCategory.VIDEO_CAMERAS,
            equipment="Cámara 3",
            failure_type="Sin imagen",
            description="La Cámara 3 del Estudio 2 quedó sin imagen.",
            operational_impact="Detiene la producción",
            priority=Incident.Priority.CRITICAL,
            status=Incident.Status.REPORTED,
            original_message=(
                "Área Producción, la Cámara 3 del Estudio 2 quedó "
                "sin imagen y detiene la producción."
            ),
        )

    def test_returns_secuoya_incident_analytics(self):
        response = self.client.get(
            "/api/v1/incidents/analytics/",
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        data = response.json()

        self.assertEqual(
            data["metrics"]["total"],
            3,
        )

        self.assertEqual(
            data["by_area"],
            [
                {
                    "area": "Redes",
                    "count": 2,
                },
                {
                    "area": "Producción",
                    "count": 1,
                },
            ],
        )

        self.assertEqual(
            data["by_location_context"],
            [
                {
                    "location_context": "Control Central",
                    "count": 2,
                },
                {
                    "location_context": "Estudio 2",
                    "count": 1,
                },
            ],
        )

        asset_category_labels = dict(
            Incident.AssetCategory.choices
        )

        self.assertEqual(
            data["by_asset_category"],
            [
                {
                    "value": Incident.AssetCategory.NETWORKS,
                    "label": asset_category_labels[
                        Incident.AssetCategory.NETWORKS
                    ],
                    "count": 2,
                },
                {
                    "value": Incident.AssetCategory.VIDEO_CAMERAS,
                    "label": asset_category_labels[
                        Incident.AssetCategory.VIDEO_CAMERAS
                    ],
                    "count": 1,
                },
            ],
        )

    def test_groups_area_variants_ignoring_case_and_accents(self):
        area_variants = (
            "audio",
            "Audio",
            "AUDIO",
            "Áudio",
        )

        for index, area in enumerate(
            area_variants,
            start=1,
        ):
            Incident.objects.create(
                technician=self.technician,
                unit=f"Estudio Audio {index}",
                area=area,
                location_context=f"Estudio Audio {index}",
                asset_category=Incident.AssetCategory.AUDIO,
                equipment=f"Micrófono {index}",
                failure_type="Sin audio",
                description=(
                    f"El Micrófono {index} quedó sin audio."
                ),
                operational_impact=(
                    "Permite continuar usando un respaldo"
                ),
                priority=Incident.Priority.MEDIUM,
                status=Incident.Status.REPORTED,
                original_message=(
                    f"Incidente de prueba en área {area}."
                ),
            )

        response = self.client.get(
            "/api/v1/incidents/analytics/",
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        data = response.json()

        self.assertEqual(
            data["metrics"]["total"],
            7,
        )

        self.assertEqual(
            data["by_area"],
            [
                {
                    "area": "Audio",
                    "count": 4,
                },
                {
                    "area": "Redes",
                    "count": 2,
                },
                {
                    "area": "Producción",
                    "count": 1,
                },
            ],
        )