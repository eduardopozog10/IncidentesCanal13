"""Optimización v1: PostgreSQL de tests y proveedores simulados exclusivamente."""
from copy import deepcopy

from django.test import TestCase, override_settings

from incidents.ai.schemas import IncidentExtractionResult
from incidents.evaluation.runner import no_external_connections
from incidents.models import ChannelMessage, Incident, IncidentDraft, IncidentNotification
from incidents.services.incident_processor import IncidentProcessor
from incidents.test_natural_confirmation import ConfirmationFixture, DATA
from incidents.test_user_management import SETTINGS
from incidents.test_telegram_groups import GroupFixture


@override_settings(DEBUG=False, TELEGRAM_AUTO_REGISTER=False, PILOT_MESSAGE_RETRY_SECONDS=0, **SETTINGS)
class ConversationOptimizationTests(ConfirmationFixture, TestCase):
    def setUp(self):
        self.external_attempts = self.enterContext(no_external_connections())
        self.addCleanup(self.assertEqual, self.external_attempts, [])
        super().setUp()

    @staticmethod
    def snapshot(draft):
        draft.refresh_from_db()
        return deepcopy(draft.extracted_data), draft.status, draft.last_question, draft.updated_at

    def correction(self, text):
        self.ai.extract_incident.return_value = IncidentExtractionResult(
            **{**deepcopy(DATA), "priority": "critical"}, update_kind="correction",
            dialogue_actions=["update"], field_evidence={"priority": "crítica"})
        return self.send(text)

    def test_priority_definition_reads_no_data_and_does_not_repeat_priority_question(self):
        draft = self.ready()
        before, calls = self.snapshot(draft), self.ai.extract_incident.call_count
        self.send("¿Qué significa la prioridad?")
        self.assertIn("• Baja:", self.last_reply())
        self.assertIn("• Crítica:", self.last_reply())
        self.assertNotIn("Según el borrador guardado", self.last_reply())
        self.assertNotIn("¿Cuál corresponde", self.last_reply())
        self.assertEqual(self.snapshot(draft), before)
        self.assertEqual(self.ai.extract_incident.call_count, calls)
        self.assertFalse(Incident.objects.exists())

    def test_priority_definition_without_draft_does_not_start_report_or_call_ai(self):
        self.ai.extract_incident.return_value = IncidentExtractionResult(intent="conversation")
        self.send("¿Qué significa prioridad alta?")
        self.assertIn("Alta", self.last_reply())
        self.assertIn("función importante", self.last_reply())
        self.ai.extract_incident.assert_not_called()
        self.assertFalse(IncidentDraft.objects.exists())

    def test_impact_definition_does_not_answer_with_missing_saved_value(self):
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**DATA, "operational_impact": None})
        self.send("Cámara 2 en Estudio A sin imagen; Producción, prioridad alta. No he indicado el impacto.")
        draft = IncidentDraft.objects.get()
        before, calls = self.snapshot(draft), self.ai.extract_incident.call_count
        self.send("¿Qué es el impacto operacional?")
        self.assertIn("cómo afecta", self.last_reply())
        self.assertNotIn("• Impacto operacional: No informado", self.last_reply())
        self.assertEqual(self.snapshot(draft), before)
        self.assertEqual(self.ai.extract_incident.call_count, calls)
        self.assertFalse(IncidentNotification.objects.exists())

    def test_polite_correction_reaches_existing_update_pipeline(self):
        draft = self.ready()
        before, calls = self.snapshot(draft), self.ai.extract_incident.call_count
        self.correction("¿Podrías corregir la prioridad a crítica?")
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data["priority"], "critical")
        for field in ("equipment", "description", "operational_impact", "area", "location_context"):
            self.assertEqual(draft.extracted_data[field], before[0][field])
        self.assertIn("Crítica", self.last_reply())
        self.assertEqual(self.ai.extract_incident.call_count, calls + 1)
        self.assertFalse(Incident.objects.exists())
        self.assertFalse(IncidentNotification.objects.exists())

    def test_saved_priority_question_stays_read_only(self):
        draft = self.ready()
        before, calls = self.snapshot(draft), self.ai.extract_incident.call_count
        self.send("¿Qué prioridad tiene guardada el borrador?")
        self.assertIn("Alta", self.last_reply())
        self.assertNotIn("• Baja:", self.last_reply())
        self.assertEqual(self.snapshot(draft), before)
        self.assertEqual(self.ai.extract_incident.call_count, calls)

    def test_hypothetical_change_question_does_not_write_or_call_ai(self):
        draft = self.ready()
        before, calls = self.snapshot(draft), self.ai.extract_incident.call_count
        self.send("¿Qué pasaría si cambio la prioridad a crítica?")
        self.assertEqual(self.snapshot(draft), before)
        self.assertEqual(self.ai.extract_incident.call_count, calls)
        self.assertFalse(Incident.objects.exists())

    def test_targeted_priority_definition_is_short_and_does_not_reask_saved_choice(self):
        draft = self.ready()
        before, calls = self.snapshot(draft), self.ai.extract_incident.call_count
        for text in ("¿Puedes explicarme la prioridad baja?", "Explícame prioridad crítica, por favor.",
                     "¿Me explicas la prioridad media?"):
            with self.subTest(text=text):
                self.send(text)
                self.assertEqual(self.last_reply().count("• "), 1)
                self.assertLess(len(self.last_reply()), 380)
                self.assertNotIn("¿Qué prioridad", self.last_reply())
                self.assertEqual(self.snapshot(draft), before)
        self.assertEqual(self.ai.extract_incident.call_count, calls)

    def test_explanation_does_not_invalidate_current_delivered_summary(self):
        self.ready()
        self.send("¿Qué es el impacto operacional?")
        self.send("Sí, confirmo la creación del incidente.")
        self.assertEqual(Incident.objects.count(), 1)
        self.assertEqual(IncidentNotification.objects.count(), 1)

    def test_correction_and_explanation_do_not_authorize_undelivered_updated_summary(self):
        self.ready()
        self.correction("¿Puedes cambiar la prioridad a crítica?")
        self.send("¿Qué significa prioridad crítica?")
        self.send("Sí")
        self.assertFalse(Incident.objects.exists())
        self.assertIn("¿Confirmas", self.last_reply())
        self.send("Sí")
        self.assertEqual(Incident.objects.get().priority, "critical")
        self.assertEqual(IncidentNotification.objects.count(), 1)
        self.send("Sí")
        self.assertEqual(Incident.objects.count(), 1)

    def test_polite_update_and_query_use_the_persisted_new_value(self):
        draft = self.ready()
        self.correction("¿Podrías corregir la prioridad a crítica y decirme qué prioridad quedó?")
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data["priority"], "critical")
        self.assertIn("Crítica", self.last_reply())
        self.assertNotIn("• Equipo:", self.last_reply())
        self.assertFalse(Incident.objects.exists())

    def test_conditional_deferred_negated_or_explanatory_orders_stay_read_only(self):
        draft = self.ready()
        before, calls = self.snapshot(draft), self.ai.extract_incident.call_count
        for text in ("¿Puedes decirme cómo cambiar la prioridad a crítica?",
                     "¿Puedes corregir la prioridad a crítica si fuera necesario?",
                     "¿Puedes corregir la prioridad a crítica mañana?",
                     "¿Puedes corregir la prioridad a crítica, pero mejor no?",
                     "¿Podrías no cambiar la prioridad a crítica?"):
            with self.subTest(text=text):
                self.send(text)
                self.assertEqual(self.snapshot(draft), before)
                self.assertFalse(Incident.objects.exists())
        self.assertEqual(self.ai.extract_incident.call_count, calls)

    def test_explicit_priority_validator_still_rejects_question_alternatives(self):
        processor = self.handler.incoming_service.incident_processor
        for text in ("¿Es prioridad crítica?", "¿Qué significa cambiar la prioridad a alta?",
                     "¿Puedes cambiar la prioridad a crítica o alta?",
                     "¿Puedes cambiar la descripción? ¿Qué significa prioridad crítica?"):
            with self.subTest(text=text):
                self.assertIsNone(processor._extract_explicit_priority(text))
        self.assertEqual(processor._extract_explicit_priority("¿Puedes corregir la prioridad a crítica, no alta?"), "critical")

    def test_explanation_with_new_facts_is_not_consumed_as_local_definition(self):
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**DATA, "priority": "critical"},
            dialogue_actions=["update", "help"], dialogue_action="help", help_field="priority",
            action_evidence="¿Qué significa prioridad?", field_evidence={"priority": "Prioridad crítica"})
        self.send("Cámara 2 en Estudio A sin imagen. Producción. Impide las pruebas. Prioridad crítica. ¿Qué significa prioridad?")
        self.ai.extract_incident.assert_called_once()
        self.assertEqual(IncidentDraft.objects.get().extracted_data["priority"], "critical")
        self.assertFalse(Incident.objects.exists())

    def test_priority_correction_preserves_backup_hypothesis_and_postgres_context_after_restart(self):
        from incidents.messaging.telegram_update_handler import TelegramUpdateHandler
        from incidents.services.incoming_message_service import IncomingMessageService
        draft = self.ready()
        draft.extracted_data.update(
            description=DATA["description"] + " Se sospecha del cable, sin confirmar.",
            operational_impact="Pruebas detenidas. Otra cámara disponible, sin conectar.",
            _operational_facts={"consequence": "Pruebas detenidas", "backup": "Otra cámara disponible, sin conectar"})
        draft.save(update_fields=["extracted_data"])
        before = self.snapshot(draft)
        self.correction("¿Podrías actualizar la prioridad a crítica?")
        draft.refresh_from_db()
        for field in ("description", "operational_impact", "_operational_facts"):
            self.assertEqual(draft.extracted_data[field], before[0][field])
        self.handler = TelegramUpdateHandler(messaging_provider=self.messaging,
            incoming_service=IncomingMessageService(IncidentProcessor(self.ai, require_delivered_summary=True)))
        calls = self.ai.extract_incident.call_count
        self.send("¿Qué impacto y prioridad quedaron guardados?")
        self.assertIn("sin conectar", self.last_reply())
        self.assertIn("Crítica", self.last_reply())
        self.assertEqual(self.ai.extract_incident.call_count, calls)

    def test_transient_failure_preserves_polite_request_then_recovers_once(self):
        draft = self.ready()
        before = self.snapshot(draft)
        self.ai.extract_incident.side_effect = TimeoutError("Fallo simulado")
        self.send("¿Puedes corregir la prioridad a crítica?")
        pending = self.snapshot(draft)[0]
        self.assertEqual({k: v for k, v in pending.items() if not k.startswith("_")}, before[0])
        self.assertIn("¿Puedes corregir la prioridad a crítica?", pending["_pending_messages"])
        self.ai.extract_incident.side_effect = None
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**DATA, "priority": "critical"},
            update_kind="correction", dialogue_actions=["update"], field_evidence={"priority": "crítica"})
        self.handler.recover_pending()
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data["priority"], "critical")
        calls = self.ai.extract_incident.call_count
        self.handler.recover_pending()
        self.assertEqual(self.ai.extract_incident.call_count, calls)
        self.assertFalse(Incident.objects.exists())
        self.assertEqual(ChannelMessage.objects.filter(direction="inbound", content="¿Puedes corregir la prioridad a crítica?").count(), 1)

    def test_other_reporters_draft_is_not_changed_by_polite_correction(self):
        from incidents.test_qa_extreme import handler_fixture
        draft = self.ready()
        _, other_ai, _, other_handler = handler_fixture(user=202)
        from incidents.test_pilot_recovery import update
        other_ai.extract_incident.return_value = IncidentExtractionResult(**DATA)
        other_handler.handle(update(1, user=202, text="Cámara 2 Estudio A sin imagen. Producción. Impide pruebas. Alta"))
        other = IncidentDraft.objects.exclude(pk=draft.pk).get()
        before = self.snapshot(other)
        self.correction("¿Puedes corregir la prioridad a crítica?")
        self.assertEqual(self.snapshot(other), before)
        self.assertFalse(Incident.objects.exists())


