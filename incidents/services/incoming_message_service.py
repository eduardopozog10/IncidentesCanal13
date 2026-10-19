
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone

from incidents.models import (
    ChannelMessage,
    Technician,
    TechnicianChannel,
)
from incidents.services.incident_processor import IncidentProcessor
from incidents.services.conversation_lock import conversation_lock


class ChannelNotAuthorizedError(Exception):
    """El usuario no está asociado a un reportante activo."""


class IncomingMessageService:
    MAX_PROCESSING_ATTEMPTS = 3

    @staticmethod
    def _failure_reply():
        return {"action": "processing_failed", "incident_created": False,
                "reply": "No pude completar el procesamiento tras varios intentos. El mensaje quedó conservado para revisión; no se registró una nueva incidencia."}

    @staticmethod
    def recoverable_messages():
        final_reply = ChannelMessage.objects.filter(in_reply_to_id=OuterRef("pk"), reply_purpose="reply").filter(
            Q(status__in=["sent", "uncertain"]) | Q(status="failed", processing_attempts__gte=3)
        )
        return ChannelMessage.objects.filter(
            direction="inbound", received_via_polling=True,
            channel_account__is_active=True, technician__is_active=True,
            channel_account__technician_id=F("technician_id"),
        ).annotate(final_reply=Exists(final_reply)).filter(
            Q(status="processing")
            | Q(status__in=["received", "failed"], processing_attempts__lt=3)
            | Q(processing_result__has_key="reply", final_reply=False)
        )
    @staticmethod
    def is_private_telegram_context(raw_payload, external_user_id, external_chat_id):
        """Validar el contexto, no autenticar JSON: solo usar tras polling."""
        if not isinstance(raw_payload, dict):
            return False
        message = raw_payload.get("message")
        if not isinstance(message, dict):
            return False
        sender, chat = message.get("from"), message.get("chat")
        return (
            isinstance(sender, dict) and isinstance(chat, dict)
            and chat.get("type") == "private"
            and not sender.get("is_bot", False)
            and bool(external_user_id)
            and str(sender.get("id", "")) == external_user_id
            and str(chat.get("id", "")) == external_chat_id
            and external_user_id == external_chat_id
        )

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
        existing = TechnicianChannel.objects.select_related("technician").filter(
            provider=TechnicianChannel.Provider.TELEGRAM,
            external_user_id=external_user_id,
        ).first()

        if existing is not None:
            # Otro hilo pudo registrar esta misma identidad después de la
            # primera consulta. No confundir esa carrera con una cuenta bloqueada.
            return existing if existing.is_active and existing.technician.is_active else None

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
        trusted_telegram: bool = False,
        command_handler=None,
    ) -> dict:
        # Esta capacidad se concede desde Python por el receptor de polling;
        # nunca se toma del cuerpo de una solicitud HTTP.
        if trusted_telegram and (
            provider != TechnicianChannel.Provider.TELEGRAM
            or not self.is_private_telegram_context(raw_payload, external_user_id, external_chat_id)
        ):
            raise ChannelNotAuthorizedError("Contexto de Telegram no admitido.")
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
            and trusted_telegram
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

        if (provider == TechnicianChannel.Provider.TELEGRAM
                and channel_account.external_chat_id
                and channel_account.external_chat_id != external_chat_id):
            raise ChannelNotAuthorizedError("El chat no corresponde al canal del reportante.")

        message, created = ChannelMessage.objects.get_or_create(
            provider=provider,
            external_message_id=external_message_id,
            defaults={
                "received_via_polling": trusted_telegram,
                "external_sender_id": external_user_id,
                "external_chat_id": external_chat_id,
                "channel_account": channel_account,
                "technician": channel_account.technician,
                "direction": ChannelMessage.Direction.INBOUND,
                "message_type": ChannelMessage.MessageType.TEXT,
                "content": text,
                "raw_payload": raw_payload,
                "status": ChannelMessage.Status.RECEIVED,
            },
        )
        with conversation_lock(f"reporter:{channel_account.technician_id}"):
            message.refresh_from_db()
            self._validate_message_owner(message, channel_account, external_chat_id)
            # Reutilizar siempre el contenido persistido, no el cuerpo de un replay.
            incomplete_timeout = trusted_telegram and message.processing_result.get("action") == "ai_timeout"
            if message.status == ChannelMessage.Status.PROCESSED and not incomplete_timeout:
                return {"duplicate": True, "message": message, "processing": None,
                        "cached_processing": message.processing_result or None}
            if message.incident_id:
                # Compatibilidad con un registro histórico sin resultado cacheado.
                result = {"action": "incident_created", "incident_created": True,
                          "incident_id": message.incident_id, "incident_code": message.incident.code,
                          "reply": f"Reporte {message.incident.code} creado correctamente."}
                ChannelMessage.objects.filter(pk=message.pk).update(status="processed", processing_result=result)
                return {"duplicate": True, "message": message, "processing": result}
            if message.processing_attempts >= self.MAX_PROCESSING_ATTEMPTS:
                if message.status == "processing" or incomplete_timeout:
                    result = self._failure_reply()
                    ChannelMessage.objects.filter(pk=message.pk).update(status="failed", processing_result=result, error_message="InterruptedProcessing")
                    return {"duplicate": False, "message": message, "processing": result}
                return {"duplicate": True, "message": message, "processing": message.processing_result or None}
            if trusted_telegram and self.recoverable_messages().filter(
                technician=message.technician, pk__lt=message.pk,
            ).exists():
                return {"duplicate": not created, "message": message, "processing": None, "pending": True}
            message.processing_attempts += 1
            message.status = ChannelMessage.Status.PROCESSING
            message.save(update_fields=["processing_attempts", "status"])
            try:
                if command_handler is not None:
                    with transaction.atomic():
                        result = command_handler(message)
                        message.processing_result = result
                        message.status = "processed"
                        message.processed_at = timezone.now()
                        message.save(update_fields=["draft", "processing_result", "status", "processed_at"])
                else:
                    result = self.incident_processor.process(message)
                if trusted_telegram and result["action"] == "ai_timeout":
                    raise TimeoutError("Extracción temporalmente no disponible.")
            except Exception as error:
                # El intento atómico se revirtió o conservó datos pendientes de
                # timeout. Refrescar evita guardar referencias revertidas.
                message.refresh_from_db()
                message.status = ChannelMessage.Status.FAILED
                message.error_message = type(error).__name__
                result = None
                if trusted_telegram and message.processing_attempts >= self.MAX_PROCESSING_ATTEMPTS:
                    result = self._failure_reply()
                message.processing_result = result or {}
                message.save(update_fields=["status", "error_message", "processing_result"])
                if not trusted_telegram:
                    raise
                return {"duplicate": False, "message": message, "processing": result, "pending": result is None}
            return {"duplicate": False, "message": message, "processing": result}

    @staticmethod
    def _validate_message_owner(message, channel_account, external_chat_id):
        if (message.channel_account_id != channel_account.pk
                or message.technician_id != channel_account.technician_id
                or message.external_chat_id != external_chat_id
                or message.direction != ChannelMessage.Direction.INBOUND):
            raise ChannelNotAuthorizedError("El identificador del mensaje pertenece a otro contexto.")
