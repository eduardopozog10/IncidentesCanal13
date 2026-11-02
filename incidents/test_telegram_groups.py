"""Agrupación durable: transporte simulado, PostgreSQL de tests, reloj controlado."""
from concurrent.futures import ThreadPoolExecutor
import io
from datetime import timedelta
from threading import Event, Barrier
from unittest.mock import patch, Mock

from django.db import close_old_connections, connection
from django.core.management import call_command
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from incidents.ai.schemas import IncidentExtractionResult
from incidents.ai.base import AITemporaryError
from incidents.messaging.telegram_provider import TelegramDeliveryUncertainError
from incidents.models import ChannelMessage, Incident, IncidentDraft, IncidentNotification
from incidents.messaging.telegram_update_handler import TelegramUpdateHandler
from incidents.services.telegram_message_groups import TelegramMessageGroups, TelegramGroupRunner
from incidents.management.commands.run_telegram_bot import Command
from incidents.test_natural_confirmation import DATA, CONFIRM
from incidents.test_pilot_recovery import update
from incidents.test_qa_extreme import handler_fixture
from incidents.test_user_management import SETTINGS


class GroupFixture:
    def setUp(self):
        self.reporter, self.ai, self.transport, self.handler = handler_fixture()
        self.ai.extract_incident.return_value = IncidentExtractionResult(**DATA)
        self.groups = TelegramMessageGroups(self.handler)
        self.clock = timezone.now()
        self.number = 0

    def queue(self, text, delta=0, number=None):
        self.number = number if number is not None else self.number + 1
        with patch("django.utils.timezone.now", return_value=self.clock + timedelta(seconds=delta)):
            return self.handler.queue_update(update(self.number, text=text))

    def flush(self, delta=10):
        with patch("django.utils.timezone.now", return_value=self.clock + timedelta(seconds=delta)):
            return self.groups.process_reporter(self.reporter.pk)

    def last_reply(self):
        return ChannelMessage.objects.filter(direction="outbound", reply_purpose="reply").order_by("pk").last().content


