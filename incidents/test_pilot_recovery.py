"""Fallos y caídas simuladas. Ninguna prueba usa APIs ni SMTP reales."""
import io
import json
import smtplib
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch
from unittest import skip

from django.core import mail
from django.core.management import call_command
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings

from incidents.ai.gemini_provider import GeminiAIProvider
from incidents.ai.schemas import IncidentExtractionResult
from incidents.management.commands.run_telegram_bot import Command as PollingCommand
from incidents.messaging.telegram_provider import TelegramDeliveryUncertainError, TelegramRejectedError
from incidents.messaging.telegram_update_handler import TelegramUpdateHandler
from incidents.models import ChannelMessage, Incident, IncidentDraft, IncidentNotification, Technician, TechnicianChannel
from incidents.services.incident_notification_service import send_incident_created_email
from incidents.services.incident_processor import IncidentProcessor
from incidents.services.incoming_message_service import IncomingMessageService
from incidents.services.notification_worker import start_notification_worker


DATA = dict(area="Producción", unit="Estudio 2", location_context="Estudio 2", equipment="Cámara 3",
            asset_category="video_cameras", failure_type="Sin imagen", description="La Cámara 3 no tiene imagen",
            operational_impact="No podemos grabar", priority="high")


def update(number=1, user=101, text="La Cámara 3 no tiene imagen. Prioridad alta."):
    return {"update_id": number, "message": {"message_id": number, "from": {"id": user},
            "chat": {"id": user, "type": "private"}, "text": text}}


def service_call(service, payload):
    message = payload["message"]
    return service.process_text(provider="telegram", external_user_id=str(message["from"]["id"]),
                                external_chat_id=str(message["chat"]["id"]),
                                external_message_id=f"{message['chat']['id']}:{message['message_id']}",
                                text=message["text"], raw_payload=payload, trusted_telegram=True)


