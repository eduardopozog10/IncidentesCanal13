"""Confirmaciones basadas en el borrador guardado, sin APIs ni datos operativos."""
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.db import IntegrityError
from django.db.models.query import QuerySet
from django.test import TestCase, override_settings

from incidents.ai.gemini_provider import GeminiAIProvider
from incidents.ai.schemas import IncidentExtractionResult
from incidents.messaging.telegram_update_handler import TelegramUpdateHandler
from incidents.models import ChannelMessage, Incident, IncidentDraft, Technician, TechnicianChannel
from incidents.services.incident_processor import IncidentProcessor
from incidents.services.incoming_message_service import IncomingMessageService


BASE = dict(unit="Estudio 4", location_context="Estudio 4", area="Audio", equipment="Micrófono 2",
            asset_category="audio", failure_type="Cortes constantes de audio",
            description="El Micrófono 2 del Estudio 4 presenta cortes constantes de audio durante la transmisión en vivo.",
            operational_impact="Afecta la salida al aire", priority="medium")


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class CorrectionFeedbackTests(TestCase):
    def setUp(self):
        self.reporter = Technician.objects.create(full_name="QA conversacional")
        TechnicianChannel.objects.create(technician=self.reporter, provider="telegram", external_user_id="101", external_chat_id="101")
        self.ai = Mock()
        self.processor = IncidentProcessor(self.ai)
        self.service = IncomingMessageService(self.processor)
        self.number = 0

    def seed(self, **overrides):
        data = {**BASE, **overrides}
        missing = [name for name in self.processor.REQUIRED_FIELDS if not data.get(name)]
        return IncidentDraft.objects.create(technician=self.reporter, extracted_data=data, missing_fields=missing,
            status="collecting" if missing else "ready", last_question=self.processor.FALLBACK_QUESTIONS[missing[0]] if missing else self.processor._build_confirmation_reply(data))

    def send(self, text):
        self.number += 1
        return self.service.process_text(provider="telegram", external_user_id="101", external_chat_id="101",
            external_message_id=f"qa:{self.number}", text=text, raw_payload={})["processing"]

    def extracted(self, data, evidence, kind="correction", question=None):
        self.ai.extract_incident.return_value = IncidentExtractionResult(**data, field_evidence=evidence,
            update_kind=kind, clarification_question=question)

    def assert_saved_diff(self, previous, draft, result):
        draft.refresh_from_db()
        expected = {name: draft.extracted_data.get(name) for name in self.processor.CHANGE_LABELS
                    if previous.get(name) != draft.extracted_data.get(name)}
        self.assertIn("changes", result, result)
        self.assertEqual({item["field"]: item["value"] for item in result["changes"]}, expected)
        if draft.status == "ready":
            # El resumen contiene los valores guardados una sola vez, sin una
            # segunda lista de cambios delante. El diff sigue verificándose arriba.
            self.assertIn(self.processor._build_confirmation_reply(draft.extracted_data), result["reply"])
            self.assertEqual(sum(line.startswith("• ") for line in result["reply"].splitlines()), 8)
            self.assertIn("¿Confirmas la creación", result["reply"])
        else:
            acknowledgement = result["reply"].split("\n\n", 2)[1]
            self.assertEqual(len(acknowledgement.splitlines()), len(expected))
            for name in expected:
                self.assertIn(f"• {self.processor.CHANGE_LABELS[name]}:", acknowledgement)
        self.assertEqual(Incident.objects.count(), 0)

    def test_single_direct_correction_confirms_symptom_and_saved_description(self):
        draft = self.seed(operational_impact=None)
        previous = dict(draft.extracted_data)
        result = self.send("El síntoma es Sin audio")
        self.assert_saved_diff(previous, draft, result)
        self.assertIn("• Síntoma: Sin audio", result["reply"])
        self.assertIn("impacto", result["reply"].lower())
        self.assertNotIn("Antes de crear", result["reply"])
        self.ai.extract_incident.assert_not_called()

    def test_location_and_equipment_correction_lists_all_persisted_changes(self):
        draft = self.seed(area=None)
        previous = dict(draft.extracted_data)
        text = "Perdón, me equivoqué. Estamos en el Estudio 7 y el equipo afectado es el Micrófono 5."
        self.extracted({**previous, "location_context": "Estudio 7", "equipment": "Micrófono 5"},
                       {"location_context": "Estudio 7", "equipment": "Micrófono 5"})
        result = self.send(text)
        self.assert_saved_diff(previous, draft, result)
        self.assertEqual(draft.extracted_data["equipment"], "Micrófono 5")
        self.assertEqual(draft.extracted_data["location_context"], "Estudio 7")
        self.assertIn("• Ubicación: Estudio 7", result["reply"])
        self.assertIn("• Equipo: Micrófono 5", result["reply"])
        self.assertIn("área", result["reply"].lower())

    def test_priority_evolution_updates_symptom_impact_and_description_together(self):
        draft = self.seed()
        previous = dict(draft.extracted_data)
        text = "También corrijo la prioridad: es crítica porque ahora perdimos completamente el audio de la transmisión y no tenemos respaldo disponible."
        new = {**previous, "priority": "critical", "failure_type": "Pérdida total de audio",
               "description": "La transmisión perdió completamente el audio.",
               "operational_impact": "Transmisión sin audio y sin respaldo disponible"}
        self.extracted(new, {"priority": "crítica", "failure_type": "perdimos completamente el audio",
            "description": "perdimos completamente el audio de la transmisión", "operational_impact": "no tenemos respaldo disponible"}, "evolution")
        result = self.send(text)
        self.assert_saved_diff(previous, draft, result)
        self.assertEqual({item["field"] for item in result["changes"]}, {"priority", "failure_type", "description", "operational_impact"})
        self.assertIn("• Prioridad: Crítica", result["reply"])
        self.assertNotIn("cortes constantes", result["reply"].lower())
        self.assertIn("Antes de crear", result["reply"])
        self.assertEqual(draft.last_question, result["reply"])
        self.assertEqual(draft.status, "ready")

    def test_real_microphone_5_studio_7_critical_case_while_waiting_area(self):
        draft = self.seed(area=None)
        text = "Perdón, me equivoqué. No estamos en el Estudio 4, estamos en el Estudio 7, y el equipo afectado es el Micrófono 5."
        self.extracted({**draft.extracted_data, "location_context": "Estudio 7", "equipment": "Micrófono 5"},
                       {"location_context": "Estudio 7", "equipment": "Micrófono 5"})
        self.send(text)
        draft.refresh_from_db()
        changed = {**draft.extracted_data, "priority": "critical", "failure_type": "Pérdida total de audio",
                   "description": "El Micrófono 5 perdió completamente el audio de la transmisión.",
                   "operational_impact": "Transmisión sin audio y sin respaldo disponible"}
        self.extracted(changed, {"priority": "crítica", "failure_type": "perdimos completamente el audio",
            "description": "perdimos completamente el audio de la transmisión", "operational_impact": "no tenemos respaldo disponible"}, "evolution")
        result = self.send("También corrijo la prioridad: es crítica porque ahora perdimos completamente el audio de la transmisión y no tenemos respaldo disponible.")
        self.assertIn("área", result["reply"].lower())
        self.assertNotIn("Antes de crear", result["reply"])
        self.ai.extract_incident.reset_mock()
        final = self.send("Audio")
        self.ai.extract_incident.assert_not_called()
        draft.refresh_from_db()
        for name, value in dict(area="Audio", location_context="Estudio 7", equipment="Micrófono 5", priority="critical",
                                failure_type="Pérdida total de audio", operational_impact=changed["operational_impact"]).items():
            self.assertEqual(draft.extracted_data[name], value)
        self.assertIn("Antes de crear", final["reply"])
        self.assertNotIn("cortes constantes", final["reply"].lower())
        self.assertEqual(Incident.objects.count(), 0)

    def test_same_family_equipment_retains_symptom_and_updates_every_reference(self):
        draft = self.seed(equipment="Cámara 3", asset_category="video_cameras", failure_type="Imagen en negro en la Cámara 3",
            description="La Cámara 3 no entrega imagen. La Cámara 3 está en Estudio 4.", operational_impact="No podemos usar la Cámara 3")
        previous = dict(draft.extracted_data)
        result = self.send("El equipo es Cámara 1")
        self.assert_saved_diff(previous, draft, result)
        self.assertEqual(draft.extracted_data["failure_type"], "Imagen en negro en la Cámara 1")
        self.assertNotIn("Cámara 3", draft.extracted_data["description"])
        self.assertNotIn("Cámara 3", draft.extracted_data["operational_impact"])
        self.assertEqual(draft.status, "ready")

    def test_new_equipment_family_clears_incompatible_details_and_explains_it(self):
        draft = self.seed(equipment="Cámara 3", asset_category="video_cameras", failure_type="Imagen en negro")
        previous = dict(draft.extracted_data)
        result = self.send("El equipo es Consola de audio")
        self.assert_saved_diff(previous, draft, result)
        self.assertEqual(draft.extracted_data["asset_category"], "audio")
        for field in ("failure_type", "description", "operational_impact"):
            self.assertIsNone(draft.extracted_data[field])
        self.assertIn("• Síntoma: Por confirmar", result["reply"])
        self.assertIn("• Familia:", result["reply"])
        self.assertNotIn("Antes de crear", result["reply"])
        self.assertEqual(draft.status, "collecting")

    def test_correction_during_final_confirmation_requires_another_explicit_yes(self):
        draft = self.seed()
        result = self.send("La ubicación es Estudio 7")
        self.assertIn("• Ubicación/contexto: Estudio 7", result["reply"])
        self.assertIn("¿Confirmas la creación", result["reply"])
        self.assertFalse(result["incident_created"])
        self.assertEqual(Incident.objects.count(), 0)
        created = self.send("confirmo")
        self.assertTrue(created["incident_created"])
        self.assertEqual(Incident.objects.get().location_context, "Estudio 7")

    def test_noop_does_not_invent_an_update(self):
        draft = self.seed()
        previous = dict(draft.extracted_data)
        result = self.send("La ubicación es Estudio 4")
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data, previous)
        self.assertEqual(result["changes"], [])
        self.assertIn("ya estaba registrado", result["reply"])
        self.assertNotIn("actualicé", result["reply"])

    def test_ambiguous_correction_preserves_data_and_asks_for_correct_value(self):
        draft = self.seed(area=None)
        previous = dict(draft.extracted_data)
        self.extracted({**previous, "location_context": "Estudio 7"}, {}, "ambiguous",
                       "¿La ubicación correcta es Estudio 7 o Estudio 8?")
        result = self.send("Quizá estamos en el Estudio 7 o en el Estudio 8, no estoy seguro.")
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data, previous)
        self.assertIn("¿Cuál es el dato correcto y su nuevo valor?", result["reply"])
        self.assertIn("Mantengo los datos anteriores", result["reply"])
        self.assertNotIn("actualicé", result["reply"])
        self.assertEqual(draft.status, "collecting")

    def test_additional_information_keeps_existing_values_and_appends_supported_detail(self):
        draft = self.seed()
        previous = dict(draft.extracted_data)
        text = "Además, el cable está conectado a la entrada 2."
        self.extracted({**previous, "failure_type": "Sin audio", "operational_impact": "Emisión detenida"},
                       {"description": "el cable está conectado a la entrada 2"}, "additional")
        result = self.send(text)
        self.assert_saved_diff(previous, draft, result)
        self.assertEqual(draft.extracted_data["failure_type"], previous["failure_type"])
        self.assertEqual(draft.extracted_data["operational_impact"], previous["operational_impact"])
        self.assertTrue(draft.extracted_data["description"].startswith(previous["description"]))
        self.assertIn("entrada 2", draft.extracted_data["description"])
        self.assertEqual({item["field"] for item in result["changes"]}, {"description"})

    def test_priority_explanation_without_new_operational_facts_only_changes_priority(self):
        draft = self.seed()
        previous = dict(draft.extracted_data)
        result = self.send("También corrijo la prioridad: es crítica porque es importante.")
        self.assert_saved_diff(previous, draft, result)
        self.assertEqual({item["field"] for item in result["changes"]}, {"priority"})
        for name in ("failure_type", "description", "operational_impact", "equipment", "area"):
            self.assertEqual(draft.extracted_data[name], previous[name])
        self.ai.extract_incident.assert_not_called()

    def test_natural_rectification_without_correction_keyword_uses_context(self):
        draft = self.seed()
        previous = dict(draft.extracted_data)
        self.extracted({**previous, "failure_type": "Sin audio", "description": "El Micrófono 2 dejó de entregar audio."},
            {"failure_type": "dejó de entregar audio", "description": "dejó de entregar audio"}, "evolution")
        result = self.send("Ahora dejó de entregar audio completamente.")
        self.assert_saved_diff(previous, draft, result)
        self.assertEqual(draft.extracted_data["failure_type"], "Sin audio")
        self.assertEqual(draft.extracted_data["operational_impact"], previous["operational_impact"])

    def test_unmentioned_ai_changes_do_not_overwrite_valid_fields(self):
        draft = self.seed()
        previous = dict(draft.extracted_data)
        self.extracted({**previous, "location_context": "Estudio 7", "equipment": "Cámara 99", "failure_type": "Incendio"},
                       {"location_context": "Estudio 7"})
        result = self.send("Me equivoqué, estamos en el Estudio 7.")
        self.assert_saved_diff(previous, draft, result)
        self.assertEqual(draft.extracted_data["equipment"], previous["equipment"])
        self.assertEqual(draft.extracted_data["failure_type"], previous["failure_type"])
        self.assertNotIn("Cámara 99", result["reply"])

    def test_evolution_is_not_swallowed_by_pending_priority_question(self):
        draft = self.seed(priority=None)
        previous = dict(draft.extracted_data)
        self.extracted({**previous, "failure_type": "Sin audio", "description": "El Micrófono 2 dejó de entregar audio."},
            {"failure_type": "dejó de entregar audio", "description": "dejó de entregar audio"}, "evolution")
        result = self.send("Ahora dejó de entregar audio completamente.")
        self.assert_saved_diff(previous, draft, result)
        self.assertEqual(draft.extracted_data["failure_type"], "Sin audio")
        self.assertIsNone(draft.extracted_data["priority"])
        self.assertIn("prioridad", result["reply"].lower())

    def test_ai_normalized_noop_reports_existing_value_without_side_effects(self):
        draft = self.seed()
        previous = dict(draft.extracted_data)
        self.extracted({**previous, "equipment": "el Micrófono 2"}, {"equipment": "Micrófono 2"})
        result = self.send("Me equivoqué, era el Micrófono 2.")
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data, previous)
        self.assertEqual(result["changes"], [])
        self.assertIn("ya estaba registrado", result["reply"])

    def test_another_incident_does_not_silently_replace_active_draft(self):
        draft = self.seed()
        previous = dict(draft.extracted_data)
        self.extracted({**previous, "equipment": "Cámara 5", "failure_type": "Sin imagen"},
                       {"equipment": "Cámara 5", "failure_type": "sin imagen"}, "new_incident")
        result = self.send("Hay otra incidencia: la Cámara 5 está sin imagen en Estudio 9.")
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data, previous)
        self.assertIn("otra incidencia", result["reply"])
        self.assertEqual(IncidentDraft.objects.count(), 1)
        self.assertEqual(Incident.objects.count(), 0)

    def test_failed_result_commit_does_not_leave_claimed_changes(self):
        draft = self.seed()
        previous = dict(draft.extracted_data)
        original = QuerySet.update

        def reject_cache(queryset, **kwargs):
            if kwargs.get("processing_result", {}).get("changes"):
                raise IntegrityError("Fallo QA al confirmar resultado")
            return original(queryset, **kwargs)

        with patch.object(QuerySet, "update", new=reject_cache):
            with self.assertRaises(IntegrityError):
                self.send("La ubicación es Estudio 7")
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data, previous)
        self.assertNotIn("reply", ChannelMessage.objects.get(direction="inbound").processing_result)
        self.assertNotIn("changes", ChannelMessage.objects.get(direction="inbound").processing_result)

    def test_polling_replay_does_not_duplicate_feedback_or_authorize_an_old_summary(self):
        draft = self.seed()
        messaging = Mock()
        messaging.send_text.side_effect = lambda *args: {"result": {"message_id": 900 + messaging.send_text.call_count}}
        handler = TelegramUpdateHandler(messaging_provider=messaging,
            incoming_service=IncomingMessageService(IncidentProcessor(self.ai, require_delivered_summary=True)))

        def update(number, text):
            return {"update_id": number, "message": {"message_id": number, "from": {"id": 101},
                    "chat": {"id": 101, "type": "private"}, "text": text}}

        payload = update(1, "La ubicación es Estudio 7")
        handler.handle(payload)
        handler.handle(payload)
        handler.recover_pending()
        self.assertEqual(messaging.send_text.call_count, 1)
        draft.refresh_from_db()
        self.assertEqual(draft.last_question, ChannelMessage.objects.get(direction="outbound").content)
        self.assertEqual(ChannelMessage.objects.get(direction="inbound").processing_result["reply"], draft.last_question)
        self.assertEqual(Incident.objects.count(), 0)
        self.assertTrue(handler.handle(update(2, "confirmo"))["incident_created"])
        handler.handle(update(2, "confirmo"))
        self.assertEqual(Incident.objects.count(), 1)


@override_settings(GEMINI_API_KEY="qa-placeholder")
class GeminiCorrectionEvidenceTests(TestCase):
    def test_provider_rejects_non_literal_or_historical_evidence(self):
        provider = GeminiAIProvider()
        response = {"intent": "incident_followup", **BASE, "update_kind": "correction",
                    "field_evidence": {"location_context": "Estudio 7", "equipment": "Micrófono 2"}}
        with patch.object(provider, "_create_interaction_with_retry", return_value=SimpleNamespace(output_text=json.dumps(response))):
            result = provider.extract_incident("Estamos en el Estudio 7.", previous_data=BASE)
        self.assertEqual(result.field_evidence, {"location_context": "Estudio 7"})

    def test_provider_validates_evidence_structure(self):
        provider = GeminiAIProvider()
        for evidence in ([], {"equipment": ["Micrófono 5"]}):
            with self.subTest(evidence=evidence), patch.object(provider, "_create_interaction_with_retry",
                    return_value=SimpleNamespace(output_text=json.dumps({"intent": "incident_followup", **BASE, "field_evidence": evidence}))):
                with self.assertRaises(ValueError):
                    provider.extract_incident("QA")