@override_settings(TELEGRAM_GROUP_WINDOW_SECONDS=2.5, PILOT_MESSAGE_RETRY_SECONDS=0, DEBUG=False, TELEGRAM_AUTO_REGISTER=False, **SETTINGS)
class TelegramGroupTests(GroupFixture, TestCase):
    def test_four_messages_are_kept_individually_and_extracted_once(self):
        texts = ["Tengo un problema con una cámara", "Es la Cámara 2 del Estudio A", "La imagen tiene tirones", "Área Producción. Impide las pruebas. Prioridad alta"]
        for number, text in enumerate(texts): self.queue(text, delta=number / 4)
        self.assertEqual(ChannelMessage.objects.filter(direction="inbound").count(), 4)
        self.ai.extract_incident.assert_not_called()
        self.transport.send_text.assert_not_called()
        self.assertEqual(self.groups.ready_reporters(now=self.clock + timedelta(seconds=2)), [])
        self.flush()
        self.ai.extract_incident.assert_called_once()
        prompt = self.ai.extract_incident.call_args.kwargs["message"]
        self.assertEqual([prompt.index(text) for text in texts], sorted(prompt.index(text) for text in texts))
        self.assertEqual(list(ChannelMessage.objects.filter(direction="inbound").order_by("pk").values_list("content", flat=True)), texts)
        self.assertEqual(self.transport.send_text.call_count, 1)
        self.assertEqual(IncidentDraft.objects.get().extracted_data["equipment"], "Cámara 2")
        self.assertFalse(Incident.objects.exists())

    def test_single_message_flushes_when_no_more_messages_arrive(self):
        self.queue("En Producción, Cámara 2 Estudio A sin imagen. No permite pruebas. Prioridad alta")
        self.assertIn(self.reporter.pk, self.groups.ready_reporters(now=self.clock + timedelta(seconds=3)))
        self.flush(); self.assertEqual(self.transport.send_text.call_count, 1)

    def test_ten_fast_messages_are_one_response(self):
        for i in range(10): self.queue(f"Dato adicional {i}: Cámara 2 Estudio A. Prioridad alta", delta=i / 10)
        self.flush()
        self.assertEqual(self.ai.extract_incident.call_count, 1)
        self.assertEqual(self.transport.send_text.call_count, 1)
        self.assertEqual(ChannelMessage.objects.filter(direction="inbound", status="processed").count(), 10)

    def test_duplicate_update_is_not_a_second_group_member(self):
        self.queue("Cámara 2, Estudio A. Prioridad alta", number=1)
        self.queue("Otro contenido no debe reemplazarlo", number=1)
        self.queue("Área Producción", number=2)
        self.flush(); self.flush()
        self.assertEqual(ChannelMessage.objects.filter(direction="inbound").count(), 2)
        self.assertEqual(self.transport.send_text.call_count, 1)
        self.assertNotIn("Otro contenido", self.ai.extract_incident.call_args.kwargs["message"])

    def test_replay_of_a_follower_does_not_send_an_extra_reply(self):
        self.queue("Cámara 2, Estudio A. Prioridad alta")
        self.queue("Área Producción")
        self.flush()
        self.handler.handle(update(2, text="Área Producción"))
        self.assertEqual(self.transport.send_text.call_count, 1)

    def test_confirmation_in_same_group_as_new_data_cannot_create(self):
        self.queue("Cámara 2 Estudio A. Producción. No hay imagen. Impide pruebas. Prioridad alta")
        self.queue(CONFIRM)
        self.flush()
        self.assertFalse(Incident.objects.exists())
        self.assertIn("¿Confirmas", self.last_reply())
        self.queue(CONFIRM, delta=12)
        self.flush(15)
        self.assertEqual(Incident.objects.count(), 1)
        self.assertEqual(IncidentNotification.objects.count(), 1)

    def test_affirmative_and_correction_in_group_require_new_confirmation(self):
        self.queue("Cámara 2 Estudio A. Producción. Impide pruebas. Prioridad alta")
        self.flush()
        self.queue(CONFIRM, delta=12)
        self.queue("Pero cambia prioridad a crítica", delta=12.2)
        draft = IncidentDraft.objects.get()
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**{key:value for key,value in draft.extracted_data.items() if not key.startswith("_")}, "priority":"critical"}, update_kind="correction", field_evidence={"priority":"crítica"})
        self.flush(16)
        self.assertFalse(Incident.objects.exists())
        self.assertEqual(IncidentDraft.objects.get().extracted_data["priority"], "critical")

    def test_later_priority_declaration_supersedes_earlier_message(self):
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**DATA, "priority":"critical"})
        self.queue("Cámara 2 Estudio A sin imagen, Producción. Impide pruebas. Prioridad alta")
        self.queue("Corrijo la prioridad: crítica")
        self.flush()
        self.assertEqual(IncidentDraft.objects.get().extracted_data["priority"], "critical")
        self.assertIn("Crítica", self.last_reply())
        self.assertFalse(Incident.objects.exists())

    def test_cancel_in_burst_prevents_queued_confirmation_from_creating(self):
        self.queue("Cámara 2 Estudio A. Producción. Impide pruebas. Prioridad alta")
        self.flush()
        self.queue(CONFIRM, delta=12)
        self.queue("/cancelar", delta=12.2)
        self.flush(16)
        self.assertFalse(Incident.objects.exists())
        self.assertEqual(IncidentDraft.objects.get().status, "cancelled")

    def test_cancel_then_new_report_does_not_discard_later_messages(self):
        self.queue("Cámara 2 Estudio A. Producción. Impide pruebas. Prioridad alta")
        self.flush()
        self.queue("/cancelar", delta=12)
        self.queue("Nueva incidencia: Cámara 3 Estudio B sin imagen. Prioridad alta", delta=12.1)
        self.flush(16); self.flush(20)
        self.assertEqual(IncidentDraft.objects.filter(status="cancelled").count(), 1)
        self.assertEqual(IncidentDraft.objects.filter(status="ready").count(), 1)
        self.assertFalse(Incident.objects.exists())

    def test_start_delimits_group_and_keeps_following_report(self):
        self.queue("/start")
        self.queue("Cámara 2 Estudio A. Producción. Impide pruebas. Prioridad alta", delta=.1)
        self.flush(); self.flush(20)
        self.assertEqual(IncidentDraft.objects.count(), 1)
        self.assertIn("¿Confirmas", self.last_reply())

    def test_mixed_data_and_query_responds_using_updated_snapshot(self):
        self.queue("Cámara 2 Estudio A. Producción. Impide pruebas. Prioridad alta")
        self.queue("¿Qué cámara indiqué?")
        self.flush()
        self.assertIn("Cámara 2", self.last_reply())
        self.assertEqual(self.transport.send_text.call_count, 1)
        self.assertFalse(Incident.objects.exists())

    def test_pure_queries_do_not_change_draft_or_call_ai(self):
        self.queue("Cámara 2 Estudio A. Producción. Impide pruebas. Prioridad alta")
        self.flush()
        draft = IncidentDraft.objects.get()
        before = (draft.extracted_data, draft.status, draft.last_question, draft.updated_at)
        self.ai.extract_incident.reset_mock()
        self.queue("¿Qué cámara indiqué?", delta=12)
        self.queue("¿En qué ubicación está?", delta=12.1)
        self.flush(16)
        self.ai.extract_incident.assert_not_called()
        draft.refresh_from_db()
        self.assertEqual((draft.extracted_data, draft.status, draft.last_question, draft.updated_at), before)

    def test_restart_after_result_commit_before_delivery_does_not_reextract(self):
        self.queue("Cámara 2 Estudio A. Prioridad alta")
        self.queue("Área Producción. Impide pruebas")
        with patch.object(self.handler, "_deliver_reply", side_effect=SystemExit):
            with self.assertRaises(SystemExit): self.flush()
        calls = self.ai.extract_incident.call_count
        self.handler.recover_pending()
        self.assertEqual(self.ai.extract_incident.call_count, calls)
        self.assertEqual(self.transport.send_text.call_count, 1)
        self.assertEqual(ChannelMessage.objects.filter(direction="inbound", status="processed").count(), 2)

    def test_maximum_wait_flushes_continuous_typing_without_sleep(self):
        self.queue("Cámara 2 Estudio A. Prioridad alta")
        for n in range(1, 5): self.queue(f"Complemento {n} Producción", delta=n*2)
        self.assertIn(self.reporter.pk, self.groups.ready_reporters(now=self.clock + timedelta(seconds=8)))

    def test_exhausted_group_is_terminal_without_reprocessing_followers(self):
        self.queue("Cámara 2 Estudio A. Prioridad alta")
        self.queue("Área Producción. Impide pruebas")
        self.ai.extract_incident.side_effect = TimeoutError()
        self.flush(); self.flush(20); self.flush(30); self.flush(40)
        leader = ChannelMessage.objects.filter(direction="inbound").order_by("pk").first()
        self.assertEqual(leader.processing_attempts, 3)
        self.assertEqual(self.ai.extract_incident.call_count, 3)
        self.assertEqual(ChannelMessage.objects.filter(direction="outbound", reply_purpose="reply", status="sent").count(), 1)
        self.assertFalse(Incident.objects.exists())

    def test_grouped_polling_does_not_advance_offset_before_durable_receipt(self):
        handler = Mock()
        handler.queue_update.side_effect = [RuntimeError("QA"), {"action":"queued", "durable":True}, {"action":"queued", "durable":True}]
        command = Command(stdout=io.StringIO(), stderr=io.StringIO())
        with patch("incidents.management.commands.run_telegram_bot.TelegramGroupRunner") as runner, \
             patch.object(command, "_get_updates", side_effect=[[update(1), update(2)], [update(1), update(2)], KeyboardInterrupt]) as get_updates:
            with self.assertRaises(KeyboardInterrupt): command._poll_grouped(handler, Mock(), "QA", None)
        self.assertEqual([c.kwargs["offset"] for c in get_updates.call_args_list], [None, None, 3])
        self.assertEqual([c.args[0]["update_id"] for c in handler.queue_update.call_args_list], [1, 1, 2])
        runner.return_value.close.assert_called_once()

    def test_out_of_order_delivery_preserves_telegram_message_order(self):
        self.queue("Perdón: es la Cámara 2. Área Producción. Prioridad alta", number=2)
        self.queue("Cámara 3 Estudio A sin imagen. Impide pruebas", number=1)
        self.flush()
        text = self.ai.extract_incident.call_args.kwargs["message"]
        self.assertLess(text.index("Cámara 3"), text.index("Perdón"))

    def test_restart_recovers_unfrozen_group(self):
        self.queue("Cámara 2 Estudio A. Prioridad alta")
        self.queue("Área Producción. Impide pruebas")
        restarted = TelegramMessageGroups(TelegramUpdateHandler(messaging_provider=self.transport, incoming_service=self.handler.incoming_service))
        with patch("django.utils.timezone.now", return_value=self.clock + timedelta(seconds=10)):
            restarted.process_reporter(self.reporter.pk)
        self.assertEqual(self.transport.send_text.call_count, 1)
        self.assertEqual(IncidentDraft.objects.count(), 1)

    def test_transient_error_keeps_all_group_members_and_recovers_once(self):
        self.queue("Cámara 2 Estudio A. Prioridad alta")
        self.queue("Área Producción. Impide pruebas")
        self.ai.extract_incident.side_effect = TimeoutError()
        self.flush()
        self.assertEqual(ChannelMessage.objects.filter(direction="inbound").count(), 2)
        self.ai.extract_incident.side_effect = None
        self.flush(20); self.flush(30)
        self.assertEqual(IncidentDraft.objects.count(), 1)
        self.assertEqual(ChannelMessage.objects.filter(direction="outbound", reply_purpose="reply", status="sent").count(), 1)
        self.assertLessEqual(ChannelMessage.objects.filter(direction="outbound", reply_purpose="notice").count(), 1)

    def test_direct_recovery_keeps_frozen_group_instead_of_only_first_text(self):
        self.queue("Cámara 2 Estudio A. Producción. Impide pruebas. Prioridad alta")
        self.flush()
        self.queue(CONFIRM, delta=12)
        self.queue("Pero cambia la prioridad a crítica", delta=12.1)
        self.ai.extract_incident.side_effect = TimeoutError()
        self.flush(16)
        leader = ChannelMessage.objects.filter(direction="inbound", status="failed").last()
        self.ai.extract_incident.side_effect = None
        draft = IncidentDraft.objects.get()
        data = {key:value for key,value in draft.extracted_data.items() if not key.startswith("_")}
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**data, "priority":"critical"}, update_kind="correction", field_evidence={"priority":"crítica"})
        with patch("django.utils.timezone.now", return_value=self.clock + timedelta(seconds=20)):
            self.handler.handle(leader.raw_payload)
        self.assertEqual(IncidentDraft.objects.get().extracted_data["priority"], "critical")
        self.assertFalse(Incident.objects.exists())

    def test_manual_recovery_of_exhausted_group_preserves_its_members(self):
        self.queue("Cámara 2 Estudio A. Prioridad alta")
        self.queue("Área Producción. Impide pruebas")
        self.ai.extract_incident.side_effect = TimeoutError()
        self.flush(); self.flush(20); self.flush(30)
        leader = ChannelMessage.objects.filter(direction="inbound").order_by("pk").first()
        self.ai.extract_incident.side_effect = None
        with patch("incidents.messaging.telegram_update_handler.TelegramUpdateHandler", return_value=self.handler), \
             patch("django.utils.timezone.now", return_value=self.clock + timedelta(seconds=50)):
            call_command("recover_pilot", message_id=leader.pk, stdout=io.StringIO())
        self.assertEqual(IncidentDraft.objects.get().status, "ready")
        leader.refresh_from_db()
        self.assertEqual(len(leader.processing_result["_group_member_ids"]), 2)
        self.assertFalse(Incident.objects.exists())

    def test_uncertain_group_reply_needs_acknowledgement_and_can_be_manually_retried(self):
        self.queue("Cámara 2 Estudio A. Prioridad alta")
        self.queue("Área Producción. Impide pruebas")
        original = self.transport.send_text.side_effect
        self.transport.send_text.side_effect = TelegramDeliveryUncertainError("QA")
        self.flush()
        reply = ChannelMessage.objects.get(direction="outbound", reply_purpose="reply")
        self.assertEqual(reply.status, "uncertain")
        self.transport.send_text.side_effect = original
        self.flush(20)
        self.assertEqual(self.transport.send_text.call_count, 1)
        with patch("incidents.messaging.telegram_update_handler.TelegramUpdateHandler", return_value=self.handler):
            call_command("recover_pilot", reply_id=reply.pk, acknowledge_duplicate_risk=True, stdout=io.StringIO())
        reply.refresh_from_db(); self.assertEqual(reply.status, "sent")

    def test_group_respects_provider_retry_after_without_losing_members(self):
        self.queue("Cámara 2 Estudio A. Prioridad alta")
        self.queue("Área Producción. Impide pruebas")
        self.ai.extract_incident.side_effect = AITemporaryError("QA", retry_after=60)
        self.flush(10)
        self.flush(40)
        self.assertEqual(self.ai.extract_incident.call_count, 1)
        self.ai.extract_incident.side_effect = None
        self.flush(71)
        self.assertEqual(IncidentDraft.objects.count(), 1)
        self.assertEqual(ChannelMessage.objects.filter(direction="inbound", status="processed").count(), 2)
        self.assertEqual(ChannelMessage.objects.filter(direction="outbound", reply_purpose="reply", status="sent").count(), 1)

    def test_messages_arriving_during_retry_are_applied_before_final_reply(self):
        self.queue("Cámara 2 Estudio A. Prioridad alta")
        self.queue("Área Producción. Impide pruebas")
        self.ai.extract_incident.side_effect = AITemporaryError("QA", retry_after=60)
        self.flush(10)
        self.queue("La ubicación correcta es Estudio B", delta=20)
        self.ai.extract_incident.side_effect = None
        self.flush(71); self.flush(80)
        self.assertEqual(ChannelMessage.objects.filter(direction="inbound", status="processed").count(), 3)
        self.assertEqual(ChannelMessage.objects.filter(direction="outbound", reply_purpose="reply", status="sent").count(), 1)
        self.assertEqual(IncidentDraft.objects.get().extracted_data["location_context"], "Estudio B")
        self.assertFalse(Incident.objects.exists())

    def test_isolated_yes_with_later_ambiguous_rejection_does_not_create(self):
        self.queue("Cámara 2 Estudio A. Producción. Prioridad alta")
        self.flush()
        self.queue("Sí", delta=12)
        self.queue("No, espera, quiero revisar", delta=12.1)
        self.ai.extract_incident.return_value = IncidentExtractionResult(intent="conversation", conversation_reply="Ya registré SC-999999")
        self.flush(16)
        self.assertFalse(Incident.objects.exists())
        self.assertNotIn("SC-999999", self.last_reply())