@override_settings(DEBUG=True, TELEGRAM_AUTO_REGISTER=True, PILOT_MESSAGE_RETRY_SECONDS=0, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", INCIDENT_NOTIFICATION_EMAIL="qa@example.invalid")
class MessageRecoveryTests(TestCase):
    def setUp(self):
        self.ai = Mock()
        self.ai.extract_incident.return_value = IncidentExtractionResult(**DATA)
        self.service = IncomingMessageService(IncidentProcessor(self.ai, require_delivered_summary=True))
        self.messaging = Mock()
        self.sent = 100

        def send(recipient, text):
            self.sent += 1
            return {"result": {"message_id": self.sent}}

        self.messaging.send_text.side_effect = send
        self.handler = TelegramUpdateHandler(messaging_provider=self.messaging, incoming_service=self.service)

    def test_same_id_failed_before_completion_is_recovered(self):
        self.ai.extract_incident.side_effect = RuntimeError("Fallo temporal simulado")
        first = service_call(self.service, update())
        self.assertTrue(first["pending"])
        self.assertEqual(ChannelMessage.objects.get(direction="inbound").status, "failed")
        self.ai.extract_incident.side_effect = None
        self.handler.recover_pending()
        self.assertEqual(IncidentDraft.objects.count(), 1)
        self.assertEqual(ChannelMessage.objects.filter(direction="inbound").count(), 1)
        self.assertEqual(ChannelMessage.objects.get(direction="inbound").processing_attempts, 2)
        self.assertEqual(Incident.objects.count(), 0)

    def test_processed_duplicate_does_not_repeat_ai_or_response(self):
        self.handler.handle(update())
        calls = self.messaging.send_text.call_count
        self.assertEqual(self.handler.handle(update())["action"], "duplicate_message")
        self.assertEqual(self.messaging.send_text.call_count, calls)
        self.ai.extract_incident.assert_called_once()
        self.assertEqual(IncidentDraft.objects.count(), 1)

    def test_crash_after_receive_before_processing_recovers(self):
        with patch.object(self.service.incident_processor, "process", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                service_call(self.service, update())
        row = ChannelMessage.objects.get(direction="inbound")
        self.assertEqual(row.status, "processing")
        self.handler.recover_pending()
        row.refresh_from_db()
        self.assertEqual(row.status, "processed")
        self.assertEqual(row.processing_attempts, 2)
        self.assertEqual(IncidentDraft.objects.count(), 1)

    def test_received_record_without_attempt_is_recovered(self):
        reporter = Technician.objects.create(full_name="QA")
        account = TechnicianChannel.objects.create(technician=reporter, provider="telegram", external_user_id="101", external_chat_id="101")
        ChannelMessage.objects.create(provider="telegram", external_message_id="101:1", external_sender_id="101", external_chat_id="101",
                                      channel_account=account, technician=reporter, direction="inbound", content=update()["message"]["text"], raw_payload=update(), received_via_polling=True)
        self.handler.recover_pending()
        self.assertEqual(ChannelMessage.objects.get(direction="inbound").status, "processed")

    def test_crash_after_incident_commit_replays_only_reply(self):
        self.handler.handle(update())
        with patch.object(self.handler, "_deliver_reply", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                self.handler.handle(update(2, text="confirmo"))
        self.assertEqual(Incident.objects.count(), 1)
        self.assertEqual(IncidentNotification.objects.get().status, "pending")
        self.handler.recover_pending()
        self.handler.handle(update(2, text="confirmo"))
        self.assertEqual(Incident.objects.count(), 1)
        self.assertEqual(ChannelMessage.objects.filter(in_reply_to__external_message_id="101:2", reply_purpose="reply").count(), 1)
        self.assertEqual(self.ai.extract_incident.call_count, 1)

    def test_rejected_reply_is_retried_without_processing_again(self):
        original = self.messaging.send_text.side_effect
        self.messaging.send_text.side_effect = TelegramRejectedError()
        self.assertEqual(self.handler.handle(update())["action"], "reply_failed")
        self.messaging.send_text.side_effect = original
        self.handler.recover_pending()
        outgoing = ChannelMessage.objects.get(direction="outbound", reply_purpose="reply")
        self.assertEqual(outgoing.status, "sent")
        self.assertEqual(outgoing.processing_attempts, 2)
        self.ai.extract_incident.assert_called_once()

    def test_uncertain_send_is_never_repeated_automatically(self):
        self.messaging.send_text.side_effect = TelegramDeliveryUncertainError("Acuse perdido")
        self.handler.handle(update())
        self.handler.handle(update())
        self.handler.recover_pending()
        self.messaging.send_text.assert_called_once()
        self.assertEqual(ChannelMessage.objects.get(direction="outbound").status, "uncertain")

    def test_interrupted_sending_is_marked_uncertain_without_resend(self):
        service_call(self.service, update())
        incoming = ChannelMessage.objects.get(direction="inbound")
        ChannelMessage.objects.create(provider="telegram", direction="outbound", in_reply_to=incoming, status="processing",
                                      external_chat_id="101", external_sender_id="bot", content=incoming.processing_result["reply"])
        self.handler.recover_pending()
        self.messaging.send_text.assert_not_called()
        self.assertEqual(incoming.replies.get().status, "uncertain")

    def test_multipart_rejection_resumes_only_unsent_parts(self):
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**DATA, "description": "La cámara pierde imagen. " * 500})
        original = self.messaging.send_text.side_effect
        texts = []

        def send(recipient, text):
            texts.append(text)
            if len(texts) == 2:
                raise TelegramRejectedError()
            return original(recipient, text)

        self.messaging.send_text.side_effect = send
        self.handler.handle(update())
        self.assertEqual(Incident.objects.count(), 0)
        self.handler.recover_pending()
        self.assertEqual(texts.count(texts[0]), 1)
        self.assertEqual(ChannelMessage.objects.get(direction="outbound", reply_purpose="reply").status, "sent")
        self.assertTrue(self.handler.handle(update(2, text="confirmo"))["incident_created"])
        self.assertEqual(Incident.objects.count(), 1)

    def test_temporary_timeout_recovers_same_input_without_user_repetition(self):
        self.ai.extract_incident.side_effect = TimeoutError()
        self.assertEqual(self.handler.handle(update())["action"], "processing_pending")
        self.ai.extract_incident.side_effect = None
        self.handler.recover_pending()
        self.assertEqual(IncidentDraft.objects.count(), 1)
        self.assertEqual(Incident.objects.count(), 0)
        self.assertEqual(ChannelMessage.objects.filter(direction="inbound").count(), 1)

    def test_attempt_budget_is_bounded_and_notice_is_not_duplicated(self):
        self.ai.extract_incident.side_effect = TimeoutError()
        self.handler.handle(update())
        self.handler.recover_pending()
        self.handler.recover_pending()
        self.handler.recover_pending()
        row = ChannelMessage.objects.get(direction="inbound")
        self.assertEqual(row.processing_attempts, 3)
        self.assertEqual(row.status, "failed")
        self.assertEqual(row.replies.filter(reply_purpose="notice").count(), 1)
        self.assertEqual(row.replies.filter(reply_purpose="reply").count(), 1)
        self.assertEqual(self.messaging.send_text.call_count, 2)
        self.assertEqual(Incident.objects.count(), 0)

    def test_later_message_waits_for_recoverable_earlier_message(self):
        self.ai.extract_incident.side_effect = TimeoutError()
        self.handler.handle(update())
        result = self.handler.handle(update(2, text="confirmo"))
        self.assertEqual(result["action"], "processing_pending")
        self.assertEqual(Incident.objects.count(), 0)
        self.ai.extract_incident.side_effect = None
        self.handler.recover_pending()
        # La confirmación llegó antes del resumen recuperado: conservar el
        # orden no permite autorizar datos que todavía no se habían entregado.
        self.assertEqual(Incident.objects.count(), 0)
        self.assertEqual(IncidentDraft.objects.get().status, "ready")
        self.assertIn("¿Confirmas", self.messaging.send_text.call_args.args[1])
        self.handler.handle(update(3, text="confirmo"))
        self.assertEqual(Incident.objects.count(), 1)
        self.assertEqual(IncidentDraft.objects.count(), 1)

    def test_http_json_cannot_be_replayed_as_trusted_polling(self):
        reporter = Technician.objects.create(full_name="QA")
        TechnicianChannel.objects.create(technician=reporter, provider="telegram", external_user_id="101", external_chat_id="101")
        self.service.process_text(provider="telegram", external_user_id="101", external_chat_id="101", external_message_id="http-test",
                                  text="hola", raw_payload=update(user=202), trusted_telegram=False)
        self.assertFalse(ChannelMessage.objects.get().received_via_polling)
        self.assertEqual(self.handler.recover_pending(), [])
        self.assertEqual(Technician.objects.count(), 1)
        self.messaging.send_text.assert_not_called()

    def test_completed_parts_can_finalize_after_crash_without_resending(self):
        service_call(self.service, update())
        incoming = ChannelMessage.objects.get(direction="inbound")
        outgoing = ChannelMessage.objects.create(provider="telegram", direction="outbound", in_reply_to=incoming, status="processing",
                      external_chat_id="101", external_sender_id="bot", content="Respuesta completa", processing_attempts=3,
                      raw_payload={"parts_total": 1, "parts_done": [{"result": {"message_id": 999}}]})
        # Recuperación automática: no hace falta una cuarta llamada externa.
        self.handler.recover_pending()
        outgoing.refresh_from_db()
        self.assertEqual(outgoing.status, "sent")
        self.messaging.send_text.assert_not_called()

    def test_cancel_replay_does_not_cancel_a_newer_draft(self):
        self.handler.handle(update())
        first = IncidentDraft.objects.get()
        self.handler.handle(update(2, text="/cancelar"))
        self.handler.handle(update(3))
        newer = IncidentDraft.objects.exclude(pk=first.pk).get()
        before = self.messaging.send_text.call_count
        self.handler.handle(update(2, text="/cancelar"))
        newer.refresh_from_db()
        self.assertEqual(newer.status, "ready")
        self.assertEqual(self.messaging.send_text.call_count, before)

    def test_start_is_durable_and_deduplicated_without_ai(self):
        self.handler.handle(update(text="/start"))
        self.handler.handle(update(text="/start"))
        self.assertEqual(Technician.objects.count(), 1)
        self.assertEqual(self.messaging.send_text.call_count, 1)
        self.ai.extract_incident.assert_not_called()

    def test_failure_saving_result_rolls_back_incident_and_notification(self):
        from django.db import IntegrityError
        from django.db.models.query import QuerySet
        self.handler.handle(update())
        original = QuerySet.update

        def fail_cache(queryset, **values):
            if values.get("processing_result", {}).get("action") == "incident_created":
                raise IntegrityError("Fallo simulado antes del commit")
            return original(queryset, **values)

        with patch.object(QuerySet, "update", new=fail_cache):
            self.handler.handle(update(2, text="confirmo"))
        self.assertEqual(Incident.objects.count(), 0)
        self.assertEqual(IncidentNotification.objects.count(), 0)
        self.handler.recover_pending()
        self.assertEqual(Incident.objects.count(), 1)
        self.assertEqual(IncidentNotification.objects.count(), 1)

    def test_registration_race_reuses_existing_active_account(self):
        self.handler.handle(update())
        account = self.service._auto_register_telegram_user(external_user_id="101", external_chat_id="101", raw_payload=update())
        self.assertEqual(account, TechnicianChannel.objects.get())
        self.assertEqual(Technician.objects.count(), 1)

    def test_manual_uncertain_reply_requires_acknowledgement(self):
        from django.core.management.base import CommandError
        original = self.messaging.send_text.side_effect
        self.messaging.send_text.side_effect = TelegramDeliveryUncertainError("QA")
        self.handler.handle(update())
        reply = ChannelMessage.objects.get(direction="outbound")
        self.messaging.send_text.side_effect = original
        with patch("incidents.messaging.telegram_update_handler.TelegramUpdateHandler", return_value=self.handler):
            with self.assertRaises(CommandError):
                call_command("recover_pilot", reply_id=reply.pk, stdout=io.StringIO())
            call_command("recover_pilot", reply_id=reply.pk, acknowledge_duplicate_risk=True, stdout=io.StringIO())
            call_command("recover_pilot", reply_id=reply.pk, stdout=io.StringIO())
        self.assertEqual(self.messaging.send_text.call_count, 2)
        reply.refresh_from_db()
        self.assertEqual(reply.status, "sent")

    def test_manual_exhausted_processing_can_resume_without_creating_incident(self):
        self.ai.extract_incident.side_effect = TimeoutError()
        self.handler.handle(update())
        self.handler.recover_pending()
        self.handler.recover_pending()
        incoming = ChannelMessage.objects.get(direction="inbound")
        self.ai.extract_incident.side_effect = None
        with patch("incidents.messaging.telegram_update_handler.TelegramUpdateHandler", return_value=self.handler):
            call_command("recover_pilot", message_id=incoming.pk, stdout=io.StringIO())
        incoming.refresh_from_db()
        self.assertEqual(incoming.status, "processed")
        self.assertEqual(IncidentDraft.objects.get().status, "ready")
        self.assertEqual(Incident.objects.count(), 0)
        self.assertEqual(incoming.replies.filter(reply_purpose="reply").count(), 1)

    def test_blocked_reporter_is_not_contacted_by_recovery(self):
        from django.core.management.base import CommandError
        self.messaging.send_text.side_effect = TelegramDeliveryUncertainError("QA")
        self.handler.handle(update())
        reply = ChannelMessage.objects.get(direction="outbound")
        Technician.objects.update(is_active=False)
        self.assertEqual(self.handler.recover_pending(), [])
        with patch("incidents.messaging.telegram_update_handler.TelegramUpdateHandler", return_value=self.handler):
            with self.assertRaises(CommandError):
                call_command("recover_pilot", reply_id=reply.pk, acknowledge_duplicate_risk=True, stdout=io.StringIO())
        self.messaging.send_text.assert_called_once()

    def test_crash_on_last_attempt_does_not_leave_processing_forever(self):
        reporter = Technician.objects.create(full_name="QA")
        account = TechnicianChannel.objects.create(technician=reporter, provider="telegram", external_user_id="101", external_chat_id="101")
        row = ChannelMessage.objects.create(provider="telegram", external_message_id="101:1", external_sender_id="101", external_chat_id="101",
            channel_account=account, technician=reporter, direction="inbound", content=update()["message"]["text"], raw_payload=update(),
            received_via_polling=True, status="processing", processing_attempts=3)
        self.handler.recover_pending()
        row.refresh_from_db()
        self.assertEqual(row.status, "failed")
        self.assertEqual(row.processing_result["action"], "processing_failed")
        self.ai.extract_incident.assert_not_called()
        self.assertEqual(Incident.objects.count(), 0)

    def test_interrupted_timeout_bookkeeping_still_recovers_original_input(self):
        # Simular la caída entre el commit del timeout y el registro de fallo
        # del servicio. No debe enviarse el timeout como resultado definitivo.
        self.ai.extract_incident.side_effect = TimeoutError()
        original = self.service.incident_processor.process
        with self.assertRaises(SystemExit):
            def interrupt(message):
                original(message)
                raise SystemExit
            with patch.object(self.service.incident_processor, "process", side_effect=interrupt):
                service_call(self.service, update())
        row = ChannelMessage.objects.get(direction="inbound")
        self.assertEqual(row.status, "processed")
        self.ai.extract_incident.side_effect = None
        self.handler.recover_pending()
        self.assertEqual(IncidentDraft.objects.count(), 1)
        row.refresh_from_db()
        self.assertNotEqual(row.processing_result["action"], "ai_timeout")


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", INCIDENT_NOTIFICATION_EMAIL="qa@example.invalid")
class NotificationRecoveryTests(TestCase):
    def setUp(self):
        self.incident = Incident.objects.create(technician=Technician.objects.create(full_name="QA"), **DATA)

    def test_failed_connection_retries_email_without_duplicate_incident_or_mail(self):
        with patch("incidents.services.incident_notification_service.send_mail", side_effect=smtplib.SMTPConnectError(421, b"QA")):
            self.assertFalse(send_incident_created_email(self.incident.pk))
        self.assertEqual(IncidentNotification.objects.get().status, "failed")
        self.assertTrue(send_incident_created_email(self.incident.pk))
        self.assertTrue(send_incident_created_email(self.incident.pk))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(Incident.objects.count(), 1)
        self.assertEqual(IncidentNotification.objects.get().attempts, 2)

    def test_smtp_timeout_is_persisted_as_uncertain_not_auto_retried(self):
        with patch("incidents.services.incident_notification_service.send_mail", side_effect=TimeoutError()) as send:
            self.assertFalse(send_incident_created_email(self.incident.pk))
            self.assertFalse(send_incident_created_email(self.incident.pk))
        send.assert_called_once()
        self.assertEqual(IncidentNotification.objects.get().status, "uncertain")

    def test_interrupted_sending_notification_is_not_sent_again(self):
        IncidentNotification.objects.create(incident=self.incident, status="sending", attempts=1)
        with patch("incidents.services.incident_notification_service.send_mail") as send:
            self.assertFalse(send_incident_created_email(self.incident.pk))
        send.assert_not_called()

    def test_manual_uncertain_notification_requires_acknowledgement(self):
        from django.core.management.base import CommandError
        notification = IncidentNotification.objects.create(incident=self.incident, status="uncertain", attempts=1)
        with self.assertRaises(CommandError):
            call_command("recover_pilot", notification_id=notification.pk, stdout=io.StringIO())
        call_command("recover_pilot", notification_id=notification.pk, acknowledge_duplicate_risk=True, stdout=io.StringIO())
        self.assertEqual(len(mail.outbox), 1)
        call_command("recover_pilot", notification_id=notification.pk, stdout=io.StringIO())
        self.assertEqual(len(mail.outbox), 1)

    def test_listing_recovery_state_does_not_send_anything(self):
        IncidentNotification.objects.create(incident=self.incident)
        with patch("incidents.services.incident_notification_service.send_mail") as send:
            call_command("recover_pilot", stdout=io.StringIO())
        send.assert_not_called()

    def test_configured_smtp_timeout_reaches_backend(self):
        from django.core.mail.backends.smtp import EmailBackend
        self.assertEqual(EmailBackend().timeout, 10)


@override_settings(GEMINI_API_KEY="qa-placeholder")
class GeminiRetryTests(TestCase):
    def test_503_is_retried_with_remaining_time_budget(self):
        provider = GeminiAIProvider()
        error = RuntimeError("503 UNAVAILABLE")
        error.code = 503
        with patch.object(provider.client.interactions, "create", side_effect=[error, SimpleNamespace(output_text=json.dumps({"intent": "incident_report", **DATA}))]) as request:
            with patch("incidents.ai.gemini_provider.time.perf_counter", side_effect=[0, 0, 0, 30, 30, 30, 40]):
                result = provider.extract_incident("Prioridad alta")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].kwargs["timeout"], 30)
        self.assertEqual(request.call_args_list[1].kwargs["timeout"], 15)
        self.assertEqual(result.equipment, "Cámara 3")

    def test_exhausted_503_becomes_recoverable_timeout(self):
        provider = GeminiAIProvider()
        error = RuntimeError("QA")
        error.code = 503
        with patch.object(provider.client.interactions, "create", side_effect=error) as request:
            with self.assertRaises(TimeoutError):
                provider.extract_incident("Reporte QA")
        self.assertEqual(request.call_count, 2)


