"""Confirmación y mensajes honestos: PostgreSQL QA, Gemini y Telegram simulados."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import smtplib
from unittest.mock import patch

from django.db import IntegrityError, close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings

from incidents.ai.schemas import IncidentExtractionResult
from incidents.messaging.telegram_provider import TelegramDeliveryUncertainError, TelegramRejectedError
from incidents.messaging.telegram_update_handler import TelegramUpdateHandler
from incidents.models import ChannelMessage, Incident, IncidentDraft, IncidentChange, IncidentNotification
from incidents.services.incident_notification_service import send_incident_created_email
from incidents.test_qa_extreme import handler_fixture
from incidents.test_pilot_recovery import update
from incidents.test_user_management import SETTINGS


DATA = dict(area="Producción", unit="Estudio A", location_context="Estudio A", equipment="Cámara 2",
    asset_category="video_cameras", failure_type="Imagen a negro intermitente y tirones",
    description="La Cámara 2 del Estudio A se va a negro y presenta tirones durante las pruebas.",
    operational_impact="Impide continuar las pruebas antes de salir al aire", priority="high")
INITIAL = "La cámara 2 del estudio A está guateando, la imagen se fue a negro un par de veces y ahora anda a tirones. Estamos haciendo pruebas antes de salir al aire y así no podemos seguir. Yo lo dejaría en prioridad alta."
CONFIRM = "Sí, confirmo la creación del incidente."
FALSE_CLAIM = "Entendido. He registrado la confirmación para el reporte del incidente. Los datos ya se encuentran listos para ser procesados por el equipo técnico."
AFFIRMATIVES = ["Sí.", "Si, confirmo.", CONFIRM, "Confirmo.", "Correcto, créalo.",
    "Está todo bien, regístralo.", "Dale, créalo.", "Ya, dale.", "Está correcto.",
    "Todo bien, puedes registrarlo.", "Procede con el reporte.", "Confirmado.", "Perfecto, envíalo.", "Registra la incidencia.",
    "  SÍ,   CONFIRMO LA CREACIÓN DEL INCIDENTE!! ", "si confirmo la creacion del incidente", "DÁLE, CRÉALO!!!",
    "Si, confimo", "Confrimo", "Está todo bien, registralo", "Ya po, dale", "Dale nomás, créalo"]
NON_CONFIRMATIONS = ["No confirmo", "No lo registres todavía", "Sí, no, espera", "Creo que sí, pero déjame revisar",
    "Sí, aunque hay algo malo en el resumen", "¿Sí?", "Confirmo?", "Dijo que sí", "síntoma confirmado",
    "No, está incorrecta la prioridad", "Todavía no, necesito cambiar el impacto", "Está bien, excepto la descripción",
    "Sí, corrige primero el equipo", "Todo lo anterior está mal. Sí, confirmo la creación del incidente."]


class ConfirmationFixture:
    def setUp(self):
        self.reporter, self.ai, self.messaging, self.handler = handler_fixture()
        self.ai.extract_incident.return_value = IncidentExtractionResult(**deepcopy(DATA))
        self.number = 0

    def send(self, text):
        self.number += 1
        return self.handler.handle(update(self.number, text=text))

    def ready(self):
        self.send(INITIAL)
        draft = IncidentDraft.objects.get(technician=self.reporter)
        self.assertEqual(draft.status, "ready")
        self.assertTrue(ChannelMessage.objects.filter(draft=draft, status="sent", content=draft.last_question).exists())
        return draft

    def last_reply(self):
        return ChannelMessage.objects.filter(direction="outbound", reply_purpose="reply").order_by("pk").last().content


@override_settings(DEBUG=False, TELEGRAM_AUTO_REGISTER=False, PILOT_MESSAGE_RETRY_SECONDS=0, **SETTINGS)
class NaturalConfirmationTests(ConfirmationFixture, TestCase):
    def test_real_transcript_creates_persisted_incident_without_ai_for_confirmation(self):
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**DATA, "area":None}, clarification_question="¿A qué área operacional o técnica corresponde este incidente?")
        self.send(INITIAL)
        self.ai.extract_incident.return_value = IncidentExtractionResult(**DATA)
        self.send("Corresponde al área de Producción. Es la cámara 2 del estudio A que mencioné antes.")
        calls = self.ai.extract_incident.call_count
        self.ai.extract_incident.return_value = IncidentExtractionResult(intent="conversation", conversation_reply=FALSE_CLAIM)
        result = self.send(CONFIRM)
        self.assertTrue(result.get("incident_created"), result)
        incident = Incident.objects.get()
        self.assertIn(incident.code, self.last_reply())
        self.assertEqual(self.ai.extract_incident.call_count, calls)
        self.assertEqual(incident.area, "Producción")
        self.assertEqual(IncidentDraft.objects.get().incident_id, incident.pk)
        self.assertEqual(IncidentNotification.objects.get().incident_id, incident.pk)

    def assert_natural_affirmative(self, text):
        draft = self.ready()
        self.ai.extract_incident.side_effect = AssertionError("Una confirmación inequívoca no requiere IA")
        result = self.send(text)
        self.assertTrue(result.get("incident_created"), (text, result))
        incident = Incident.objects.get()
        self.assertIn(incident.code, self.last_reply())
        self.assertNotIn(FALSE_CLAIM, self.last_reply())
        draft.refresh_from_db(); self.assertEqual(draft.status, "completed")
        self.assertFalse(IncidentChange.objects.exists())
        repeated = self.handler.handle(update(self.number, text=text))
        self.assertEqual(repeated["action"], "duplicate_message")
        self.send(text)
        self.assertEqual(Incident.objects.count(), 1)
        self.assertEqual(IncidentNotification.objects.count(), 1)

    def assert_non_confirmation(self, text):
        draft = self.ready()
        self.ai.extract_incident.return_value = IncidentExtractionResult(intent="conversation", conversation_reply="Ya registré el incidente SC-999999 y envié la notificación.")
        self.send(text)
        self.assertFalse(Incident.objects.exists())
        self.assertNotIn("SC-999999", self.last_reply())
        draft.refresh_from_db()
        self.assertIsNone(draft.incident_id)

    def test_confirmation_after_hold_presents_summary_before_creating(self):
        self.ready()
        self.send("Sí, no, espera")
        self.send(CONFIRM)
        self.assertFalse(Incident.objects.exists())
        self.assertIn("¿Confirmas", self.last_reply())
        self.send(CONFIRM)
        self.assertEqual(Incident.objects.count(), 1)

    def test_failed_corrected_summary_cannot_authorize_creation_from_old_summary(self):
        self.ready()
        original = self.messaging.send_text.side_effect
        self.messaging.send_text.side_effect = TelegramRejectedError()
        self.send("Sí, pero cambia la prioridad a crítica")
        self.messaging.send_text.side_effect = original
        self.send(CONFIRM)
        self.assertFalse(Incident.objects.exists())
        self.handler.recover_pending()
        self.assertFalse(Incident.objects.exists(), "Una confirmación anterior a la entrega del resumen corregido no puede autorizarlo")
        self.assertIn("Crítica", self.last_reply())
        self.assertIn("¿Confirmas", self.last_reply())
        self.send(CONFIRM)
        self.assertEqual(Incident.objects.get().priority, "critical")

    def test_notification_failure_keeps_incident_without_claiming_mail_sent(self):
        self.ready()
        self.send(CONFIRM)
        incident = Incident.objects.get()
        with patch("incidents.services.incident_notification_service.send_mail", side_effect=smtplib.SMTPConnectError(421, b"QA")):
            self.assertFalse(send_incident_created_email(incident.pk))
        self.assertEqual(IncidentNotification.objects.get().status, "failed")
        self.assertIn(incident.code, self.last_reply())
        self.assertNotIn("notificación", self.last_reply().lower())
        self.send(CONFIRM)
        self.assertEqual(Incident.objects.count(), 1)
        self.assertEqual(IncidentNotification.objects.count(), 1)

    def test_correction_with_affirmative_requires_new_summary_and_new_confirmation(self):
        draft = self.ready()
        self.send("Sí, pero cambia la prioridad a crítica")
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data["priority"], "critical")
        self.assertFalse(Incident.objects.exists())
        self.assertIn("Crítica", self.last_reply())
        self.assertIn("¿Confirmas", self.last_reply())
        self.send(CONFIRM)
        self.assertEqual(Incident.objects.get().priority, "critical")

    def test_location_and_equipment_corrections_win_over_confirming_words(self):
        draft = self.ready()
        for text, change in [("Confirmo, aunque la ubicación es estudio B", {"location_context":"Estudio B", "unit":"Estudio B"}),
                             ("Dale, pero no es cámara 2, es cámara 3", {"equipment":"Cámara 3"})]:
            draft.refresh_from_db()
            self.ai.extract_incident.return_value = IncidentExtractionResult(**{**draft.extracted_data, **change},
                update_kind="correction", field_evidence={key:value for key,value in change.items() if key != "unit"})
            self.send(text)
            self.assertFalse(Incident.objects.exists())
            draft.refresh_from_db()
            for name,value in change.items(): self.assertEqual(draft.extracted_data[name], value)
        self.send("Correcto, créalo")
        incident = Incident.objects.get()
        self.assertEqual((incident.location_context, incident.equipment), ("Estudio B", "Cámara 3"))

    def test_no_draft_or_incomplete_draft_cannot_be_confirmed(self):
        self.ai.extract_incident.return_value = IncidentExtractionResult(intent="conversation", conversation_reply=FALSE_CLAIM)
        self.send(CONFIRM)
        self.assertFalse(Incident.objects.exists())
        self.assertNotIn(FALSE_CLAIM, self.last_reply())
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**DATA, "area":None})
        self.send(INITIAL)
        draft = IncidentDraft.objects.get()
        calls = self.ai.extract_incident.call_count
        self.ai.extract_incident.return_value = IncidentExtractionResult(intent="conversation", conversation_reply=FALSE_CLAIM)
        self.send(CONFIRM)
        self.assertFalse(Incident.objects.exists())
        self.assertEqual(self.ai.extract_incident.call_count, calls)
        self.assertIn("área", self.last_reply().lower())
        draft.refresh_from_db(); self.assertEqual(draft.status, "collecting")

    def test_undelivered_summary_is_presented_before_accepting_confirmation(self):
        draft = IncidentDraft.objects.create(technician=self.reporter, status="ready", extracted_data=DATA,
                                            last_question=self.handler.incoming_service.incident_processor._build_confirmation_reply(DATA))
        self.send(CONFIRM)
        self.assertFalse(Incident.objects.exists())
        self.assertIn("¿Confirmas", self.last_reply())
        self.send(CONFIRM)
        self.assertEqual(Incident.objects.count(), 1)

    def test_free_ai_prose_cannot_claim_persistence_or_notification(self):
        self.ready()
        self.ai.extract_incident.return_value = IncidentExtractionResult(intent="conversation", conversation_reply="Ya quedó creado SC-999999. Envié la notificación y he procesado el reporte.")
        self.send("¿Qué va a pasar después con el reporte?")
        self.assertFalse(Incident.objects.exists())
        self.assertNotIn("SC-999999", self.last_reply())
        self.assertNotIn("Envié la notificación", self.last_reply())
        self.assertIn("pendiente", self.last_reply().lower())

    def test_free_ai_question_cannot_claim_unperformed_actions(self):
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**DATA, "area":None},
            clarification_question="Ya registré SC-999999. ¿A qué área pertenece?")
        self.send(INITIAL)
        self.assertNotIn("SC-999999", self.last_reply())
        self.assertFalse(Incident.objects.exists())

    def test_initial_clarification_acknowledges_only_saved_fields(self):
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**DATA, "area":None}, clarification_question="¿A qué área corresponde?")
        self.send(INITIAL)
        reply = self.last_reply()
        self.assertIn("Cámara 2", reply)
        self.assertIn("Estudio A", reply)
        self.assertIn("área", reply.lower())
        self.assertNotIn("Producción", reply.split("¿")[0])
        self.assertLess(len(reply), 550)

    def test_final_summary_does_not_repeat_changed_field_values_in_an_acknowledgement(self):
        self.ready()
        self.send("Sí, pero cambia la prioridad a crítica")
        reply = self.last_reply()
        self.assertEqual(reply.count("• Prioridad:"), 1)
        self.assertIn("Crítica", reply)
        self.assertIn("¿Confirmas", reply)
        self.assertFalse(Incident.objects.exists())

    def test_legacy_cached_conversation_cannot_send_old_false_success(self):
        draft = self.ready()
        payload = update(10, text=CONFIRM)
        incoming = ChannelMessage.objects.create(provider="telegram", external_message_id="101:10", external_sender_id="101", external_chat_id="101",
            technician=self.reporter, channel_account=self.reporter.channels.get(), direction="inbound", draft=draft,
            content=CONFIRM, raw_payload=payload, received_via_polling=True, status="processed",
            processing_result={"action":"conversation", "incident_created":False, "draft_id":draft.pk, "reply":FALSE_CLAIM})
        ChannelMessage.objects.create(provider="telegram", external_sender_id="bot", external_chat_id="101", technician=self.reporter,
            direction="outbound", in_reply_to=incoming, reply_purpose="reply", content=FALSE_CLAIM, status="received")
        self.handler.handle(payload)
        self.assertFalse(Incident.objects.exists())
        self.assertNotIn(FALSE_CLAIM, self.last_reply())


for index, text in enumerate(AFFIRMATIVES, 1):
    def affirmative(self, value=text): self.assert_natural_affirmative(value)
    setattr(NaturalConfirmationTests, f"test_affirmative_{index:02d}", affirmative)

for index, text in enumerate(NON_CONFIRMATIONS, 1):
    def non_confirmation(self, value=text): self.assert_non_confirmation(value)
    setattr(NaturalConfirmationTests, f"test_non_confirmation_{index:02d}", non_confirmation)


@override_settings(DEBUG=False, TELEGRAM_AUTO_REGISTER=False, PILOT_MESSAGE_RETRY_SECONDS=0, **SETTINGS)
class ConfirmationCommitTests(ConfirmationFixture, TransactionTestCase):
    def test_success_is_sent_only_after_commit_and_contains_real_code(self):
        self.ready()
        original = self.messaging.send_text.side_effect
        def send(recipient, text):
            self.assertFalse(connection.in_atomic_block)
            incident = Incident.objects.get()
            self.assertIn(incident.code, text)
            self.assertTrue(IncidentNotification.objects.filter(incident=incident).exists())
            return original(recipient, text)
        self.messaging.send_text.side_effect = send
        self.send(CONFIRM)
        self.assertEqual(Incident.objects.count(), 1)

    def test_creation_failure_is_honest_and_retry_creates_once(self):
        self.ready()
        with patch("incidents.services.incident_processor.Incident.objects.create", side_effect=IntegrityError("QA fallo de persistencia")):
            result = self.send(CONFIRM)
        self.assertFalse(Incident.objects.exists())
        self.assertEqual(result["action"], "processing_pending")
        notice = ChannelMessage.objects.filter(direction="outbound", reply_purpose="notice").last()
        self.assertIn("registro", notice.content.lower())
        self.assertIn("confirm", notice.content.lower())
        self.handler.recover_pending()
        self.assertEqual(Incident.objects.count(), 1)
        self.assertEqual(IncidentNotification.objects.count(), 1)

    def test_restart_after_commit_replays_persisted_reply_without_creating_again(self):
        self.ready()
        with patch.object(self.handler, "_deliver_reply", side_effect=SystemExit):
            with self.assertRaises(SystemExit): self.send(CONFIRM)
        self.assertEqual(Incident.objects.count(), 1)
        restarted = TelegramUpdateHandler(messaging_provider=self.messaging, incoming_service=self.handler.incoming_service)
        restarted.recover_pending()
        self.assertEqual(Incident.objects.count(), 1)
        self.assertIn(Incident.objects.get().code, self.last_reply())

    def test_concurrent_natural_confirmations_create_only_one_incident(self):
        self.ready()
        def confirm(pair):
            close_old_connections()
            try: return self.handler.handle(update(pair[0], text=pair[1]))
            finally: close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(confirm, (2, CONFIRM))
            second = pool.submit(confirm, (3, "Dale, créalo"))
            first.result(timeout=12); second.result(timeout=12)
        self.handler.recover_pending()
        self.assertEqual(Incident.objects.count(), 1)
        self.assertEqual(IncidentNotification.objects.count(), 1)

    def test_lost_telegram_ack_after_commit_does_not_undo_or_duplicate_incident(self):
        self.ready()
        self.messaging.send_text.side_effect = TelegramDeliveryUncertainError("QA acuse perdido")
        result = self.send(CONFIRM)
        self.assertEqual(result["action"], "reply_failed")
        self.assertEqual(Incident.objects.count(), 1)
        self.handler.recover_pending()
        self.assertEqual(Incident.objects.count(), 1)
