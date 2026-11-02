
import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone

from incidents.models import (
    ChannelMessage,
    Technician,
    TechnicianChannel,
)
from incidents.ai.base import AIConfigurationError
from incidents.services.incident_processor import IncidentProcessor
from incidents.services.conversation_lock import conversation_lock

logger = logging.getLogger(__name__)


class ChannelNotAuthorizedError(Exception):
    """El usuario no está asociado a un reportante activo."""


class IncomingMessageService:
    MAX_PROCESSING_ATTEMPTS = 3
    RETRY_DELAY_SECONDS = 30

    @classmethod
    def retry_delay(cls):
        return getattr(settings, "PILOT_MESSAGE_RETRY_SECONDS", cls.RETRY_DELAY_SECONDS)

    @staticmethod
    def retry_is_due(message):
        # El resultado temporal se guarda en el JSON existente. No es una
        # respuesta confirmada y sobrevive al reinicio del receptor.
        retry_at = message.processing_result.get("_retry_not_before", 0)
        return not isinstance(retry_at, (int, float)) or retry_at <= timezone.now().timestamp()

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
        ).exclude(processing_result__has_key="_group_leader_id").annotate(final_reply=Exists(final_reply)).filter(
            Q(status="processing")
            | (Q(status__in=["received", "failed"]) & ~Q(processing_result__has_key="reply"))
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
        receive_only=False,
        processing_text=None,
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
                "processing_result": {"_queued_for_group": True} if receive_only else {},
            },
        )
        self._validate_message_owner(message, channel_account, external_chat_id)
        if trusted_telegram:
            logger.info("telegram.message_persisted message=%s reporter=%s duplicate=%s", message.pk, message.technician_id, not created)
        if receive_only:
            return {"duplicate": not created, "message": message, "processing": None, "pending": True}
        with conversation_lock(f"reporter:{channel_account.technician_id}", wait=not trusted_telegram) as acquired:
            if not acquired:
                return {"duplicate": not created, "message": message, "processing": None, "pending": True}
            message.refresh_from_db()
            self._validate_message_owner(message, channel_account, external_chat_id)
            group_metadata = {key: value for key, value in message.processing_result.items() if key.startswith("_group_")}
            # Reutilizar siempre el contenido persistido, no el cuerpo de un replay.
            incomplete_timeout = trusted_telegram and message.processing_result.get("action") == "ai_timeout"
            if (message.status == ChannelMessage.Status.PROCESSED or "reply" in message.processing_result) and not incomplete_timeout:
                return {"duplicate": True, "message": message, "processing": None,
                        "cached_processing": message.processing_result or None}
            if message.incident_id:
                # Compatibilidad con un registro histórico sin resultado cacheado.
                result = {"action": "incident_created", "incident_created": True,
                          "incident_id": message.incident_id, "incident_code": message.incident.code,
                          "reply": f"Reporte {message.incident.code} creado correctamente."}
                result.update(group_metadata)
                ChannelMessage.objects.filter(pk=message.pk).update(status="processed", processing_result=result)
                return {"duplicate": True, "message": message, "processing": result}
            if message.processing_attempts >= self.MAX_PROCESSING_ATTEMPTS:
                if "reply" not in message.processing_result or incomplete_timeout:
                    result = self._failure_reply()
                    result.update(group_metadata)
                    ChannelMessage.objects.filter(pk=message.pk).update(status="failed", processing_result=result, error_message=message.error_message or "InterruptedProcessing")
                    return {"duplicate": False, "message": message, "processing": result}
                return {"duplicate": True, "message": message, "processing": message.processing_result or None}
            if trusted_telegram and not self.retry_is_due(message):
                return {"duplicate": not created, "message": message, "processing": None, "pending": True}
            if trusted_telegram and self.recoverable_messages().filter(
                technician=message.technician, pk__lt=message.pk,
            ).exists():
                return {"duplicate": not created, "message": message, "processing": None, "pending": True}
            message.processing_attempts += 1
            message.status = ChannelMessage.Status.PROCESSING
            if trusted_telegram:
                message.processing_result = {**group_metadata, "_retry_not_before": timezone.now().timestamp() + self.retry_delay()}
            message.save(update_fields=["processing_attempts", "status", "processing_result"])
            logger.info("telegram.processing_started message=%s attempt=%s", message.pk, message.processing_attempts)
            if processing_text is not None:
                message.processing_text = processing_text
            try:
                if command_handler is not None:
                    with transaction.atomic():
                        result = command_handler(message)
                        result.update(group_metadata)
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
                if trusted_telegram and (message.processing_attempts >= self.MAX_PROCESSING_ATTEMPTS or isinstance(error, AIConfigurationError)):
                    result = self._failure_reply()
                    if isinstance(error, AIConfigurationError):
                        result["reply"] = "No pude procesar el mensaje por un problema de configuración del proveedor. Quedó conservado para revisión; no se registró una nueva incidencia."
                delay = max(self.retry_delay(), getattr(error, "retry_after", 0))
                message.processing_result = {**(result or {"_retry_not_before": timezone.now().timestamp() + delay}), **group_metadata}
                message.save(update_fields=["status", "error_message", "processing_result"])
                logger.info("telegram.processing_deferred message=%s attempt=%s error=%s retry_after=%s", message.pk, message.processing_attempts, type(error).__name__, delay)
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