@override_settings(DEBUG=True, TELEGRAM_AUTO_REGISTER=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", INCIDENT_NOTIFICATION_EMAIL="qa@example.invalid")
class RecoveryConcurrencyTests(TransactionTestCase):
    def thread_call(self, user, ai):
        close_old_connections()
        try:
            service = IncomingMessageService(IncidentProcessor(ai))
            return service_call(service, update(user=user))
        finally:
            close_old_connections()

    def test_different_reporters_reach_ai_in_parallel_without_atomic_transaction(self):
        barrier = threading.Barrier(2)
        observed = []

        def extract(**kwargs):
            observed.append(connection.in_atomic_block)
            barrier.wait(timeout=5)
            return IncidentExtractionResult(**DATA)

        ai = Mock()
        ai.extract_incident.side_effect = extract
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda user: self.thread_call(user, ai), [101, 202]))
        self.assertEqual(observed, [False, False])
        self.assertEqual(IncidentDraft.objects.count(), 2)
        self.assertTrue(all(not result["processing"]["incident_created"] for result in results))

    def test_same_reporter_concurrent_duplicate_only_extracts_once(self):
        ai = Mock()
        ai.extract_incident.return_value = IncidentExtractionResult(**DATA)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.thread_call(101, ai), range(2)))
        self.assertEqual(sum(result["duplicate"] for result in results), 1)
        self.assertEqual(IncidentDraft.objects.count(), 1)
        self.assertEqual(ChannelMessage.objects.filter(direction="inbound").count(), 1)
        ai.extract_incident.assert_called_once()

    def test_slow_mail_worker_does_not_block_message_processing(self):
        entered, release, stop = threading.Event(), threading.Event(), threading.Event()

        def slow_mail():
            entered.set()
            release.wait(timeout=5)

        with patch("incidents.services.notification_worker.drain_notifications", side_effect=slow_mail):
            thread = start_notification_worker(stop)
            try:
                self.assertTrue(entered.wait(timeout=3))
                ai = Mock()
                ai.extract_incident.return_value = IncidentExtractionResult(**DATA)
                result = self.thread_call(101, ai)
                self.assertFalse(result["processing"]["incident_created"])
                self.assertFalse(release.is_set())
            finally:
                stop.set()
                release.set()
                thread.join(timeout=3)
        self.assertFalse(thread.is_alive())

    def test_two_different_messages_same_reporter_keep_order_and_data(self):
        entered, release = threading.Event(), threading.Event()
        ai = Mock()

        def extract(**kwargs):
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return IncidentExtractionResult(**{**DATA, "priority": None})

        ai.extract_incident.side_effect = extract

        def handle(payload):
            close_old_connections()
            try:
                messaging = Mock()
                messaging.send_text.return_value = {"result": {"message_id": 1000 + payload["message"]["message_id"]}}
                handler = TelegramUpdateHandler(messaging_provider=messaging,
                    incoming_service=IncomingMessageService(IncidentProcessor(ai, require_delivered_summary=True)))
                return handler.handle(payload)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(handle, update(text="La Cámara 3 no tiene imagen"))
            self.assertTrue(entered.wait(timeout=3))
            second = pool.submit(handle, update(2, text="Alta"))
            release.set()
            first.result(timeout=8)
            second.result(timeout=8)
        messaging = Mock()
        messaging.send_text.return_value = {"result": {"message_id": 2000}}
        TelegramUpdateHandler(messaging_provider=messaging, incoming_service=IncomingMessageService(IncidentProcessor(ai))).recover_pending()
        draft = IncidentDraft.objects.get()
        self.assertEqual(draft.extracted_data["equipment"], "Cámara 3")
        self.assertEqual(draft.extracted_data["priority"], "high")
        self.assertEqual(draft.status, "ready")
        self.assertEqual(Incident.objects.count(), 0)
        ai.extract_incident.assert_called_once()

    def test_smtp_runs_without_open_transaction(self):
        incident = Incident.objects.create(technician=Technician.objects.create(full_name="QA"), **DATA)
        observed = []

        def send(**kwargs):
            observed.append(connection.in_atomic_block)
            return 1

        with patch("incidents.services.incident_notification_service.send_mail", side_effect=send):
            self.assertTrue(send_incident_created_email(incident.pk))
        self.assertEqual(observed, [False])

    def test_two_notification_workers_send_one_email(self):
        incident = Incident.objects.create(technician=Technician.objects.create(full_name="QA"), **DATA)

        def send(_):
            close_old_connections()
            try:
                return send_incident_created_email(incident.pk)
            finally:
                close_old_connections()

        with patch("incidents.services.incident_notification_service.send_mail", return_value=1) as smtp:
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(list(pool.map(send, range(2))), [True, True])
        smtp.assert_called_once()
        self.assertEqual(IncidentNotification.objects.get().attempts, 1)


