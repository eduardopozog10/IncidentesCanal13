"""Regresiones puntuales: reinicio, exclusión activa, pausas y deadline de IA.

Solo PostgreSQL de tests, proveedores simulados y transporte HTTP en memoria.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from google import genai
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from incidents.ai.base import AIConfigurationError, AITemporaryError, AIUnavailableError
from incidents.ai.gemini_provider import GeminiAIProvider
from incidents.ai.schemas import IncidentExtractionResult
from incidents.messaging.telegram_update_handler import TelegramUpdateHandler
from incidents.models import ChannelMessage, Incident, IncidentDraft, Technician, TechnicianChannel
from incidents.services.conversation_lock import conversation_lock
from incidents.services.incident_processor import IncidentProcessor
from incidents.services.incoming_message_service import IncomingMessageService
from incidents.test_pilot_recovery import DATA, service_call, update


@override_settings(DEBUG=True, TELEGRAM_AUTO_REGISTER=True, PILOT_MESSAGE_RETRY_SECONDS=30)
class RecoveryLimitTests(TestCase):
    def setUp(self):
        self.ai = Mock()
        self.ai.extract_incident.return_value = IncidentExtractionResult(**DATA)
        self.service = IncomingMessageService(IncidentProcessor(self.ai, require_delivered_summary=True))
        self.messaging = Mock()
        self.messaging.send_text.side_effect = lambda *args: {"result": {"message_id": 1000 + self.messaging.send_text.call_count}}
        self.handler = TelegramUpdateHandler(messaging_provider=self.messaging, incoming_service=self.service)

    def interrupt(self, attempts=1):
        with patch.object(self.service.incident_processor, "process", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                service_call(self.service, update())
        row = ChannelMessage.objects.get(direction="inbound")
        row.processing_attempts = attempts
        row.save(update_fields=["processing_attempts"])
        return row

    def test_exact_675_shape_recovers_last_attempt_and_polling_replay_once(self):
        row = self.interrupt(attempts=2)
        ChannelMessage.objects.filter(pk=row.pk).update(error_message="TimeoutError", processing_result={})
        row.refresh_from_db()
        self.assertEqual((row.status, row.processing_attempts, row.received_via_polling,
                          row.error_message, row.processing_result, row.incident_id, row.draft_id),
                         ("processing", 2, True, "TimeoutError", {}, None, None))
        self.handler.recover_pending()
        sent = self.messaging.send_text.call_count
        self.handler.handle(update())
        self.handler.recover_pending()
        row.refresh_from_db()
        self.assertEqual((row.status, row.processing_attempts), ("processed", 3))
        self.assertEqual(IncidentDraft.objects.count(), 1)
        self.assertEqual(Incident.objects.count(), 0)
        self.assertEqual(self.messaging.send_text.call_count, sent)
        self.ai.extract_incident.assert_called_once()

    def test_restart_during_gemini_honors_persisted_pause_then_recovers(self):
        with patch.object(self.ai, "extract_incident", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                service_call(self.service, update())
        row = ChannelMessage.objects.get(direction="inbound")
        self.assertEqual(row.status, "processing")
        # Nueva instancia del receptor: no depende de memoria del servicio.
        restarted = TelegramUpdateHandler(messaging_provider=self.messaging,
            incoming_service=IncomingMessageService(IncidentProcessor(self.ai, require_delivered_summary=True)))
        self.assertEqual(restarted.recover_pending(), [])
        self.assertEqual(row.processing_attempts, 1)
        with patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(seconds=31)):
            restarted.recover_pending()
        row.refresh_from_db()
        self.assertEqual((row.status, row.processing_attempts), ("processed", 2))
        self.assertEqual(IncidentDraft.objects.count(), 1)

    def test_failure_pause_survives_replay_and_does_not_starve_other_reporter(self):
        self.ai.extract_incident.side_effect = TimeoutError()
        self.handler.handle(update())
        self.handler.handle(update())
        self.handler.handle(update(2, text="confirmo"))
        self.assertEqual(self.handler.recover_pending(limit=1), [])
        self.assertEqual(self.ai.extract_incident.call_count, 1)
        self.ai.extract_incident.side_effect = None
        # Dejar otro mensaje durable detrás de los aplazados.
        other = Technician.objects.create(full_name="Otro reportante")
        account = TechnicianChannel.objects.create(technician=other, provider="telegram", external_user_id="202", external_chat_id="202")
        payload = update(user=202)
        ChannelMessage.objects.create(provider="telegram", external_message_id="202:1", external_sender_id="202", external_chat_id="202",
            technician=other, channel_account=account, direction="inbound", content=payload["message"]["text"], raw_payload=payload, received_via_polling=True)
        self.handler.recover_pending(limit=1)
        self.assertEqual(ChannelMessage.objects.get(external_message_id="202:1").status, "processed")
        self.assertEqual(ChannelMessage.objects.get(external_message_id="101:1").processing_attempts, 1)
        self.assertEqual(Incident.objects.count(), 0)

    def test_exhaustion_delivers_final_once_then_releases_next_message(self):
        self.ai.extract_incident.side_effect = TimeoutError()
        now = timezone.now()
        self.handler.handle(update())
        self.handler.handle(update(2, text="confirmo"))
        for seconds in (31, 62):
            with patch("django.utils.timezone.now", return_value=now + timedelta(seconds=seconds)):
                self.handler.recover_pending(limit=1)
        row = ChannelMessage.objects.get(external_message_id="101:1")
        self.assertEqual((row.status, row.processing_attempts, row.error_message), ("failed", 3, "TimeoutError"))
        self.assertEqual(row.replies.filter(reply_purpose="reply", status="sent").count(), 1)
        self.ai.extract_incident.side_effect = None
        self.handler.recover_pending()
        self.handler.handle(update())
        later = ChannelMessage.objects.get(external_message_id="101:2")
        self.assertEqual(later.status, "processed")
        self.assertEqual(row.replies.filter(reply_purpose="reply").count(), 1)
        self.assertEqual(Incident.objects.count(), 0)

    def test_interrupted_exhausted_attempt_normalizes_without_new_ai_call(self):
        row = self.interrupt(attempts=3)
        ChannelMessage.objects.filter(pk=row.pk).update(error_message="TimeoutError")
        self.handler.recover_pending()
        self.handler.recover_pending()
        row.refresh_from_db()
        self.assertEqual((row.status, row.processing_attempts, row.error_message), ("failed", 3, "TimeoutError"))
        self.ai.extract_incident.assert_not_called()
        self.assertEqual(row.replies.filter(reply_purpose="reply").count(), 1)

    def test_failed_exhausted_empty_result_also_gets_terminal_reply(self):
        row = self.interrupt(attempts=3)
        ChannelMessage.objects.filter(pk=row.pk).update(status="failed", processing_result={})
        self.handler.recover_pending()
        self.ai.extract_incident.assert_not_called()
        self.assertEqual(row.replies.filter(reply_purpose="reply", status="sent").count(), 1)

    def test_configuration_error_is_terminal_without_three_provider_calls(self):
        self.ai.extract_incident.side_effect = AIConfigurationError("Rechazo simulado")
        self.handler.handle(update())
        self.handler.recover_pending()
        self.handler.handle(update())
        row = ChannelMessage.objects.get(direction="inbound")
        self.assertEqual((row.status, row.processing_attempts, row.error_message), ("failed", 1, "AIConfigurationError"))
        self.ai.extract_incident.assert_called_once()
        self.assertEqual(self.messaging.send_text.call_count, 1)
        self.assertEqual(IncidentDraft.objects.count(), 0)

    def test_provider_cooldown_is_persisted_without_ai_timeout_conversation_mutation(self):
        self.ai.extract_incident.side_effect = AIUnavailableError("QA", retry_after=60)
        now = timezone.now()
        with patch("django.utils.timezone.now", return_value=now):
            self.handler.handle(update())
        row = ChannelMessage.objects.get(direction="inbound")
        self.assertEqual(row.error_message, "AIUnavailableError")
        self.assertEqual(row.processing_result["_retry_not_before"], now.timestamp() + 60)
        with patch("django.utils.timezone.now", return_value=now + timedelta(seconds=31)):
            self.assertEqual(self.handler.recover_pending(), [])
        self.assertEqual(IncidentDraft.objects.count(), 0)
        self.ai.extract_incident.assert_called_once()


@override_settings(GEMINI_API_KEY="qa-placeholder")
class GeminiDeadlineTests(TestCase):
    def test_real_sdk_with_mock_transport_has_no_hidden_retry_on_500_503(self):
        original_client = genai.Client
        responses = []

        def transport(request):
            status = (500, 503)[len(responses)]
            responses.append(status)
            return httpx.Response(status, json={"error": {"code": status, "message": "QA", "status": "UNAVAILABLE"}})

        with httpx.Client(transport=httpx.MockTransport(transport)) as http_client:
            def client(**kwargs):
                self.assertEqual(kwargs["http_options"].retry_options.attempts, 1)
                kwargs["http_options"].httpx_client = http_client
                return original_client(**kwargs)

            with patch("incidents.ai.gemini_provider.genai.Client", side_effect=client):
                provider = GeminiAIProvider()
            with self.assertRaises(AIUnavailableError):
                provider.extract_incident("QA")
            with self.assertRaises(AIUnavailableError):
                provider.extract_incident("Otro reportante durante enfriamiento")
        self.assertEqual(responses, [500, 503])

    def test_never_returning_transport_bounds_wait_and_keeps_only_one_request(self):
        provider = GeminiAIProvider()
        provider.TOTAL_TIMEOUT_SECONDS = .06
        provider.FIRST_ATTEMPT_TIMEOUT_SECONDS = .04
        release = threading.Event()

        def stuck(**kwargs):
            release.wait(timeout=2)
            return SimpleNamespace(output_text="resultado tardío descartado")

        with patch.object(provider.client.interactions, "create", side_effect=stuck) as request:
            started = time.monotonic()
            try:
                with self.assertRaises(AITemporaryError):
                    provider.extract_incident("QA")
                self.assertLess(time.monotonic() - started, .5)
                with self.assertRaises(AITemporaryError):
                    provider.extract_incident("QA otra vez")
                self.assertEqual(request.call_count, 1)
            finally:
                release.set()
                provider._request_thread.join(timeout=1)
        self.assertFalse(provider._request_thread.is_alive())
        self.assertEqual(Incident.objects.count(), 0)

    def test_permanent_http_error_is_classified_without_retry(self):
        provider = GeminiAIProvider()
        error = RuntimeError("QA")
        error.code = 403
        with patch.object(provider.client.interactions, "create", side_effect=error) as request:
            with self.assertRaises(AIConfigurationError):
                provider.extract_incident("QA")
        request.assert_called_once()

    def test_late_success_beyond_total_budget_is_discarded(self):
        provider = GeminiAIProvider()
        clock = [0]

        def late(**kwargs):
            clock[0] = 46
            return SimpleNamespace(output_text="no aplicar")

        with patch("incidents.ai.gemini_provider.time.perf_counter", side_effect=lambda: clock[0]), \
             patch.object(provider.client.interactions, "create", side_effect=late) as request:
            with self.assertRaises(AITemporaryError):
                provider.extract_incident("QA")
        request.assert_called_once()


@override_settings(DEBUG=True, TELEGRAM_AUTO_REGISTER=True, PILOT_MESSAGE_RETRY_SECONDS=0)
class ActiveRecoveryTests(TransactionTestCase):
    def test_active_reporter_is_skipped_while_another_reporter_recovers(self):
        for user in (101, 202):
            reporter = Technician.objects.create(full_name=f"QA {user}")
            account = TechnicianChannel.objects.create(technician=reporter, provider="telegram", external_user_id=str(user), external_chat_id=str(user))
            payload = update(user=user)
            ChannelMessage.objects.create(provider="telegram", external_message_id=f"{user}:1", external_sender_id=str(user), external_chat_id=str(user),
                channel_account=account, technician=reporter, direction="inbound", content=payload["message"]["text"], raw_payload=payload,
                received_via_polling=True, status="processing", processing_attempts=2)
        active = ChannelMessage.objects.get(external_message_id="101:1")
        entered, release = threading.Event(), threading.Event()

        def own_lock():
            close_old_connections()
            try:
                with conversation_lock(f"reporter:{active.technician_id}"):
                    entered.set()
                    release.wait(timeout=5)
            finally:
                close_old_connections()

        ai, messaging = Mock(), Mock()
        ai.extract_incident.return_value = IncidentExtractionResult(**DATA)
        messaging.send_text.side_effect = lambda *args: {"result": {"message_id": 1000 + messaging.send_text.call_count}}
        handler = TelegramUpdateHandler(messaging_provider=messaging, incoming_service=IncomingMessageService(IncidentProcessor(ai)))
        with ThreadPoolExecutor(max_workers=1) as pool:
            owner = pool.submit(own_lock)
            self.assertTrue(entered.wait(timeout=2))
            try:
                handler.recover_pending(limit=1)
                # La reentrega del mismo update tampoco espera el lock activo.
                self.assertEqual(handler.handle(update())["action"], "processing_pending")
                active.refresh_from_db()
                self.assertEqual((active.status, active.processing_attempts), ("processing", 2))
                self.assertEqual(ChannelMessage.objects.get(external_message_id="202:1").status, "processed")
                self.assertFalse(release.is_set())
            finally:
                release.set()
                owner.result(timeout=2)
        ai.extract_incident.assert_called_once()
        self.assertEqual(IncidentDraft.objects.count(), 1)
