
from django.test import TestCase

from incidents.ai.schemas import IncidentExtractionResult
from incidents.models import (
    Incident,
    IncidentDraft,
    Technician,
    TechnicianChannel,
)
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

    def test_natural_negative_confirmation_enters_correction_mode(self):
        draft = IncidentDraft.objects.create(
            technician=self.reporter,
            status=IncidentDraft.Status.READY,
            extracted_data={
                "unit": "Estudio de streaming",
                "area": "Audio",
                "location_context": "Estudio de streaming",
                "asset_category": "digital_streaming",
                "equipment": "Consola de audio",
                "failure_type": "Sin conexión",
                "description": (
                    "La consola de audio no tiene conexión."
                ),
                "operational_impact": (
                    "No permite realizar el streaming."
                ),
                "priority": Incident.Priority.CRITICAL,
            },
            missing_fields=[],
            last_question=(
                "¿Confirmas la creación del incidente?"
            ),
        )

        service = IncomingMessageService(
            incident_processor=IncidentProcessor(
                ai_provider=SequentialAIProvider([])
            )
        )

        result = self.send(
            service,
            "No, me equivoqué con la ubicación",
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
            "ubicación",
            processing["reply"].lower(),
        )

        self.assertIn(
            "correcto",
            processing["reply"].lower(),
        )

        self.assertEqual(
            Incident.objects.count(),
            0,
        )

        draft.refresh_from_db()

        self.assertEqual(
            draft.status,
            IncidentDraft.Status.COLLECTING,
        )

    def test_natural_location_correction_updates_location(self):
        draft = IncidentDraft.objects.create(
            technician=self.reporter,
            status=IncidentDraft.Status.READY,
            extracted_data={
                "unit": "Estudio de streaming",
                "area": "Audio",
                "location_context": "Estudio de streaming",
                "asset_category": "digital_streaming",
                "equipment": "Consola de audio",
                "failure_type": "Sin conexión",
                "description": (
                    "La consola de audio no tiene conexión."
                ),
                "operational_impact": (
                    "No permite realizar el streaming."
                ),
                "priority": Incident.Priority.CRITICAL,
            },
            missing_fields=[],
            last_question=(
                "¿Confirmas la creación del incidente?"
            ),
        )

        service = IncomingMessageService(
            incident_processor=IncidentProcessor(
                ai_provider=SequentialAIProvider([])
            )
        )

        first_result = self.send(
            service,
            "No, me equivoqué con la ubicación",
        )

        self.assertEqual(
            first_result["processing"]["action"],
            "ask_clarification",
        )

        draft.refresh_from_db()

        self.assertEqual(
            draft.status,
            IncidentDraft.Status.COLLECTING,
        )

        second_result = self.send(
            service,
            "Era el Estudio 12",
        )

        processing = second_result["processing"]

        self.assertEqual(
            processing["extracted_data"]["location_context"],
            "Estudio 12",
        )

        self.assertEqual(
            processing["extracted_data"]["unit"],
            "Estudio 12",
        )

        self.assertIn(
            "Estudio 12",
            processing["reply"],
        )

        self.assertNotIn(
            "Ubicación/contexto: Estudio de streaming",
            processing["reply"],
        )

        self.assertFalse(
            processing["incident_created"],
        )

        self.assertEqual(
            Incident.objects.count(),
            0,
        )

        draft.refresh_from_db()

        self.assertEqual(
            draft.extracted_data["location_context"],
            "Estudio 12",
        )

        self.assertEqual(
            draft.extracted_data["unit"],
            "Estudio 12",
        )

        self.assertEqual(
            draft.status,
            IncidentDraft.Status.READY,
        )
    def test_area_correction_with_location_value_guides_user(self):
        draft = IncidentDraft.objects.create(
            technician=self.reporter,
            status=IncidentDraft.Status.COLLECTING,
            extracted_data={
                "unit": "Estudio de streaming",
                "area": "Audio",
                "location_context": "Estudio de streaming",
                "asset_category": "digital_streaming",
                "equipment": "Consola de audio",
                "failure_type": "Sin conexión",
                "description": (
                    "La consola de audio no tiene conexión."
                ),
                "operational_impact": (
                    "No permite realizar el streaming."
                ),
                "priority": Incident.Priority.CRITICAL,
            },
            missing_fields=[],
            last_question=(
                "De acuerdo. Indícame qué dato quieres corregir "
                "antes de crear el incidente."
            ),
        )

        service = IncomingMessageService(
            incident_processor=IncidentProcessor(
                ai_provider=SequentialAIProvider([])
            )
        )

        result = self.send(
            service,
            (
                "Quiero corregir el área, "
                "la correcta es el Estudio 12"
            ),
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
            "ubicación",
            processing["reply"].lower(),
        )

        self.assertIn(
            "Estudio 12",
            processing["reply"],
        )

        draft.refresh_from_db()

        self.assertEqual(
            draft.extracted_data["area"],
            "Audio",
        )

        self.assertEqual(
            draft.extracted_data["location_context"],
            "Estudio de streaming",
        )

        self.assertEqual(
            Incident.objects.count(),
            0,
        )

    def test_selecting_equipment_correction_asks_for_new_value(self):
        draft = IncidentDraft.objects.create(
            technician=self.reporter,
            status=IncidentDraft.Status.READY,
            extracted_data={
                "unit": "Estadio Nacional",
                "area": "Producción",
                "location_context": "Estadio Nacional",
                "asset_category": Incident.AssetCategory.VIDEO_CAMERAS,
                "equipment": "Cámara principal",
                "failure_type": "No da imagen",
                "description": (
                    "La Cámara principal no da imagen "
                    "en el Estadio Nacional."
                ),
                "operational_impact": (
                    "Obliga a ocupar otra cámara."
                ),
                "priority": Incident.Priority.HIGH,
            },
            missing_fields=[],
            last_question=(
                "¿Confirmas la creación del incidente?"
            ),
        )

        service = IncomingMessageService(
            incident_processor=IncidentProcessor(
                ai_provider=SequentialAIProvider([])
            )
        )

        first_result = self.send(
            service,
            "No, quiero cambiar el equipo",
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
            "equipo",
            first_processing["reply"].lower(),
        )

        self.assertIn(
            "correcto",
            first_processing["reply"].lower(),
        )

        draft.refresh_from_db()

        self.assertEqual(
            draft.status,
            IncidentDraft.Status.COLLECTING,
        )

        second_result = self.send(
            service,
            "Cámara de sala prensa",
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
            second_processing[
                "extracted_data"
            ]["equipment"],
            "Cámara de sala prensa",
        )

        self.assertEqual(
            second_processing[
                "extracted_data"
            ]["asset_category"],
            Incident.AssetCategory.VIDEO_CAMERAS,
        )

        self.assertIn(
            "Cámara de sala prensa",
            second_processing[
                "extracted_data"
            ]["description"],
        )

        self.assertNotIn(
            "Cámara principal",
            second_processing[
                "extracted_data"
            ]["description"],
        )

        self.assertIn(
            "¿Confirmas la creación del incidente?",
            second_processing["reply"],
        )

        draft.refresh_from_db()

        self.assertEqual(
            draft.extracted_data["equipment"],
            "Cámara de sala prensa",
        )

        self.assertEqual(
            draft.status,
            IncidentDraft.Status.READY,
        )

    def test_natural_equipment_correction_uses_ai_context(self):
        draft = IncidentDraft.objects.create(
            technician=self.reporter,
            status=IncidentDraft.Status.READY,
            extracted_data={
                "unit": "Estadio Nacional",
                "area": "Producción",
                "location_context": "Estadio Nacional",
                "asset_category": Incident.AssetCategory.VIDEO_CAMERAS,
                "equipment": "Cámara principal",
                "failure_type": "No da imagen",
                "description": (
                    "La Cámara principal no da imagen "
                    "en el Estadio Nacional."
                ),
                "operational_impact": (
                    "Obliga a ocupar otra cámara."
                ),
                "priority": Incident.Priority.HIGH,
            },
            missing_fields=[],
            last_question=(
                "¿Confirmas la creación del incidente?"
            ),
        )

        fake_ai = SequentialAIProvider(
            [
                IncidentExtractionResult(
                    intent="incident_followup",
                    unit="Estadio Nacional",
                    area="Producción",
                    location_context="Estadio Nacional",
                    asset_category="video_cameras",
                    equipment="Cámara de sala prensa",
                    failure_type="No da imagen",
                    description=(
                        "La Cámara de sala prensa no da imagen "
                        "en el Estadio Nacional."
                    ),
                    operational_impact=(
                        "Obliga a ocupar otra cámara."
                    ),
                    priority=Incident.Priority.HIGH,
                    clarification_question=None,
                ),
            ]
        )

        service = IncomingMessageService(
            incident_processor=IncidentProcessor(
                ai_provider=fake_ai
            )
        )

        result = self.send(
            service,
            (
                "No era cámara principal, "
                "era la de sala prensa"
            ),
        )

        processing = result["processing"]

        self.assertEqual(
            processing["action"],
            "ask_clarification",
        )

        self.assertFalse(
            processing["incident_created"],
        )

        self.assertEqual(
            processing[
                "extracted_data"
            ]["equipment"],
            "Cámara de sala prensa",
        )

        self.assertEqual(
            processing[
                "extracted_data"
            ]["asset_category"],
            Incident.AssetCategory.VIDEO_CAMERAS,
        )

        self.assertIn(
            "Cámara de sala prensa",
            processing[
                "extracted_data"
            ]["description"],
        )

        self.assertNotIn(
            "Cámara principal",
            processing[
                "extracted_data"
            ]["description"],
        )

        self.assertEqual(
            processing[
                "extracted_data"
            ]["location_context"],
            "Estadio Nacional",
        )

        self.assertEqual(
            processing[
                "extracted_data"
            ]["area"],
            "Producción",
        )

        self.assertEqual(
            processing[
                "extracted_data"
            ]["priority"],
            Incident.Priority.HIGH,
        )

        self.assertIn(
            "¿Confirmas la creación del incidente?",
            processing["reply"],
        )

        draft.refresh_from_db()

        self.assertEqual(
            draft.extracted_data["equipment"],
            "Cámara de sala prensa",
        )

        self.assertEqual(
            draft.status,
            IncidentDraft.Status.READY,
        )

    def test_natural_equipment_reply_is_cleaned(self):
        IncidentDraft.objects.create(
            technician=self.reporter,
            status=IncidentDraft.Status.COLLECTING,
            extracted_data={
                "unit": "Estudio de streaming",
                "area": None,
                "location_context": "Estudio de streaming",
                "asset_category": None,
                "equipment": None,
                "failure_type": None,
                "description": None,
                "operational_impact": None,
                "priority": None,
            },
            missing_fields=[
                "area",
                "equipment",
                "failure_type",
                "description",
                "operational_impact",
                "priority",
            ],
            last_question=(
                "¿Qué equipo presenta la falla?"
            ),
        )

        service = IncomingMessageService(
            incident_processor=IncidentProcessor(
                ai_provider=SequentialAIProvider([])
            )
        )

        result = self.send(
            service,
            "Es la consola de audio que tiene problemas",
        )

        processing = result["processing"]

        self.assertEqual(
            processing["extracted_data"]["equipment"],
            "Consola de audio",
        )

        draft = IncidentDraft.objects.get(
            technician=self.reporter
        )

        self.assertEqual(
            draft.extracted_data["equipment"],
            "Consola de audio",
        )

        self.assertNotEqual(
            draft.extracted_data["equipment"],
            "Es la consola de audio que tiene problemas",
        )
    def test_audio_equipment_corrects_wrong_asset_category(self):
        IncidentDraft.objects.create(
            technician=self.reporter,
            status=IncidentDraft.Status.COLLECTING,
            extracted_data={
                "unit": "Estudio de streaming",
                "area": "Audio",
                "location_context": "Estudio de streaming",
                "asset_category": None,
                "equipment": None,
                "failure_type": None,
                "description": None,
                "operational_impact": None,
                "priority": None,
            },
            missing_fields=[
                "equipment",
                "failure_type",
                "description",
                "operational_impact",
                "priority",
            ],
            last_question=(
                "¿Qué equipo presenta la falla?"
            ),
        )

        fake_ai = SequentialAIProvider(
            [
                IncidentExtractionResult(
                    intent="incident_followup",
                    equipment="Consola de audio",
                    asset_category="digital_streaming",
                    failure_type="Sin conexión",
                    description=(
                        "La consola de audio no tiene "
                        "conexión con otros equipos."
                    ),
                    clarification_question=(
                        "¿Qué impacto operacional "
                        "está generando la falla?"
                    ),
                ),
            ]
        )

        service = IncomingMessageService(
            incident_processor=IncidentProcessor(
                ai_provider=fake_ai
            )
        )

        result = self.send(
            service,
            (
                "Es la consola de audio que tiene "
                "problemas y no se conecta."
            ),
        )

        processing = result["processing"]

        self.assertEqual(
            processing["extracted_data"]["equipment"],
            "Consola de audio",
        )

        self.assertEqual(
            processing[
                "extracted_data"
            ]["asset_category"],
            Incident.AssetCategory.AUDIO,
        )

        draft = IncidentDraft.objects.get(
            technician=self.reporter
        )

        self.assertEqual(
            draft.extracted_data["asset_category"],
            Incident.AssetCategory.AUDIO,
        )

        self.assertNotEqual(
            draft.extracted_data["asset_category"],
            Incident.AssetCategory.DIGITAL_STREAMING,
        )
    def test_generic_camera_requires_specific_identifier(self):
        fake_ai = SequentialAIProvider(
            [
                IncidentExtractionResult(
                    intent="incident_report",
                    equipment="Cámara",
                    asset_category="video_cameras",
                    clarification_question=None,
                ),
            ]
        )

        service = IncomingMessageService(
            incident_processor=IncidentProcessor(
                ai_provider=fake_ai
            )
        )

        result = self.send(
            service,
            "Tengo un problema con una cámara.",
        )

        processing = result["processing"]

        self.assertEqual(
            processing["action"],
            "ask_clarification",
        )

        self.assertIn(
            "equipment",
            processing["missing_fields"],
        )

        self.assertIsNone(
            processing["extracted_data"]["equipment"],
        )

        self.assertIn(
            "Cámara 3",
            processing["reply"],
        )

        self.assertFalse(
            processing["incident_created"],
        )
    def test_natural_location_replies_are_cleaned(self):
        cases = (
            ("Estamos en el estudio 6", "Estudio 6"),
            ("Estoy en la sala 4", "Sala 4"),
            ("La ubicación es el Control Central", "Control Central"),
            ("Acá en el Estudio 2", "Estudio 2"),
            ("Se encuentra en la unidad móvil 5", "Unidad móvil 5"),
        )

        for raw_value, expected_value in cases:
            with self.subTest(raw_value=raw_value):
                IncidentDraft.objects.all().delete()

                IncidentDraft.objects.create(
                    technician=self.reporter,
                    status=IncidentDraft.Status.COLLECTING,
                    extracted_data={
                        "unit": None,
                        "area": "Producción",
                        "location_context": None,
                        "asset_category": "video_cameras",
                        "equipment": "Cámara 3",
                        "failure_type": None,
                        "description": None,
                        "operational_impact": None,
                        "priority": None,
                    },
                    missing_fields=[
                        "location_context",
                        "failure_type",
                        "description",
                        "operational_impact",
                        "priority",
                    ],
                    last_question=(
                        "¿Dónde está ocurriendo la falla?"
                    ),
                )

                service = IncomingMessageService(
                    incident_processor=IncidentProcessor(
                        ai_provider=SequentialAIProvider([])
                    )
                )

                result = self.send(
                    service,
                    raw_value,
                )

                processing = result["processing"]

                self.assertEqual(
                    processing[
                        "extracted_data"
                    ]["location_context"],
                    expected_value,
                )

                self.assertEqual(
                    processing[
                        "extracted_data"
                    ]["unit"],
                    expected_value,
                )
    def test_operational_impact_removes_conversational_fillers(self):
        cases = (
            (
                "Ahh sí, nos obliga a usar un respaldo",
                "Obliga a usar un respaldo",
            ),
            (
                "Bueno, no nos deja continuar con la grabación",
                "No permite continuar con la grabación",
            ),
            (
                "Ehmm, claro, nos impide iniciar el streaming",
                "Impide iniciar el streaming",
            ),
            (
                "Ok, nos obliga a cambiar de cámara",
                "Obliga a cambiar de cámara",
            ),
        )

        for raw_value, expected_value in cases:
            with self.subTest(raw_value=raw_value):
                IncidentDraft.objects.all().delete()

                IncidentDraft.objects.create(
                    technician=self.reporter,
                    status=IncidentDraft.Status.COLLECTING,
                    extracted_data={
                        "unit": "Estudio 6",
                        "area": "Producción",
                        "location_context": "Estudio 6",
                        "asset_category": "video_cameras",
                        "equipment": "Cámara 3",
                        "failure_type": "No enciende",
                        "description": (
                            "La cámara no enciende."
                        ),
                        "operational_impact": None,
                        "priority": None,
                    },
                    missing_fields=[
                        "operational_impact",
                        "priority",
                    ],
                    last_question=(
                        "¿Qué impacto operacional "
                        "está generando la falla?"
                    ),
                )

                service = IncomingMessageService(
                    incident_processor=IncidentProcessor(
                        ai_provider=SequentialAIProvider([])
                    )
                )

                result = self.send(
                    service,
                    raw_value,
                )

                processing = result["processing"]

                self.assertEqual(
                    processing[
                        "extracted_data"
                    ]["operational_impact"],
                    expected_value,
                )

                draft = IncidentDraft.objects.get(
                    technician=self.reporter
                )

                self.assertEqual(
                    draft.extracted_data[
                        "operational_impact"
                    ],
                    expected_value,
                )
    def test_conversational_commas_are_not_compound_reply(self):
        processor = IncidentProcessor(
            ai_provider=SequentialAIProvider([])
        )

        self.assertFalse(
            processor._looks_like_compound_reply(
                "Ehmm, claro, nos impide iniciar el streaming"
            )
        )

        self.assertTrue(
            processor._looks_like_compound_reply(
                "Estudio 2, Cámara 3, prioridad alta"
            )
        )