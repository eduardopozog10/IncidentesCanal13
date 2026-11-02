"""Regresiones del 27/10: contrato simulado; sin Gemini ni Telegram reales."""
from copy import deepcopy
from unittest.mock import patch

from django.test import TestCase, override_settings

from incidents.ai.schemas import IncidentExtractionResult
from incidents.ai.base import AITemporaryError
from incidents.ai.gemini_provider import GeminiAIProvider
from incidents.models import ChannelMessage, Incident, IncidentDraft
from incidents.services.incident_processor import IncidentProcessor
from incidents.test_natural_confirmation import DATA, INITIAL, CONFIRM
from incidents.test_qa_extreme import handler_fixture
from incidents.test_pilot_recovery import update
from incidents.test_user_management import SETTINGS


@override_settings(DEBUG=False, TELEGRAM_AUTO_REGISTER=False, PILOT_MESSAGE_RETRY_SECONDS=0, **SETTINGS)
class ConversationReal27Tests(TestCase):
    def setUp(self):
        self.reporter, self.ai, self.transport, self.handler = handler_fixture()
        self.ai.extract_incident.return_value = IncidentExtractionResult(**deepcopy(DATA))
        self.number = 0

    def send(self, text):
        self.number += 1
        return self.handler.handle(update(self.number, text=text))

    def seed(self, **changes):
        data = {**deepcopy(DATA), "description": "La Cámara 3 del Estudio B presenta líneas verdes. La falla comenzó hace aproximadamente una hora.",
                "equipment": "Cámara 3", "location_context": "Estudio B", "unit": "Estudio B", **changes}
        missing = [name for name in IncidentProcessor.REQUIRED_FIELDS if not data.get(name)]
        return IncidentDraft.objects.create(technician=self.reporter, extracted_data=data,
            status="collecting" if missing else "ready", missing_fields=missing,
            last_question=IncidentProcessor.FALLBACK_QUESTIONS[missing[0]] if missing else IncidentProcessor(self.ai)._build_confirmation_reply(data))

    def reply(self):
        return ChannelMessage.objects.filter(direction="outbound", reply_purpose="reply").last().content

    def assert_query(self, query, expected, **missing):
        draft = self.seed(**missing)
        before = (deepcopy(draft.extracted_data), draft.status, draft.last_question, draft.updated_at)
        self.ai.extract_incident.side_effect = AssertionError("Consultar datos guardados no requiere IA")
        self.send(query)
        draft.refresh_from_db()
        self.assertEqual((draft.extracted_data, draft.status, draft.last_question, draft.updated_at), before)
        self.assertIn(expected, self.reply())
        self.assertFalse(Incident.objects.exists())

    def test_ready_multi_question_does_not_become_correction(self):
        self.assert_query("¿Qué cámara está presentando la falla, en qué estudio se encuentra, cuánto tiempo lleva el problema y qué impacto tiene en nuestras operaciones? No quiero modificar ni confirmar nada.", "una hora")
        for value in ("Cámara 3", "Estudio B", DATA["operational_impact"]): self.assertIn(value, self.reply())

    def test_incomplete_summary_query_is_read_only(self):
        self.assert_query("Muéstrame un resumen de toda la información almacenada, sin modificar nada", "Cámara 3", operational_impact=None)
        self.assertIn("pendiente", self.reply().lower())

    def test_missing_query_field_is_not_invented(self):
        self.assert_query("¿Qué impacto te indiqué antes?", "No informado", operational_impact=None)

    def test_pending_fields_query_is_read_only(self):
        self.assert_query("¿Qué información falta?", "Área", area=None)

    def test_unknown_area_offers_other_pending_information_without_inventing(self):
        draft = self.seed(area=None, priority=None)
        self.send("No sé a qué área pertenece")
        self.assertIn("prioridad", self.reply().lower())
        self.assertNotEqual(self.reply(), IncidentProcessor.FALLBACK_QUESTIONS["area"])
        draft.refresh_from_db(); self.assertIsNone(draft.extracted_data["area"])
        self.assertFalse(Incident.objects.exists())

    def test_repeated_unknown_area_does_not_loop_or_create(self):
        draft = self.seed(area=None)
        for text in ("No sé el área", "Sigo sin saber a qué área corresponde"):
            self.send(text)
            self.assertNotEqual(self.reply(), IncidentProcessor.FALLBACK_QUESTIONS["area"])
            self.assertIn("borrador", self.reply().lower())
        draft.refresh_from_db(); self.assertIsNone(draft.extracted_data["area"])
        self.assertFalse(Incident.objects.exists())

    def test_compatible_followup_is_not_new_incident_only_because_ai_says_so(self):
        draft = self.seed(operational_impact=None)
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**draft.extracted_data, "operational_impact":"Interrumpe la grabación"},
            update_kind="new_incident", field_evidence={"operational_impact":"interrumpe la grabación"})
        self.send("Sobre esta misma Cámara 3 del Estudio B: interrumpe la grabación")
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data["operational_impact"], "Interrumpe la grabación")
        self.assertNotIn("otra incidencia", self.reply().lower())

    def test_genuine_other_incident_does_not_replace_existing(self):
        draft = self.seed()
        before = deepcopy(draft.extracted_data)
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**DATA, "equipment":"Router 4"}, update_kind="new_incident")
        self.send("Tengo otra incidencia independiente: el Router 4 no conecta en Redes")
        draft.refresh_from_db(); self.assertEqual(draft.extracted_data, before)
        self.assertIn("otra incidencia", self.reply().lower())

    def test_equipment_and_temporal_correction_updates_all_references(self):
        draft = self.seed()
        new_description = "La Cámara 2 del Estudio B presenta líneas verdes. La falla comenzó hace aproximadamente dos horas."
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**draft.extracted_data, "equipment":"Cámara 2", "description":new_description},
            update_kind="correction", field_evidence={"equipment":"Cámara 2", "description":"hace aproximadamente dos horas"})
        self.send("La cámara que presenta el problema es la Cámara 2, no la Cámara 3, y la falla comenzó hace aproximadamente dos horas, no una. Los otros campos deben permanecer iguales.")
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data["equipment"], "Cámara 2")
        self.assertIn("dos horas", draft.extracted_data["description"])
        self.assertNotIn("una hora", draft.extracted_data["description"])
        self.assertNotIn("Cámara 3", draft.extracted_data["description"])
        self.assertEqual(draft.extracted_data["priority"], DATA["priority"])

    def test_time_correction_is_not_dropped_when_provider_only_marks_equipment_evidence(self):
        draft = self.seed()
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**draft.extracted_data, "equipment":"Cámara 2"},
            update_kind="correction", field_evidence={"equipment":"Cámara 2"})
        self.send("Corrijo el equipo a Cámara 2 y el inicio: comenzó hace dos horas, no una hora")
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data["equipment"], "Cámara 2")
        self.assertIn("dos horas", draft.extracted_data["description"])
        self.assertNotIn("una hora", draft.extracted_data["description"])

    def test_priority_and_impact_are_applied_together(self):
        draft = self.seed()
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**draft.extracted_data, "priority":"high", "operational_impact":"Interrumpe la grabación; no podemos continuar con la cámara alternativa"},
            update_kind="evolution", field_evidence={"priority":"Alta", "operational_impact":"ahora la falla está interrumpiendo la grabación y ya no podemos continuar con la cámara alternativa"})
        self.send("Cambia la prioridad a Alta. Además, ahora la falla está interrumpiendo la grabación y ya no podemos continuar con la cámara alternativa")
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data["priority"], "high")
        self.assertIn("alternativa", draft.extracted_data["operational_impact"])
        self.assertFalse(Incident.objects.exists())

    def assert_modality(self, text, field, overstated, expected):
        draft = self.seed()
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**draft.extracted_data, field:overstated},
            update_kind="additional" if field == "description" else "evolution", field_evidence={field:text})
        self.send(text)
        draft.refresh_from_db()
        self.assertIn(expected, draft.extracted_data[field])
        self.assertNotIn(overstated, draft.extracted_data[field])
        self.assertFalse(Incident.objects.exists())

    def test_available_backup_is_not_claimed_in_use(self):
        self.assert_modality("Podemos seguir parcialmente usando otra cámara", "operational_impact", "Ya estamos usando otra cámara", "Podemos")

    def test_additional_description_has_sentence_separator(self):
        draft = self.seed()
        detail = "Sospechamos que el cable podría estar fallando, sin confirmación"
        self.ai.extract_incident.return_value = IncidentExtractionResult(**draft.extracted_data, update_kind="additional", field_evidence={"description":detail})
        self.send(detail)
        draft.refresh_from_db()
        self.assertIn("hora. Sospechamos", draft.extracted_data["description"])
        self.assertEqual(draft.extracted_data["description"].count(detail), 1)
        self.send(detail)
        draft.refresh_from_db(); self.assertEqual(draft.extracted_data["description"].count(detail), 1)

    def test_evolution_keeps_unmodified_time_and_diagnostic_hypothesis(self):
        draft = self.seed(description="La Cámara 3 muestra líneas verdes desde hace una hora. Sospechamos el cable, falta revisar.")
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**draft.extracted_data,
            "priority":"critical", "operational_impact":"Detiene la grabación", "description":"La Cámara 3 muestra líneas verdes y detiene la grabación."},
            update_kind="evolution", field_evidence={"priority":"crítica", "description":"detiene la grabación", "operational_impact":"detiene la grabación"})
        self.send("Cambia prioridad a crítica porque ahora detiene la grabación")
        draft.refresh_from_db()
        self.assertIn("una hora", draft.extracted_data["description"])
        self.assertIn("Sospechamos el cable", draft.extracted_data["description"])
        self.assertEqual(draft.extracted_data["priority"], "critical")

    def test_new_report_preserves_available_backup_and_partial_impact(self):
        source = "Hay otra cámara disponible; podríamos continuar de forma parcial"
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**DATA, "operational_impact":"Ya estamos usando otra cámara y operamos sin interrupciones"},
            field_evidence={"operational_impact":source})
        self.send("Producción: Cámara 2 Estudio A sin imagen. Prioridad alta. " + source)
        self.assertIn("parcial", IncidentDraft.objects.get().extracted_data["operational_impact"])
        self.assertNotIn("estamos usando", self.reply())

    def test_help_question_followed_by_report_does_not_become_draft_query(self):
        self.send("¿Puedes ayudarme? Cámara 2 del Estudio A sin imagen. Área Producción. Impide las pruebas. Prioridad alta")
        self.assertEqual(IncidentDraft.objects.count(), 1)
        self.assertIn("¿Confirmas", self.reply())

    def test_question_and_new_data_apply_data_and_answer_saved_snapshot(self):
        draft = self.seed()
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**draft.extracted_data, "location_context":"Estudio C", "unit":"Estudio C"},
            update_kind="correction", field_evidence={"location_context":"Estudio C"})
        self.send("¿Qué cámara te indiqué? Además, la ubicación correcta es Estudio C")
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data["location_context"], "Estudio C")
        self.assertIn("Cámara 3", self.reply())
        self.assertFalse(Incident.objects.exists())

    def test_unknown_area_is_not_inferred_silently_on_later_update(self):
        draft = self.seed(area=None)
        self.send("No sé el área")
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**draft.extracted_data, "area":"Producción", "priority":"critical"},
            update_kind="evolution", field_evidence={"priority":"crítica", "operational_impact":"ya no permite continuar la grabación"})
        self.send("Cambia prioridad a crítica: ya no permite continuar la grabación")
        draft.refresh_from_db()
        self.assertIsNone(draft.extracted_data["area"])
        self.assertIn("borrador", self.reply().lower())

    def test_unknown_area_is_not_filled_by_additional_context(self):
        draft = self.seed(area=None)
        self.send("No sé el área")
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**draft.extracted_data, "area":"Producción"},
            update_kind="additional", field_evidence={"description":"el cable sigue conectado"})
        self.send("La Cámara 3 del Estudio B tiene otro detalle: el cable sigue conectado")
        draft.refresh_from_db(); self.assertIsNone(draft.extracted_data["area"])

    def test_compatible_context_without_magic_continuity_word_is_kept(self):
        draft = self.seed(operational_impact=None)
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**draft.extracted_data, "operational_impact":"Interrumpe la grabación"},
            update_kind="new_incident", field_evidence={"operational_impact":"interrumpe la grabación"})
        self.send("La Cámara 3 del Estudio B interrumpe la grabación")
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data["operational_impact"], "Interrumpe la grabación")

    def test_description_append_supplies_boundary_when_previous_has_no_period(self):
        draft = self.seed(description="Problema reportado hace aproximadamente dos horas")
        detail = "Sospechamos que el cable de video falla, sin confirmar"
        self.ai.extract_incident.return_value = IncidentExtractionResult(**draft.extracted_data, update_kind="additional", field_evidence={"description":detail})
        self.send(detail)
        draft.refresh_from_db()
        self.assertIn("horas. Sospechamos", draft.extracted_data["description"])

    def test_corrected_age_and_equipment_are_persisted_in_the_real_incident(self):
        draft = self.seed()
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**draft.extracted_data, "equipment":"Cámara 2"},
            update_kind="correction", field_evidence={"equipment":"Cámara 2", "description":"hace dos horas"})
        self.send("El equipo es Cámara 2 y comenzó hace dos horas")
        self.send(CONFIRM)
        incident = Incident.objects.get()
        self.assertEqual(incident.equipment, "Cámara 2")
        self.assertIn("dos horas", incident.description)
        self.assertNotIn("una hora", incident.description)

    def test_actual_backup_usage_remains_actual_not_hypothetical(self):
        draft = self.seed()
        source = "Estamos usando una cámara de respaldo y continuamos parcialmente"
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**draft.extracted_data, "operational_impact":source},
            update_kind="evolution", field_evidence={"operational_impact":source})
        self.send(source)
        self.assertEqual(IncidentDraft.objects.get().extracted_data["operational_impact"], source)

    def test_diagnostic_hypothesis_does_not_hide_uncertain_equipment_choice(self):
        draft = self.seed()
        source = "No sé si es la Cámara 2 o la Cámara 4. Sospechamos que podría ser el cable"
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**draft.extracted_data, "equipment":"Cámara 2"},
            update_kind="correction", field_evidence={"equipment":"Cámara 2 o la Cámara 4", "description":"Sospechamos que podría ser el cable"})
        self.send(source)
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data["equipment"], "Cámara 3")
        self.assertIn("Sospechamos", draft.extracted_data["description"])
        self.assertFalse(Incident.objects.exists())

    def test_query_after_correction_reads_latest_values_without_mutating(self):
        draft = self.seed()
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**draft.extracted_data, "equipment":"Cámara 2"},
            update_kind="correction", field_evidence={"equipment":"Cámara 2"})
        self.send("El equipo es Cámara 2 y empezó hace dos horas")
        draft.refresh_from_db()
        before = (deepcopy(draft.extracted_data), draft.status, draft.last_question, draft.updated_at)
        self.ai.extract_incident.side_effect = AssertionError("Consulta sin IA")
        self.send("¿Qué cámara indiqué y cuánto tiempo lleva el problema?")
        self.assertIn("Cámara 2", self.reply())
        self.assertIn("dos horas", self.reply())
        draft.refresh_from_db()
        self.assertEqual((draft.extracted_data, draft.status, draft.last_question, draft.updated_at), before)


