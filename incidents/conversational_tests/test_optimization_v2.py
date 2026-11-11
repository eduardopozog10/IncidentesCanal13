"""Escenarios v2 reproducibles: contratos de IA simulados y PostgreSQL temporal."""
from copy import deepcopy

from django.test import TestCase, override_settings
from django.utils import timezone

from incidents.ai.schemas import IncidentExtractionResult
from incidents.evaluation.runner import no_external_connections
from incidents.models import ChannelMessage, Incident, IncidentDraft, IncidentNotification
from incidents.services.incident_dialogue import explanation_requested
from incidents.test_natural_confirmation import ConfirmationFixture, DATA
from incidents.test_user_management import SETTINGS


@override_settings(DEBUG=False, TELEGRAM_AUTO_REGISTER=False, PILOT_MESSAGE_RETRY_SECONDS=0, **SETTINGS)
class ConversationV2Scenarios(ConfirmationFixture, TestCase):
    def setUp(self):
        self.external_attempts = self.enterContext(no_external_connections())
        self.addCleanup(self.assertEqual, self.external_attempts, [])
        super().setUp()

    def seed(self, **changes):
        data = {**deepcopy(DATA), "priority": "critical",
                "description": "La Cámara 2 del Estudio A pierde imagen desde hace 20 minutos. Sospechamos del cable, sin confirmar.",
                "operational_impact": "Las pruebas están detenidas. Hay una cámara de respaldo disponible sin conectar.", **changes}
        data["_operational_facts"] = {"consequence": "Las pruebas están detenidas.",
            "backup": "Hay una cámara de respaldo disponible sin conectar.",
            "original_fault": "La Cámara 2 sigue fallando.",
            "_states": {"consequence": "stopped", "backup": "available", "original_fault": "faulty"}}
        processor = self.handler.incoming_service.incident_processor
        missing = [name for name in processor.REQUIRED_FIELDS if not data.get(name)]
        question = processor.FALLBACK_QUESTIONS[missing[0]] if missing else processor._build_confirmation_reply(data)
        draft = IncidentDraft.objects.create(technician=self.reporter, extracted_data=data,
            status="collecting" if missing else "ready", missing_fields=missing, last_question=question)
        if not missing:
            ChannelMessage.objects.create(provider="telegram", technician=self.reporter, draft=draft,
                external_sender_id="bot", direction="outbound", content=question, status="sent", processed_at=timezone.now())
        return draft

    def interpret(self, draft, **metadata):
        changes = metadata.pop("data", {})
        data = {k: v for k, v in draft.extracted_data.items() if not k.startswith("_")}
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**data, **changes}, **metadata)

    @staticmethod
    def snapshot(draft):
        draft.refresh_from_db()
        return deepcopy(draft.extracted_data), draft.status, draft.last_question, draft.updated_at

    def observe(self, case_id, text, expected, result, draft):
        draft.refresh_from_db()
        self.observation = {"case_id": case_id, "mode": "synthetic_backend", "input": text,
            "expected": expected, "action": result.get("action"), "reply": self.last_reply(),
            "draft_status": draft.status, "data": deepcopy(draft.extracted_data),
            "ai_calls": self.ai.extract_incident.call_count, "incidents": Incident.objects.count(),
            "notifications": IncidentNotification.objects.count()}

    def test_01_backup_update_and_missing_query_preserve_original_fault(self):
        draft = self.seed(priority=None)
        text = "La cámara sigue fallando, pero conectamos el respaldo. ¿Qué falta para registrar el incidente?"
        self.interpret(draft, update_kind="evolution", dialogue_actions=["update", "query"], requested_fields=["missing"],
            field_evidence={"operational_impact": "conectamos el respaldo"}, operational_updates=[
                {"facet": "backup", "text": "Conectamos el respaldo.", "evidence": "conectamos el respaldo", "state": "none", "backup_state": None},
                {"facet": "original_fault", "text": "La cámara sigue fallando.", "evidence": "La cámara sigue fallando", "state": "faulty", "backup_state": None}])
        result = self.send(text)
        self.observe("V2-01", text, "Guardar conexión y falla original; pedir prioridad pendiente sin crear ni afirmar reanudación.", result, draft)
        self.assertIn("Conectamos el respaldo", draft.extracted_data["operational_impact"])
        self.assertIn("sigue fallando", draft.extracted_data["operational_impact"])
        self.assertIn("Prioridad", self.last_reply())
        self.assertNotIn("• Equipo:", self.last_reply())
        self.assertIsNone(draft.extracted_data["priority"])
        self.assertEqual(draft.extracted_data["_operational_facts"]["_states"]["consequence"], "stopped")
        self.assertFalse(Incident.objects.exists())

    def test_02_priority_update_does_not_confirm_hypothesis(self):
        draft = self.seed()
        text = "Cambia la prioridad a alta, aunque todavía no sabemos si la causa es el cable"
        self.interpret(draft, data={"priority": "high", "description": draft.extracted_data["description"] + " Todavía no sabemos si la causa es el cable."},
            update_kind="correction", dialogue_actions=["update"],
            field_evidence={"priority": "prioridad a alta", "description": "todavía no sabemos si la causa es el cable"})
        impact = draft.extracted_data["operational_impact"]
        result = self.send(text)
        self.observe("V2-02", text, "Guardar Alta y conservar causa no confirmada, síntoma, tiempo y respaldo.", result, draft)
        self.assertEqual(draft.extracted_data["priority"], "high")
        self.assertIn("sin confirmar", draft.extracted_data["description"])
        self.assertIn("20 minutos", draft.extracted_data["description"])
        self.assertEqual(draft.extracted_data["operational_impact"], impact)
        self.assertNotRegex(draft.extracted_data["description"].lower(), r"(?:causa confirmada|confirmamos que)")
        self.assertFalse(Incident.objects.exists())

    def test_03_explanation_with_continuation_does_not_reask_saved_impact(self):
        draft = self.seed()
        before = self.snapshot(draft)
        text = "¿Me explicas impacto operacional? Después seguimos con el reporte"
        self.ai.extract_incident.return_value = IncidentExtractionResult(intent="conversation", dialogue_action="help",
            dialogue_actions=["help"], help_field="operational_impact", action_evidence="Me explicas impacto operacional")
        result = self.send(text)
        self.observe("V2-03", text, "Explicar el concepto sin pedir impacto ya guardado, sin IA ni cambios de contexto.", result, draft)
        self.assertIn("cómo afecta", self.last_reply())
        self.assertNotIn("¿Qué impacto operacional tiene?", self.last_reply())
        self.assertEqual(self.snapshot(draft), before)
        self.ai.extract_incident.assert_not_called()
        self.assertFalse(Incident.objects.exists())

    def test_04_location_correction_preserves_every_other_fact(self):
        draft = self.seed()
        before = self.snapshot(draft)
        text = "Me equivoqué, la ubicación es Estudio B. No cambies el resto"
        self.interpret(draft, data={"location_context": "Estudio B", "unit": "Estudio B"}, update_kind="correction",
            dialogue_actions=["update"], field_evidence={"location_context": "Estudio B"})
        result = self.send(text)
        self.observe("V2-04", text, "Guardar Estudio B y sincronizar descripción, conservando todos los demás hechos.", result, draft)
        self.assertEqual(draft.extracted_data["location_context"], "Estudio B")
        self.assertEqual(draft.extracted_data["unit"], "Estudio B")
        self.assertEqual(draft.extracted_data["description"], before[0]["description"].replace("Estudio A", "Estudio B"))
        for name, value in before[0].items():
            if name not in {"description", "location_context", "unit"}:
                self.assertEqual(draft.extracted_data[name], value)
        self.assertFalse(Incident.objects.exists())

    def test_05_elapsed_fault_time_is_not_backup_event_age(self):
        draft = self.seed()
        text = "Ya llevamos 40 minutos con la falla. El respaldo lo conectamos hace 10. ¿Cuánto tiempo lleva realmente el problema?"
        self.interpret(draft, data={"description": draft.extracted_data["description"] + " Ya llevamos 40 minutos con la falla."},
            update_kind="additional", dialogue_actions=["update", "query"], requested_fields=["time"],
            field_evidence={"description": "Ya llevamos 40 minutos con la falla"}, operational_updates=[
                {"facet": "backup", "text": "El respaldo lo conectamos hace 10.", "evidence": "El respaldo lo conectamos hace 10", "state": "none", "backup_state": None}])
        result = self.send(text)
        self.observe("V2-05", text, "Guardar 40 minutos de falla; conexión hace 10 sin inferir unidades; respuesta solo sobre duración.", result, draft)
        self.assertIn("40 minutos", draft.extracted_data["description"])
        self.assertNotIn("20 minutos", draft.extracted_data["description"])
        self.assertIn("sin confirmar", draft.extracted_data["description"])
        self.assertIn("conectamos hace 10", draft.extracted_data["operational_impact"])
        self.assertIn("40 minutos", self.last_reply())
        self.assertNotIn("hace 10", self.last_reply())
        self.assertNotIn("• Equipo:", self.last_reply())
        self.assertFalse(Incident.objects.exists())

    def test_06_review_before_confirmation_is_read_only_and_delivers_summary(self):
        draft = self.seed()
        before = self.snapshot(draft)
        text = "¿Puedes revisar cómo quedó el reporte antes de que lo confirme?"
        self.interpret(draft, intent="incident_followup", dialogue_action="query", dialogue_actions=["query"],
            action_evidence="revisar cómo quedó el reporte", requested_fields=["summary"], field_evidence={})
        result = self.send(text)
        self.observe("V2-06", text, "Entregar resumen completo sin crear ni mutar el borrador; confirmar después crea una vez.", result, draft)
        self.assertIn("Información guardada en el borrador", self.last_reply())
        for label in ("• Equipo/servicio:", "• Prioridad:", "• Impacto operacional:"):
            self.assertIn(label, self.last_reply())
        self.assertEqual(self.snapshot(draft), before)
        self.assertFalse(Incident.objects.exists())
        self.send("Sí, confirmo la creación del incidente.")
        self.assertEqual(Incident.objects.count(), 1)
        self.assertEqual(IncidentNotification.objects.count(), 1)

    def test_07_definition_and_actual_correction_both_are_processed(self):
        draft = self.seed()
        before = self.snapshot(draft)
        text = "¿Me explicas impacto operacional? Después cambia la prioridad a alta."
        self.interpret(draft, data={"priority": "high"}, update_kind="correction", dialogue_action="help",
            dialogue_actions=["help", "update"], help_field="operational_impact",
            action_evidence="Me explicas impacto operacional", field_evidence={"priority": "prioridad a alta"})
        result = self.send(text)
        self.observe("V2-07", text, "Guardar Alta, reconocer el cambio y explicar impacto sin pedir el dato ya registrado.", result, draft)
        self.assertEqual(draft.extracted_data["priority"], "high")
        self.assertEqual(draft.extracted_data["operational_impact"], before[0]["operational_impact"])
        self.assertIn("Guardé los cambios de prioridad", self.last_reply())
        self.assertIn("cómo afecta", self.last_reply())
        self.assertNotIn("¿Qué impacto operacional tiene?", self.last_reply())
        self.ai.extract_incident.assert_called_once()
        self.assertFalse(Incident.objects.exists())

    def test_neutral_continuation_variants_leave_context_unchanged(self):
        draft = self.seed()
        before = self.snapshot(draft)
        for text in ("¿Qué significa prioridad alta? Luego continuamos con el borrador.",
                     "Explícame impacto operacional. Más tarde retomamos el reporte.",
                     "¿Me explicas prioridad?Después seguimos."):
            with self.subTest(text=text):
                self.send(text)
                self.assertEqual(self.snapshot(draft), before)
                self.assertNotIn("¿Qué prioridad seleccionas", self.last_reply())
                self.assertNotIn("¿Qué impacto operacional tiene?", self.last_reply())
        self.ai.extract_incident.assert_not_called()

    def test_neutral_followup_without_draft_does_not_create_one(self):
        self.send("¿Me explicas impacto operacional? Luego continuamos con el reporte.")
        self.assertIn("cómo afecta", self.last_reply())
        self.ai.extract_incident.assert_not_called()
        self.assertFalse(IncidentDraft.objects.exists())
        self.assertFalse(Incident.objects.exists())

    def test_tail_with_location_facts_reaches_interpreter_and_preserves_other_fields(self):
        draft = self.seed()
        before = self.snapshot(draft)
        text = "¿Me explicas prioridad? La ubicación correcta es Estudio B."
        self.interpret(draft, data={"unit": "Estudio B", "location_context": "Estudio B"},
            update_kind="correction", dialogue_action="help", dialogue_actions=["update", "help"],
            help_field="priority", action_evidence="Me explicas prioridad", field_evidence={"location_context": "Estudio B"})
        self.send(text)
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data["location_context"], "Estudio B")
        self.assertEqual(draft.extracted_data["priority"], before[0]["priority"])
        self.assertEqual(draft.extracted_data["operational_impact"], before[0]["operational_impact"])
        self.assertIn("Guardé los cambios de ubicación", self.last_reply())
        self.assertIn("• Baja:", self.last_reply())
        self.ai.extract_incident.assert_called_once()

    def test_new_incident_context_clarification_is_not_hidden_by_help(self):
        draft = self.seed()
        before = deepcopy(draft.extracted_data)
        text = "¿Me explicas impacto operacional? Tengo otra incidencia independiente: el Router 4 no conecta en Redes."
        self.interpret(draft, data={"equipment": "Router 4", "area": "Redes"}, update_kind="new_incident",
            dialogue_action="help", dialogue_actions=["update", "help"], help_field="operational_impact",
            action_evidence="Me explicas impacto operacional", field_evidence={"equipment": "Router 4", "area": "Redes"})
        result = self.send(text)
        self.observe("V2-CONTEXT-NEW", text, "Conservar datos y mostrar aclaración de otra incidencia antes de continuar.", result, draft)
        self.assertEqual(draft.extracted_data, before)
        self.assertIn("otra incidencia", self.last_reply())
        self.assertIn("Mantengo el borrador actual", self.last_reply())
        self.assertFalse(Incident.objects.exists())

    def test_ambiguous_update_clarification_is_not_hidden_by_help(self):
        draft = self.seed()
        before = deepcopy(draft.extracted_data)
        text = "¿Me explicas impacto operacional? No sé si falla la Cámara 2 o la Cámara 3."
        self.interpret(draft, data={"equipment": "Cámara 2 o Cámara 3"}, update_kind="ambiguous",
            dialogue_action="help", dialogue_actions=["update", "help"], help_field="operational_impact",
            action_evidence="Me explicas impacto operacional", field_evidence={"equipment": "la Cámara 2 o la Cámara 3"})
        result = self.send(text)
        self.observe("V2-CONTEXT-AMBIGUOUS", text, "Conservar datos y pedir aclaración sin ocultarla con ayuda conceptual.", result, draft)
        self.assertEqual(draft.extracted_data, before)
        self.assertIn("¿Cuál es el dato correcto", self.last_reply())
        self.assertFalse(Incident.objects.exists())

    def test_help_and_correction_do_not_replace_delivered_updated_summary(self):
        draft = self.seed()
        self.interpret(draft, data={"priority": "high"}, update_kind="correction", dialogue_action="help",
            dialogue_actions=["help", "update"], help_field="operational_impact",
            action_evidence="Me explicas impacto operacional", field_evidence={"priority": "prioridad a alta"})
        self.send("¿Me explicas impacto operacional? Después cambia la prioridad a alta.")
        self.send("¿Qué significa prioridad alta? Luego seguimos con el reporte.")
        self.send("Sí")
        self.assertFalse(Incident.objects.exists())
        self.assertIn("¿Confirmas", self.last_reply())
        self.send("Sí")
        self.assertEqual(Incident.objects.get().priority, "high")
        self.send("Sí")
        self.assertEqual(Incident.objects.count(), 1)
        self.assertEqual(IncidentNotification.objects.count(), 1)

    def test_mixed_request_recovers_once_without_losing_context(self):
        draft = self.seed()
        before = deepcopy(draft.extracted_data)
        self.ai.extract_incident.side_effect = TimeoutError("Fallo simulado, sin API")
        text = "¿Me explicas impacto operacional? Después cambia la prioridad a alta."
        self.send(text)
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data["priority"], before["priority"])
        self.ai.extract_incident.side_effect = None
        self.interpret(draft, data={"priority": "high"}, update_kind="correction", dialogue_action="help",
            dialogue_actions=["update", "help"], help_field="operational_impact",
            action_evidence="Me explicas impacto operacional", field_evidence={"priority": "prioridad a alta"})
        self.handler.recover_pending()
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data["priority"], "high")
        self.assertEqual(draft.extracted_data["operational_impact"], before["operational_impact"])
        self.assertIn("sin confirmar", draft.extracted_data["description"])
        calls = self.ai.extract_incident.call_count
        self.handler.recover_pending()
        self.assertEqual(self.ai.extract_incident.call_count, calls)
        self.assertEqual(ChannelMessage.objects.filter(direction="inbound", content=text).count(), 1)
        self.assertFalse(Incident.objects.exists())

    def test_other_reporters_context_is_never_updated(self):
        from incidents.test_qa_extreme import handler_fixture
        draft = self.seed()
        other, _, other_transport, _ = handler_fixture(user=202)
        other_draft = IncidentDraft.objects.create(technician=other, extracted_data=deepcopy(draft.extracted_data),
            status="ready", last_question="Resumen de otro reportante")
        before = self.snapshot(other_draft)
        self.interpret(draft, data={"priority": "high"}, update_kind="correction", dialogue_actions=["update", "help"],
            dialogue_action="help", help_field="operational_impact", action_evidence="Me explicas impacto operacional",
            field_evidence={"priority": "prioridad a alta"})
        self.send("¿Me explicas impacto operacional? Después cambia la prioridad a alta.")
        self.assertEqual(self.snapshot(other_draft), before)
        other_transport.send_text.assert_not_called()

    def test_non_neutral_tails_cannot_be_consumed_as_local_explanation(self):
        for tail in ("Después cambia la prioridad a alta.", "La falla lleva 40 minutos.",
                     "Tengo otra incidencia: el Router 4 no conecta.", "Cancela el borrador.",
                     "Sí, confirmo la creación del incidente.", "Luego continuamos con el reporte a las 17:00."):
            with self.subTest(tail=tail):
                self.assertIsNone(explanation_requested("¿Me explicas impacto operacional? " + tail))

    def test_guidance_for_missing_information_still_asks_for_it(self):
        draft = self.seed(operational_impact=None)
        draft.extracted_data.pop("_operational_facts")
        draft.save(update_fields=["extracted_data"])
        self.ai.extract_incident.return_value = IncidentExtractionResult(intent="conversation", dialogue_action="help",
            dialogue_actions=["help"], help_field="operational_impact", action_evidence="No entiendo qué dato necesitas")
        self.send("No entiendo qué dato necesitas para completar el impacto")
        draft.refresh_from_db()
        self.assertIn("¿Qué impacto operacional tiene?", self.last_reply())
        self.assertIsNone(draft.extracted_data["operational_impact"])
        self.assertFalse(Incident.objects.exists())

    def test_new_causal_uncertainty_is_not_lost_in_priority_shortcut(self):
        draft = self.seed(description="La Cámara 2 del Estudio A pierde imagen desde hace 20 minutos.")
        text = "Cambia la prioridad a alta, aunque todavía no sabemos si la causa es el cable"
        self.interpret(draft, data={"priority": "high", "description": draft.extracted_data["description"] + " Todavía no sabemos si la causa es el cable."},
            update_kind="correction", dialogue_actions=["update"],
            field_evidence={"priority": "prioridad a alta", "description": "todavía no sabemos si la causa es el cable"})
        result = self.send(text)
        self.observe("V2-HYPOTHESIS-NEW", text, "Interpretar el mensaje mixto, guardar Alta y la nueva incertidumbre causal sin convertirla en diagnóstico.", result, draft)
        self.assertEqual(draft.extracted_data["priority"], "high")
        self.assertIn("no sabemos si la causa es el cable", draft.extracted_data["description"])
        self.assertIn("20 minutos", draft.extracted_data["description"])
        self.ai.extract_incident.assert_called_once()
        self.assertFalse(Incident.objects.exists())