@override_settings(DEBUG=False, TELEGRAM_AUTO_REGISTER=False, PILOT_MESSAGE_RETRY_SECONDS=0, **SETTINGS)
class GroupedOptimizationTests(GroupFixture, TestCase):
    def setUp(self):
        self.external_attempts = self.enterContext(no_external_connections())
        self.addCleanup(self.assertEqual, self.external_attempts, [])
        super().setUp()

    def test_polite_priority_update_in_group_precedes_its_saved_value_question(self):
        self.queue("Cámara 2 Estudio A sin imagen. Producción. Impide pruebas. Prioridad alta")
        self.flush()
        calls = self.ai.extract_incident.call_count
        self.queue("¿Podrías corregir la prioridad a crítica?", delta=12)
        self.queue("¿Qué prioridad quedó?", delta=12.2)
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**DATA, "priority": "critical"},
            update_kind="correction", dialogue_actions=["update", "query"], requested_fields=["priority"],
            field_evidence={"priority": "crítica"})
        self.flush(16)
        self.assertEqual(IncidentDraft.objects.get().extracted_data["priority"], "critical")
        self.assertEqual(self.ai.extract_incident.call_count, calls + 1)
        self.assertEqual(ChannelMessage.objects.filter(direction="inbound").count(), 3)
        self.assertIn("Crítica", self.last_reply())
        self.assertFalse(Incident.objects.exists())