for index, question in enumerate(("¿Qué cámara indiqué anteriormente?", "¿Cuál es el equipo registrado?", "Recuérdame qué cámara falla"), 1):
    def query(self, value=question): self.assert_query(value, "Cámara 3")
    setattr(ConversationReal27Tests, f"test_equipment_query_{index}", query)

for index, phrase in enumerate(("Sospechamos que podría ser el cable", "Quizás sea el conector, no está confirmado", "Tal vez el monitor cause el problema, falta revisar", "Creemos que es la señal, todavía no sabemos"), 1):
    def modality(self, value=phrase): self.assert_modality(value, "description", "La falla es causada por el cable", value)
    setattr(ConversationReal27Tests, f"test_diagnostic_uncertainty_{index}", modality)


@override_settings(GEMINI_API_KEY="qa-placeholder")
class GeminiFailureLogTests(TestCase):
    def test_fast_attempt_exhaustion_distinguishes_elapsed_from_timeout_budget(self):
        provider = GeminiAIProvider()
        with patch.object(provider, "_request_with_deadline", side_effect=AITemporaryError("QA_SECRET")), \
             patch("incidents.ai.gemini_provider.time.perf_counter", return_value=3.86), \
             self.assertLogs("incidents.ai.gemini_provider", level="WARNING") as logs:
            with self.assertRaises(AITemporaryError): provider._create_interaction_with_retry("QA")
        output = " ".join(logs.output)
        self.assertIn("45.00", output)
        self.assertIn("intentos agotados", output)
        self.assertNotIn("QA_SECRET", output)