@override_settings(TELEGRAM_BOT_TOKEN="qa-token")
class PollingAcknowledgementTests(TestCase):
    def test_get_updates_503_is_classified_as_transient(self):
        from incidents.management.commands.run_telegram_bot import TelegramPollingTemporaryError
        client = Mock()
        client.post.return_value.status_code = 503
        with self.assertRaises(TelegramPollingTemporaryError):
            PollingCommand()._get_updates(client=client, api_url="https://example.invalid", offset=None, timeout_seconds=25)

    def test_failed_update_does_not_advance_offset_or_skip_next_update(self):
        handler = Mock()
        handler.handle.side_effect = [RuntimeError("QA"), {"action": "ignored"}, {"action": "ignored"}]
        command = PollingCommand(stdout=io.StringIO(), stderr=io.StringIO())
        with patch("incidents.management.commands.run_telegram_bot.TelegramUpdateHandler", return_value=handler), \
             patch("incidents.management.commands.run_telegram_bot.start_notification_worker", return_value=Mock()), \
             patch("incidents.management.commands.run_telegram_bot.threading.Timer"), \
             patch.object(command, "_get_updates", side_effect=[[update(1), update(2)], [update(1), update(2)], KeyboardInterrupt]) as get_updates:
            command.handle(discard_pending=False)
        self.assertEqual([call.kwargs["offset"] for call in get_updates.call_args_list], [None, None, 3])
        self.assertEqual([call.args[0]["update_id"] for call in handler.handle.call_args_list], [1, 1, 2])


class PendingDeliveryGuarantees(TestCase):
    @skip("Pendiente: Telegram no ofrece una clave de idempotencia de envío. Aceptación remota sin acuse requiere revisión manual, no reenvío automático.")
    def test_exactly_once_telegram_when_acknowledgement_is_lost(self):
        pass

    @skip("Pendiente: SMTP puede aceptar DATA y perder el acuse. No se garantiza entrega exactamente una vez; estado incierto y revisión manual.")
    def test_exactly_once_smtp_when_acknowledgement_is_lost(self):
        pass
