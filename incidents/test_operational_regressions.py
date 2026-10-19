"""Regresiones reproducibles de la ronda Secuoya del 19/10/2026 (Nivel C)."""
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import TestCase, TransactionTestCase, override_settings
from django.db import close_old_connections, IntegrityError
from concurrent.futures import ThreadPoolExecutor

from incidents.ai.gemini_provider import GeminiAIProvider
from incidents.ai.schemas import IncidentExtractionResult
from incidents.models import ChannelMessage, Incident, IncidentDraft, Technician, TechnicianChannel
from incidents.services.incident_processor import IncidentProcessor
from incidents.services.incoming_message_service import IncomingMessageService
from incidents.messaging.telegram_update_handler import TelegramUpdateHandler


DATA = dict(unit="Móvil 2", area="Producción", location_context="Móvil 2", asset_category="video_cameras", equipment="Cámara 3", failure_type="Imagen intermitente", description="La Cámara 3 del Móvil 2 pierde imagen.", operational_impact="No se puede usar en el programa", priority="critical")


class OperationalRegressionTests(TestCase):
    def setUp(self):
        self.reporter = Technician.objects.create(full_name="Reportante QA")
        TechnicianChannel.objects.create(technician=self.reporter, provider="telegram", external_user_id="qa-regression", external_chat_id="qa-regression")
        self.ai = Mock()
        self.processor = IncidentProcessor(self.ai)
        self.service = IncomingMessageService(self.processor)
        self.number = 0

    def send(self, text, identity="qa-regression"):
        self.number += 1
        return self.service.process_text(provider="telegram", external_user_id=identity, external_chat_id=identity, external_message_id=f"qa-{self.number}", text=text, raw_payload={"qa": True})["processing"]

    def draft(self, data=None, status="ready", question=None):
        data = dict(DATA if data is None else data)
        return IncidentDraft.objects.create(technician=self.reporter, extracted_data=data, status=status, missing_fields=[k for k in self.processor.REQUIRED_FIELDS if not data.get(k)], last_question=question or self.processor._build_confirmation_reply(data))

    def test_multiple_corrections_preserve_unmentioned_fields(self):
        draft = self.draft()
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**DATA, "unit": "Estudio 8", "equipment": "Cámara 4", "location_context": "Estudio 8", "priority": "critical"})
        result = self.send("Me equivoqué: es Cámara 4, ocurre en Estudio 8 y la prioridad es alta, no crítica.")
        draft.refresh_from_db()
        for field, value in dict(equipment="Cámara 4", location_context="Estudio 8", priority="high", area=DATA["area"], operational_impact=DATA["operational_impact"], failure_type=DATA["failure_type"]).items():
            self.assertEqual(draft.extracted_data[field], value)
        self.assertEqual(draft.status, "ready")
        self.assertFalse(result["incident_created"])
        self.assertIn("Cámara 4", draft.extracted_data["description"])

    def test_explicit_priority_excludes_negated_alternative_and_questions(self):
        self.assertEqual(self.processor._extract_explicit_priority("La prioridad es alta, no crítica"), "high")
        self.assertIsNone(self.processor._extract_explicit_priority("¿La prioridad es alta?"))
        self.assertEqual(self.processor._extract_explicit_priority("¿Puedes ayudarme? La prioridad es alta."), "high")
        self.assertIsNone(self.processor._extract_explicit_priority("alta o crítica"))

    def test_ambiguous_equipment_requires_clarification(self):
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**DATA, "equipment": "Cámara 3 o Cámara 4"})
        result = self.send("Puede ser Cámara 3 o Cámara 4 del Móvil 2; no sé cuál.")
        draft = IncidentDraft.objects.get(technician=self.reporter)
        self.assertEqual(draft.status, "collecting")
        self.assertIn("equipment", result["missing_fields"])
        self.assertIsNone(draft.extracted_data["equipment"])

    def test_compound_impact_reply_does_not_lose_priority(self):
        data = {**DATA, "operational_impact": None, "priority": None}
        draft = self.draft(data, status="collecting", question=self.processor.FALLBACK_QUESTIONS["operational_impact"])
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**DATA, "priority": "medium", "operational_impact": "Impide enviar material; usamos respaldo"})
        self.send("Además impide enviar material de producción; usamos un respaldo. Prioridad media.")
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data["priority"], "medium")
        self.assertEqual(draft.status, "ready")
        self.assertNotIn("Prioridad", draft.extracted_data["operational_impact"])

    def test_equipment_clarification_can_correct_symptom_while_collecting(self):
        draft = self.draft({**DATA, "equipment": None, "failure_type": "Corte total de imagen"}, status="collecting", question="¿Qué cámara presenta la falla?")
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**DATA, "equipment": "Cámara 4", "failure_type": "Imagen intermitente"})
        self.send("Revisé: es la Cámara 4. No está completamente caída la imagen, es intermitente.")
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data["equipment"], "Cámara 4")
        self.assertEqual(draft.extracted_data["failure_type"], "Imagen intermitente")
        self.assertEqual(draft.extracted_data["priority"], "critical")

    def test_repeated_confirmation_returns_existing_code_without_ai(self):
        self.draft()
        first = self.send("confirmo")
        second = self.send("confirmo")
        self.assertEqual(Incident.objects.count(), 1)
        self.assertIn(first["incident_code"], second["reply"])
        self.ai.extract_incident.assert_not_called()

    def test_transport_requires_successful_delivery_before_confirmation(self):
        draft = self.draft()
        self.processor.require_delivered_summary = True
        first = self.send("confirmo")
        self.assertFalse(first["incident_created"])
        outbound = ChannelMessage.objects.create(provider="telegram", direction="outbound", status="failed", content=first["reply"], technician=self.reporter, draft=draft, external_sender_id="bot")
        second = self.send("confirmo")
        self.assertFalse(second["incident_created"])
        self.assertEqual(Incident.objects.count(), 0)
        outbound.status = "sent"
        outbound.save(update_fields=["status"])
        third = self.send("confirmo")
        self.assertTrue(third["incident_created"])
        self.assertEqual(Incident.objects.count(), 1)
        self.ai.extract_incident.assert_not_called()

    def test_old_summary_does_not_authorize_corrected_data(self):
        draft = self.draft()
        self.processor.require_delivered_summary = True
        ChannelMessage.objects.create(provider="telegram", direction="outbound", status="sent", content=draft.last_question, technician=self.reporter, draft=draft, external_sender_id="bot")
        draft.extracted_data = {**DATA, "equipment": "Cámara 4"}
        draft.last_question = self.processor._build_confirmation_reply(draft.extracted_data)
        draft.save(update_fields=["extracted_data", "last_question"])
        result = self.send("confirmo")
        self.assertFalse(result["incident_created"])
        self.assertIn("Cámara 4", result["reply"])
        self.assertEqual(Incident.objects.count(), 0)

    def test_long_summary_is_delivered_in_parts_before_creating_incident(self):
        self.processor.require_delivered_summary = True
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**DATA, "description": "La cámara pierde imagen 🎥. " * 300})
        sent = []

        def send_text(recipient, text):
            self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4000)
            sent.append(text)
            return {"result": {"message_id": 1000 + len(sent)}}

        messaging = Mock()
        messaging.send_text.side_effect = send_text
        handler = TelegramUpdateHandler(messaging_provider=messaging, incoming_service=self.service)

        def update(number, text):
            return {"message": {"message_id": number, "from": {"id": "qa-regression"}, "chat": {"id": "qa-regression", "type": "private"}, "text": text}}

        first = handler.handle(update(1, "La Cámara 3 del Móvil 2 pierde imagen; prioridad crítica."))
        self.assertEqual(first["action"], "ask_clarification")
        self.assertGreater(len(sent), 1)
        self.assertIn("¿Confirmas la creación", sent[-1])
        self.assertEqual(Incident.objects.count(), 0)
        result = handler.handle(update(2, "confirmo"))
        self.assertTrue(result["incident_created"])
        self.assertEqual(Incident.objects.count(), 1)

    def test_malformed_provider_preserves_draft_and_pending_text(self):
        draft = self.draft({**DATA, "priority": None, "operational_impact": None}, status="collecting", question="¿Puedes ampliar el contexto?")
        self.ai.extract_incident.side_effect = ValueError("JSON inválido")
        result = self.send("Área Redes: router principal en Sala técnica perdió acceso a la red; afecta material.")
        draft.refresh_from_db()
        self.assertFalse(result["incident_created"])
        self.assertEqual(draft.extracted_data["equipment"], DATA["equipment"])
        self.assertIn("router", draft.extracted_data["_pending_messages"][0])

    def test_summary_is_revalidated_before_persistence(self):
        draft = self.draft({**DATA, "priority": None})
        result = self.send("confirmo")
        self.assertFalse(result["incident_created"])
        self.assertEqual(Incident.objects.count(), 0)
        draft.refresh_from_db()
        self.assertEqual(draft.status, "collecting")

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", INCIDENT_NOTIFICATION_EMAIL="qa@example.invalid")
    def test_mail_failure_does_not_rollback_incident(self):
        self.draft()
        with patch("incidents.services.incident_notification_service.send_mail", side_effect=OSError("Fallo QA")):
            with self.captureOnCommitCallbacks(execute=True):
                result = self.send("confirmo")
        self.assertTrue(result["incident_created"])
        self.assertEqual(Incident.objects.count(), 1)

    def test_other_reporter_has_an_independent_draft(self):
        original = self.draft()
        other = Technician.objects.create(full_name="Otro reportante QA")
        TechnicianChannel.objects.create(technician=other, provider="telegram", external_user_id="qa-other")
        self.ai.extract_incident.return_value = IncidentExtractionResult(area="Audio", equipment="Micrófono 2")
        self.send("Falla el Micrófono 2", identity="qa-other")
        original.refresh_from_db()
        self.assertEqual(original.extracted_data, DATA)
        self.assertEqual(IncidentDraft.objects.count(), 2)

    def test_overlong_equipment_requires_clarification_before_sql(self):
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**DATA, "equipment": "Cámara " + "x" * 180})
        result = self.send("Reporte de cámara con identificador inválido")
        self.assertIn("equipment", result["missing_fields"])
        self.assertEqual(Incident.objects.count(), 0)

    def test_provider_cannot_invent_priority_from_urgency(self):
        self.ai.extract_incident.return_value = IncidentExtractionResult(**DATA)
        result = self.send("La Cámara 3 del Móvil 2 pierde imagen; es muy urgente")
        self.assertIn("priority", result["missing_fields"])
        self.assertIsNone(result["extracted_data"]["priority"])

    def test_failed_persistence_keeps_ready_draft_for_retry(self):
        draft = self.draft()
        with patch.object(Incident.objects, "create", side_effect=IntegrityError("Fallo de persistencia QA")):
            with self.assertRaises(IntegrityError):
                self.send("confirmo")
        draft.refresh_from_db()
        self.assertEqual(draft.status, "ready")
        self.assertIsNone(draft.incident_id)
        result = self.send("confirmo")
        self.assertTrue(result["incident_created"])
        self.assertEqual(Incident.objects.count(), 1)


