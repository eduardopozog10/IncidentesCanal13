
from django.conf import settings
from django.db import IntegrityError, transaction

from incidents.models import (
    ChannelMessage,
    Technician,
    TechnicianChannel,
)
from incidents.services.incident_processor import IncidentProcessor


class ChannelNotAuthorizedError(Exception):
    """El usuario no está asociado a un reportante activo."""


class IncomingMessageService:
    def __init__(self, incident_processor=None):
        self.incident_processor = (
            incident_processor
            or IncidentProcessor()
        )

    def _auto_register_telegram_user(
        self,
        *,
        external_user_id: str,
        external_chat_id: str,
        raw_payload: dict,
    ):
        telegram_message = raw_payload.get("message", {})
        sender = telegram_message.get("from", {})
        chat = telegram_message.get("chat", {})

        # Permitir solamente conversaciones privadas.
        if chat.get("type") != "private":
            return None

        # Verificar que el ID corresponde al remitente real.
        if str(sender.get("id", "")) != external_user_id:
            return None

        # No reactivar automáticamente usuarios bloqueados.
        existing = TechnicianChannel.objects.filter(
            provider=TechnicianChannel.Provider.TELEGRAM,
            external_user_id=external_user_id,
        ).first()

        if existing is not None:
            return None

        first_name = str(sender.get("first_name") or "").strip()
        last_name = str(sender.get("last_name") or "").strip()

        full_name = " ".join(
            part for part in (first_name, last_name) if part
        )

        if not full_name:
            full_name = f"Reportante Telegram {external_user_id}"

        username = str(sender.get("username") or "").strip()

        try:
            with transaction.atomic():
                reporter = Technician.objects.create(
                    full_name=full_name[:150],
                    is_active=True,
                )

                channel_account = TechnicianChannel.objects.create(
                    technician=reporter,
                    provider=TechnicianChannel.Provider.TELEGRAM,
                    external_user_id=external_user_id,
                    external_chat_id=external_chat_id,
                    username=username[:150],
                    is_active=True,
                )

        except IntegrityError:
            # Evita crear dos cuentas si llegan mensajes simultáneos.
            channel_account = (
                TechnicianChannel.objects.select_related(
                    "technician"
                )
                .filter(
                    provider=TechnicianChannel.Provider.TELEGRAM,
                    external_user_id=external_user_id,
                    is_active=True,
                    technician__is_active=True,
                )
                .first()
            )

        return channel_account

    def process_text(
        self,
        *,
        provider: str,
        external_user_id: str,
        external_chat_id: str,
        external_message_id: str,
        text: str,
        raw_payload: dict,
    ) -> dict:
        channel_account = (
            TechnicianChannel.objects.select_related(
                "technician"
            )
            .filter(
                provider=provider,
                external_user_id=external_user_id,
                is_active=True,
                technician__is_active=True,
            )
            .first()
        )

        # Permitir el registro automático solamente durante el desarrollo.
        if (
            channel_account is None
            and provider == TechnicianChannel.Provider.TELEGRAM
            and settings.DEBUG
            and getattr(
                settings,
                "TELEGRAM_AUTO_REGISTER",
                False,
            )
        ):
            channel_account = self._auto_register_telegram_user(
                external_user_id=external_user_id,
                external_chat_id=external_chat_id,
                raw_payload=raw_payload,
            )

        if channel_account is None:
            raise ChannelNotAuthorizedError(
                "No existe un reportante activo asociado "
                "a este usuario y proveedor."
            )

        existing_message = ChannelMessage.objects.filter(
            provider=provider,
            external_message_id=external_message_id,
        ).first()

        if existing_message is not None:
            return {
                "duplicate": True,
                "message": existing_message,
                "processing": None,
            }

        message = ChannelMessage.objects.create(
            provider=provider,
            external_message_id=external_message_id,
            external_sender_id=external_user_id,
            external_chat_id=external_chat_id,
            channel_account=channel_account,
            technician=channel_account.technician,
            direction=ChannelMessage.Direction.INBOUND,
            message_type=ChannelMessage.MessageType.TEXT,
            content=text,
            raw_payload=raw_payload,
            status=ChannelMessage.Status.RECEIVED,
        )

        try:
            processing_result = (
                self.incident_processor.process(message)
            )
        except Exception as error:
            message.status = ChannelMessage.Status.FAILED
            message.error_message = str(error)
            message.save(
                update_fields=[
                    "status",
                    "error_message",
                ]
            )
            raise

        return {
            "duplicate": False,
            "message": message,
            "processing": processing_result,
        }