@override_settings(TELEGRAM_GROUP_WINDOW_SECONDS=2.5, PILOT_MESSAGE_RETRY_SECONDS=0, DEBUG=False, TELEGRAM_AUTO_REGISTER=False, **SETTINGS)
class TelegramGroupConcurrencyTests(GroupFixture, TransactionTestCase):
    def test_arrival_during_ai_is_durable_and_obsolete_reply_is_suppressed(self):
        entered, release = Event(), Event()
        self.queue("Cámara 2 Estudio A. Producción. Impide pruebas. Prioridad alta")
        def extract(**kwargs):
            self.assertFalse(connection.in_atomic_block)
            entered.set(); self.assertTrue(release.wait(8))
            return IncidentExtractionResult(**DATA)
        self.ai.extract_incident.side_effect = extract
        def process():
            close_old_connections()
            try: return self.flush()
            finally: close_old_connections()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(process)
            self.assertTrue(entered.wait(4))
            try: self.queue("La ubicación correcta es Estudio B", delta=1)
            finally: release.set()
            future.result(12)
        self.transport.send_text.assert_not_called()
        self.ai.extract_incident.side_effect = None
        self.flush(20)
        self.assertEqual(self.transport.send_text.call_count, 1)
        self.assertIn("Estudio B", self.last_reply())
        self.assertFalse(Incident.objects.exists())

    def test_different_reporters_process_concurrently_with_bounded_runner(self):
        handler_fixture(202)
        self.queue("Cámara 2 Estudio A. Producción. Impide pruebas. Prioridad alta")
        self.handler.queue_update(update(1, user=202, text="Cámara 2 Estudio A. Producción. Impide pruebas. Prioridad alta"))
        barrier = Barrier(2)
        def extract(**kwargs):
            self.assertFalse(connection.in_atomic_block)
            barrier.wait(timeout=6)
            return IncidentExtractionResult(**DATA)
        self.ai.extract_incident.side_effect = extract
        runner = TelegramGroupRunner(self.handler, workers=2)
        try:
            with patch("django.utils.timezone.now", return_value=self.clock + timedelta(seconds=10)):
                runner.tick()
                self.assertEqual(len(runner.active), 2)
                for future in runner.active.values(): future.result(12)
        finally: runner.close()
        self.assertEqual(IncidentDraft.objects.count(), 2)
        self.assertEqual(self.transport.send_text.call_count, 2)