class ConcurrentConfirmationTests(TransactionTestCase):
    def setUp(self):
        self.reporter = Technician.objects.create(full_name="QA concurrencia")
        TechnicianChannel.objects.create(technician=self.reporter, provider="telegram", external_user_id="qa-concurrent")
        IncidentDraft.objects.create(technician=self.reporter, extracted_data=DATA, status="ready", last_question=IncidentProcessor(Mock())._build_confirmation_reply(DATA))

    def send(self, message_id):
        close_old_connections()
        try:
            return IncomingMessageService(IncidentProcessor(Mock())).process_text(provider="telegram", external_user_id="qa-concurrent", external_chat_id="qa-concurrent", external_message_id=message_id, text="confirmo", raw_payload={"qa": True})
        finally:
            close_old_connections()

    def test_concurrent_confirmations_create_one_incident(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(self.send, ["confirmation-1", "confirmation-2"]))
        self.assertEqual(Incident.objects.count(), 1)
        codes = {r["processing"].get("incident_code") for r in results}
        self.assertEqual(len(codes), 1)

    def test_duplicate_delivery_is_idempotent_under_concurrency(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(self.send, ["same-id", "same-id"]))
        self.assertEqual(Incident.objects.count(), 1)
        self.assertEqual(sum(r["duplicate"] for r in results), 1)
        self.assertEqual(ChannelMessage.objects.filter(external_message_id="same-id").count(), 1)


@override_settings(GEMINI_API_KEY="qa-placeholder")
class GeminiResponseValidationTests(TestCase):
    def test_non_object_json_is_rejected(self):
        provider = GeminiAIProvider()
        with patch.object(provider, "_create_interaction_with_retry", return_value=SimpleNamespace(output_text="[]")):
            with self.assertRaises(ValueError):
                provider.extract_incident("Reporte QA")

    def test_wrong_field_types_are_rejected(self):
        provider = GeminiAIProvider()
        with patch.object(provider, "_create_interaction_with_retry", return_value=SimpleNamespace(output_text=json.dumps({"intent": "incident_report", "equipment": ["Cámara"]}))):
            with self.assertRaises(ValueError):
                provider.extract_incident("Reporte QA")
