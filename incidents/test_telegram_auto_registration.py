from django.test import TestCase, override_settings

from incidents.models import Technician, TechnicianChannel
from incidents.services.incoming_message_service import (
    ChannelNotAuthorizedError,
    IncomingMessageService,
)


@override_settings(
    DEBUG=True,
    TELEGRAM_AUTO_REGISTER=True,
    AI_PROVIDER="mock",
)
class TelegramAutoRegistrationTests(TestCase):
    def setUp(self):
        self.service = IncomingMessageService()
        self.user_id = "987654321"

    def send_message(
        self,
        message_id,
        *,
        chat_type="private",
    ):
        return self.service.process_text(
            provider="telegram",
            external_user_id=self.user_id,
            external_chat_id=self.user_id,
            external_message_id=message_id,
            text="Hola, tengo un problema con una cámara",
            raw_payload={
                "message": {
                    "message_id": 1,
                    "from": {
                        "id": int(self.user_id),
                        "first_name": "Usuario",
                        "last_name": "Prueba",
                        "username": "usuario_prueba",
                    },
                    "chat": {
                        "id": int(self.user_id),
                        "type": chat_type,
                    },
                    "text": (
                        "Hola, tengo un problema con una cámara"
                    ),
                }
            },
        )

    def test_registers_new_telegram_user_automatically(self):
        result = self.send_message("auto-register-1")

        self.assertFalse(result["duplicate"])

        channel = TechnicianChannel.objects.get(
            provider="telegram",
            external_user_id=self.user_id,
        )

        self.assertEqual(
            channel.technician.full_name,
            "Usuario Prueba",
        )

        self.assertEqual(
            channel.external_chat_id,
            self.user_id,
        )

        self.assertEqual(
            channel.username,
            "usuario_prueba",
        )

        self.assertTrue(channel.is_active)
        self.assertTrue(channel.technician.is_active)

    def test_does_not_duplicate_existing_user(self):
        self.send_message("auto-register-1")
        self.send_message("auto-register-2")

        self.assertEqual(
            Technician.objects.count(),
            1,
        )

        self.assertEqual(
            TechnicianChannel.objects.count(),
            1,
        )

    def test_does_not_reactivate_inactive_channel(self):
        reporter = Technician.objects.create(
            full_name="Usuario bloqueado",
            is_active=True,
        )

        TechnicianChannel.objects.create(
            technician=reporter,
            provider="telegram",
            external_user_id=self.user_id,
            external_chat_id=self.user_id,
            is_active=False,
        )

        with self.assertRaises(ChannelNotAuthorizedError):
            self.send_message("blocked-channel")

        self.assertEqual(
            TechnicianChannel.objects.count(),
            1,
        )

    def test_does_not_reactivate_inactive_reporter(self):
        reporter = Technician.objects.create(
            full_name="Reportante inactivo",
            is_active=False,
        )

        TechnicianChannel.objects.create(
            technician=reporter,
            provider="telegram",
            external_user_id=self.user_id,
            external_chat_id=self.user_id,
            is_active=True,
        )

        with self.assertRaises(ChannelNotAuthorizedError):
            self.send_message("blocked-reporter")

        reporter.refresh_from_db()

        self.assertFalse(reporter.is_active)

    def test_does_not_auto_register_group_messages(self):
        with self.assertRaises(ChannelNotAuthorizedError):
            self.send_message(
                "group-message",
                chat_type="supergroup",
            )

        self.assertEqual(
            Technician.objects.count(),
            0,
        )

        self.assertEqual(
            TechnicianChannel.objects.count(),
            0,
        )

    @override_settings(TELEGRAM_AUTO_REGISTER=False)
    def test_auto_registration_can_be_disabled(self):
        with self.assertRaises(ChannelNotAuthorizedError):
            self.send_message("disabled-registration")

        self.assertEqual(
            Technician.objects.count(),
            0,
        )

    @override_settings(DEBUG=False)
    def test_auto_registration_is_disabled_in_production(self):
        with self.assertRaises(ChannelNotAuthorizedError):
            self.send_message("production-registration")

        self.assertEqual(
            Technician.objects.count(),
            0,
        )