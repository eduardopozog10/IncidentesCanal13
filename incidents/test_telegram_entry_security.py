"""Seguridad de entradas e identidad; sin Telegram, Gemini ni SMTP reales."""
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from incidents.ai.schemas import IncidentExtractionResult
from incidents.messaging.telegram_update_handler import TelegramUpdateHandler
from incidents.models import ChannelMessage, Incident, IncidentDraft, Technician, TechnicianChannel
from incidents.services.incident_processor import IncidentProcessor
from incidents.services.incoming_message_service import ChannelNotAuthorizedError, IncomingMessageService


def update(user=101, chat=None, chat_type="private", text="Prioridad alta. Cámara 3 sin imagen.", number=1):
    return {"message": {"message_id": number, "from": {"id": user, "first_name": "Reportante QA"},
                        "chat": {"id": user if chat is None else chat, "type": chat_type}, "text": text}}


@override_settings(DEBUG=True, TELEGRAM_AUTO_REGISTER=True, AI_PROVIDER="mock")
class HTTPEntrySecurityTests(TestCase):
    def payload(self):
        return {"provider": "telegram", "external_user_id": "101", "external_chat_id": "101",
                "external_message_id": "101:1", "text": "confirmo", "trusted_telegram": True,
                **update(text="confirmo")}

    def assert_no_data(self):
        for model in (Technician, TechnicianChannel, ChannelMessage, IncidentDraft, Incident):
            self.assertEqual(model.objects.count(), 0)

    def test_anonymous_test_route_cannot_register_or_process(self):
        with patch("incidents.views.IncomingMessageService") as service:
            response = self.client.post("/api/v1/messages/test/", self.payload(), content_type="application/json")
        self.assertIn(response.status_code, (401, 403))
        service.assert_not_called()
        self.assert_no_data()

    def test_nonstaff_test_route_rejected(self):
        self.client.force_login(get_user_model().objects.create_user(username="viewer"))
        self.assertEqual(self.client.post("/api/v1/messages/test/", self.payload(), content_type="application/json").status_code, 403)
        self.assert_no_data()

    @override_settings(DEBUG=False)
    def test_test_route_disabled_even_for_staff_outside_debug(self):
        self.client.force_login(get_user_model().objects.create_user(username="staff", is_staff=True))
        with patch("incidents.views.IncomingMessageService") as service:
            response = self.client.post("/api/v1/messages/test/", self.payload(), content_type="application/json")
        self.assertEqual(response.status_code, 404)
        service.assert_not_called()
        self.assert_no_data()

    def test_staff_http_payload_cannot_grant_trusted_autoregistration(self):
        self.client.force_login(get_user_model().objects.create_user(username="staff", is_staff=True))
        self.assertEqual(self.client.post("/api/v1/messages/test/", self.payload(), content_type="application/json").status_code, 404)
        self.assert_no_data()

    def test_staff_debug_tool_preserved_for_known_account(self):
        reporter = Technician.objects.create(full_name="QA")
        TechnicianChannel.objects.create(technician=reporter, provider="telegram", external_user_id="101", external_chat_id="101")
        self.client.force_login(get_user_model().objects.create_user(username="staff", is_staff=True))
        payload = {**self.payload(), "text": "hola"}
        response = self.client.post("/api/v1/messages/test/", payload, content_type="application/json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Technician.objects.count(), 1)
        self.assertEqual(ChannelMessage.objects.get().technician, reporter)
        self.assertEqual(Incident.objects.count(), 0)

    def test_staff_tool_requires_csrf_for_session_authentication(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(get_user_model().objects.create_user(username="staff", is_staff=True))
        self.assertEqual(client.post("/api/v1/messages/test/", self.payload(), content_type="application/json").status_code, 403)
        self.assert_no_data()

    def test_webhook_disabled_with_missing_or_forged_credentials(self):
        for header in (None, "forged-secret"):
            with self.subTest(header=header), patch("incidents.messaging.telegram_update_handler.TelegramUpdateHandler.handle") as handle:
                kwargs = {} if header is None else {"HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN": header}
                response = self.client.post("/api/v1/messaging/telegram/webhook/", update(text="confirmo"), content_type="application/json", **kwargs)
                self.assertEqual(response.status_code, 404)
                handle.assert_not_called()
                self.assert_no_data()


@override_settings(DEBUG=True, TELEGRAM_AUTO_REGISTER=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", INCIDENT_NOTIFICATION_EMAIL="qa@example.invalid")
class TelegramIdentityTests(TestCase):
    def setUp(self):
        self.ai = Mock()
        self.ai.extract_incident.return_value = IncidentExtractionResult(
            unit="Estudio 2", area="Producción", location_context="Estudio 2", asset_category="video_cameras",
            equipment="Cámara 3", failure_type="Sin imagen", description="La Cámara 3 no tiene imagen",
            operational_impact="No podemos grabar", priority="high",
        )
        self.service = IncomingMessageService(IncidentProcessor(self.ai, require_delivered_summary=True))
        self.messaging = Mock()
        self.message_number = 100

        def send(recipient, text):
            self.message_number += 1
            return {"result": {"message_id": self.message_number}}

        self.messaging.send_text.side_effect = send
        self.handler = TelegramUpdateHandler(messaging_provider=self.messaging, incoming_service=self.service)

    def test_polling_private_autoregistration_and_confirmation_preserved(self):
        first = self.handler.handle(update())
        self.assertFalse(first["incident_created"])
        channel = TechnicianChannel.objects.get(external_user_id="101")
        self.assertEqual(channel.external_chat_id, "101")
        self.assertEqual(get_user_model().objects.count(), 0)
        self.assertEqual(IncidentDraft.objects.get().technician, channel.technician)
        result = self.handler.handle(update(text="confirmo", number=2))
        self.assertTrue(result["incident_created"])
        self.assertEqual(Incident.objects.get().technician, channel.technician)
        self.assertEqual(Incident.objects.count(), 1)
        duplicate = self.handler.handle(update(text="confirmo", number=2))
        self.assertEqual(duplicate["action"], "duplicate_message")
        self.assertEqual(Incident.objects.count(), 1)

    def test_groups_channels_and_mismatched_private_identity_are_ignored(self):
        self.handler.handle(update())
        draft = IncidentDraft.objects.get()
        before = dict(draft.extracted_data)
        sent = self.messaging.send_text.call_count
        count = ChannelMessage.objects.count()
        for chat_type, chat in (("group", -1), ("supergroup", -2), ("channel", -3), ("private", 202)):
            for text in ("confirmo", "/cancelar", "/start", "Cambio el equipo"):
                with self.subTest(chat_type=chat_type, text=text):
                    incoming = update(chat=chat, chat_type=chat_type, text=text, number=2)
                    self.assertEqual(self.handler.handle(incoming)["action"], "ignored")
                    self.assertEqual(self.handler.send_processing_notice(incoming)["action"], "processing_notice_ignored")
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data, before)
        self.assertEqual(draft.status, "ready")
        self.assertEqual(ChannelMessage.objects.count(), count)
        self.assertEqual(self.messaging.send_text.call_count, sent)
        self.assertEqual(Incident.objects.count(), 0)

    def test_unknown_group_cannot_autoregister(self):
        self.assertEqual(self.handler.handle(update(chat_type="supergroup", chat=-1))["action"], "ignored")
        self.assertEqual(Technician.objects.count(), 0)
        self.messaging.send_text.assert_not_called()

    def test_two_reporters_keep_separate_drafts_and_confirmations(self):
        self.handler.handle(update(user=101))
        self.handler.handle(update(user=202, text="confirmo"))
        self.assertEqual(Incident.objects.count(), 0)
        self.handler.handle(update(user=202, number=2))
        a = IncidentDraft.objects.get(technician__channels__external_user_id="101")
        b = IncidentDraft.objects.get(technician__channels__external_user_id="202")
        self.handler.handle(update(user=202, number=3, text="confirmo"))
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.status, "ready")
        self.assertEqual(b.status, "completed")
        self.assertEqual(Incident.objects.get().technician, b.technician)
        self.handler.handle(update(user=101, number=2, text="/cancelar"))
        a.refresh_from_db()
        self.assertEqual(a.status, "cancelled")
        self.assertEqual(Incident.objects.count(), 1)

    def test_registered_chat_mismatch_blocks_commands_and_messages(self):
        reporter = Technician.objects.create(full_name="QA")
        TechnicianChannel.objects.create(technician=reporter, provider="telegram", external_user_id="101", external_chat_id="202")
        for text in ("confirmo", "/cancelar", "/start"):
            self.assertEqual(self.handler.handle(update(text=text))["action"], "ignored")
        self.assertEqual(ChannelMessage.objects.count(), 0)
        self.messaging.send_text.assert_not_called()

    def test_legacy_blank_chat_keeps_valid_private_conversation(self):
        reporter = Technician.objects.create(full_name="QA")
        TechnicianChannel.objects.create(technician=reporter, provider="telegram", external_user_id="101")
        self.handler.handle(update())
        self.assertEqual(IncidentDraft.objects.get().technician, reporter)

    def test_service_rejects_forged_identity_and_untrusted_registration(self):
        for trusted, payload in ((False, update()), (True, update(chat=202)), (True, update(user=202))):
            with self.subTest(trusted=trusted, payload=payload), self.assertRaises(ChannelNotAuthorizedError):
                self.service.process_text(provider="telegram", external_user_id="101", external_chat_id="101",
                                          external_message_id="101:1", text="confirmo", raw_payload=payload, trusted_telegram=trusted)
        self.assertEqual(Technician.objects.count(), 0)
        self.ai.extract_incident.assert_not_called()

    def test_duplicate_id_cannot_return_another_reporters_message(self):
        self.handler.handle(update(user=101))
        self.handler.handle(update(user=202))
        with self.assertRaises(ChannelNotAuthorizedError):
            self.service.process_text(provider="telegram", external_user_id="202", external_chat_id="202",
                                      external_message_id="101:1", text="confirmo", raw_payload={})
        self.assertEqual(Incident.objects.count(), 0)
        self.assertEqual(IncidentDraft.objects.filter(status="ready").count(), 2)
