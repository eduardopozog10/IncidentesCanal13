import logging

from django.utils import timezone
from django.db import transaction

from incidents.messaging.factory import get_messaging_provider
from incidents.models import ChannelMessage, IncidentDraft, TechnicianChannel
from incidents.services.incident_draft_service import (
    IncidentDraftService,
)
from incidents.services.incident_processor import IncidentProcessor
from incidents.services.conversation_lock import conversation_lock
from incidents.messaging.telegram_provider import TelegramRejectedError
from incidents.services.incoming_message_service import (
    ChannelNotAuthorizedError,
    IncomingMessageService,
)

logger = logging.getLogger(__name__)


class TelegramUpdateHandler:
    PROVIDER = "telegram"

    PROCESSING_NOTICE = (
        "El procesamiento está tomando un poco más de lo habitual. "
        "Tu mensaje quedó guardado y se retomará automáticamente; no es necesario que lo envíes "
        "nuevamente."
    )

    def __init__(
        self,
        messaging_provider=None,
        incoming_service=None,
        draft_service=None,
    ):
        self.messaging_provider = (
            messaging_provider
            or get_messaging_provider(self.PROVIDER)
        )
        self.incoming_service = (
            incoming_service
            or IncomingMessageService(IncidentProcessor(require_delivered_summary=True))
        )
        self.draft_service = (
            draft_service
            or IncidentDraftService()
        )

    def queue_update(self, update):
        return self.handle(update, receive_only=True)

    def handle(self, update: dict, *, receive_only=False, processing_text=None, group_members=None) -> dict:
        # Este handler se utiliza solo desde el receptor de polling. El
        # webhook HTTP está deshabilitado; un JSON por sí solo no es confiable.
        if not self._is_admitted_private_update(update):
            return {"action": "ignored", "reason": "Contexto de conversación no admitido."}
        telegram_message = update.get("message")

        if telegram_message is None:
            return {
                "action": "ignored",
                "reason": "La actualización no contiene un mensaje.",
            }

        sender = telegram_message.get("from", {})
        chat = telegram_message.get("chat", {})

        external_user_id = str(sender.get("id", "")).strip()
        external_chat_id = str(chat.get("id", "")).strip()
        telegram_message_id = telegram_message.get("message_id")
        text = telegram_message.get("text")

        if (
            not external_user_id
            or not external_chat_id
            or telegram_message_id is None
        ):
            return {
                "action": "ignored",
                "reason": "El mensaje no contiene identificadores válidos.",
            }

        if not isinstance(text, str) or not text.strip():
            self.messaging_provider.send_text(
                external_chat_id,
                (
                    "Por ahora solo puedo recibir reportes "
                    "escritos en texto."
                ),
            )

            return {
                "action": "unsupported_message",
            }

        external_message_id = (
            f"{external_chat_id}:{telegram_message_id}"
        )

        if not receive_only and processing_text is None:
            stored = ChannelMessage.objects.filter(provider=self.PROVIDER, external_message_id=external_message_id,
                direction="inbound").first()
            if stored and (stored.processing_result.get("_group_member_ids") or stored.processing_result.get("_queued_for_group")):
                from incidents.services.telegram_message_groups import TelegramMessageGroups
                result = TelegramMessageGroups(self).process_reporter(stored.technician_id)
                return {"action": "duplicate_message", "durable": True} if result["action"] == "group_empty" else result

        try:
            result = self.incoming_service.process_text(
                provider=self.PROVIDER,
                external_user_id=external_user_id,
                external_chat_id=external_chat_id,
                external_message_id=external_message_id,
                text=text,
                raw_payload=update,
                trusted_telegram=True,
                receive_only=receive_only,
                processing_text=processing_text,
                command_handler=self._process_command if (processing_text or text) in {"/start", "/cancelar"} else None,
            )
        except ChannelNotAuthorizedError:
            self.messaging_provider.send_text(
                external_chat_id,
                (
                    "Tu cuenta de Telegram no está autorizada "
                    "para registrar incidentes."
                ),
            )

            return {
                "action": "unauthorized_user",
            }
        except Exception:
            self.messaging_provider.send_text(
                external_chat_id,
                (
                    "No fue posible procesar el reporte en este "
                    "momento. Inténtalo nuevamente más tarde."
                ),
            )

            return {
                "action": "processing_failed",
            }

        incoming_message = result["message"]

        if receive_only:
            return {"action": "queued", "message_id": incoming_message.pk, "durable": True}

        if result.get("pending"):
            self.send_processing_notice(update)
            return {"action": "processing_pending", "message_id": incoming_message.pk, "durable": True}

        if result["duplicate"] and not (result["processing"] or result.get("cached_processing")):
            return {
                "action": "duplicate_message",
                "message_id": incoming_message.pk,
            }

        processing_result = result["processing"] or result.get("cached_processing")
        if processing_result.get("action") == "grouped_message":
            return {"action": "duplicate_message", "message_id": incoming_message.pk, "durable": True}
        if group_members:
            with transaction.atomic():
                ChannelMessage.objects.filter(pk__in=group_members).exclude(pk=incoming_message.pk).update(
                    status="processed", processed_at=timezone.now(), draft_id=incoming_message.draft_id,
                    incident_id=incoming_message.incident_id,
                    processing_result={"action": "grouped_message", "incident_created": False, "_group_leader_id": incoming_message.pk},
                )
            if not processing_result.get("incident_created") and self._has_new_group_input(incoming_message, group_members):
                outgoing, _ = ChannelMessage.objects.get_or_create(in_reply_to=incoming_message, reply_purpose="reply",
                    defaults=dict(provider=self.PROVIDER, direction="outbound", external_sender_id="bot",
                        external_chat_id=incoming_message.external_chat_id, technician=incoming_message.technician,
                        channel_account=incoming_message.channel_account, draft=incoming_message.draft,
                        content=processing_result["reply"], status="failed", processing_attempts=3,
                        error_message="SupersededByNewerMessage"))
                if outgoing.status not in ("sent", "uncertain", "processing"):
                    outgoing.status, outgoing.processing_attempts = "failed", 3
                    outgoing.error_message = "SupersededByNewerMessage"
                    outgoing.save(update_fields=["status", "processing_attempts", "error_message"])
                return {"action": "group_superseded", "message_id": incoming_message.pk, "durable": True}
        reply_text = processing_result["reply"]
        if processing_result["action"] == "conversation":
            # También proteger replays de resultados de IA cacheados antes de
            # esta corrección. No reinterpretarlos como una creación retroactiva.
            processor = self.incoming_service.incident_processor
            draft = IncidentDraft.objects.filter(
                technician_id=incoming_message.technician_id,
                status__in=["ready", "collecting"],
            ).first()
            reply_text = (
                processor._get_quick_conversation_reply(incoming_message.content)
                or processor._conversation_reply(draft)
            )

        outgoing_message, delivered = self._deliver_reply(incoming_message, reply_text)
        if not delivered:
            return {"action": "reply_failed", "message_id": incoming_message.pk, "durable": True}
        if result["duplicate"]:
            return {"action": "duplicate_message", "message_id": incoming_message.pk, "durable": True}

        return {
            "action": processing_result["action"],
            "incoming_message_id": incoming_message.pk,
            "outgoing_message_id": outgoing_message.pk,
            "incident_created": processing_result[
                "incident_created"
            ],
        }

    @staticmethod
    def _has_new_group_input(incoming, members):
        return ChannelMessage.objects.filter(technician_id=incoming.technician_id,
            channel_account_id=incoming.channel_account_id, direction="inbound", received_via_polling=True,
            pk__gt=max(members), status__in=["received", "processing", "failed"]).exists()

    def _deliver_reply(self, incoming, text, purpose="reply"):
        with conversation_lock(f"reply:{incoming.pk}:{purpose}"):
            outgoing, _ = ChannelMessage.objects.get_or_create(
                in_reply_to=incoming, reply_purpose=purpose,
                defaults=dict(provider=self.PROVIDER, external_sender_id="bot",
                              external_chat_id=incoming.external_chat_id, channel_account=incoming.channel_account,
                              technician=incoming.technician, incident=incoming.incident, draft=incoming.draft,
                              direction="outbound", message_type="text", content=text, status="received"),
            )
            if outgoing.status == "sent":
                return outgoing, True
            done = outgoing.raw_payload.get("parts_done", [])
            if (purpose == "reply" and incoming.processing_result.get("action") == "conversation"
                    and outgoing.status in ("received", "failed") and not done and outgoing.content != text):
                outgoing.content, outgoing.raw_payload = text, {}
                outgoing.save(update_fields=["content", "raw_payload"])
            all_done = bool(done) and len(done) == outgoing.raw_payload.get("parts_total")
            if outgoing.status == "processing" and not all_done:
                outgoing.status = "uncertain"
                outgoing.error_message = "InterruptedDelivery"
                outgoing.save(update_fields=["status", "error_message"])
                return outgoing, False
            if outgoing.status == "uncertain" or (outgoing.processing_attempts >= 3 and not all_done):
                return outgoing, False
            outgoing.status = "processing"
            outgoing.processing_attempts += 1
            outgoing.save(update_fields=["status", "processing_attempts"])
            try:
                response = self._send_complete_reply(incoming.external_chat_id, outgoing.content, outgoing)
                outgoing.external_message_id = f"{incoming.external_chat_id}:{response['result']['message_id']}"
                outgoing.status = "sent"
                outgoing.processed_at = timezone.now()
                outgoing.save(update_fields=["external_message_id", "status", "processed_at"])
                logger.info("telegram.reply_sent message=%s reply=%s purpose=%s", incoming.pk, outgoing.pk, purpose)
                return outgoing, True
            except TelegramRejectedError as error:
                outgoing.status = "failed"
                if not error.retryable:
                    outgoing.processing_attempts = 3
                outgoing.error_message = "TelegramRejectedError"
                outgoing.save(update_fields=["status", "processing_attempts", "error_message"])
            except Exception as error:
                # Mantener PROCESSING: pudo llegar la parte que no tiene acuse.
                ChannelMessage.objects.filter(pk=outgoing.pk).update(status="uncertain", error_message=type(error).__name__)
            return outgoing, False

    def _send_complete_reply(self, recipient_id, text, outgoing=None):
        """Entregar resúmenes extensos completos antes de registrar SENT.

        Margen bajo 4096; cuenta UTF-16 también para caracteres como emojis.
        Si falla una parte, el handler registra FAILED para el resumen lógico.
        """
        remaining = text
        parts = []
        while remaining:
            units = 0
            end = 0
            for character in remaining:
                size = 2 if ord(character) > 0xFFFF else 1
                if units + size > 4000:
                    break
                units += size
                end += 1
            if end < len(remaining):
                boundary = remaining.rfind("\n", 0, end)
                if boundary < end // 2:
                    boundary = remaining.rfind(" ", 0, end)
                if boundary >= end // 2:
                    end = boundary
            part = remaining[:end].strip()
            remaining = remaining[end:].lstrip()
            if part:
                parts.append(part)
        responses = list(outgoing.raw_payload.get("parts_done", [])) if outgoing else []
        if outgoing:
            outgoing.raw_payload = {"parts_total": len(parts), "parts_done": responses}
            outgoing.save(update_fields=["raw_payload"])
        for part in parts[len(responses):]:
            response = self.messaging_provider.send_text(recipient_id, part)
            responses.append(response)
            if outgoing:
                outgoing.raw_payload = {"parts_total": len(parts), "parts_done": list(responses)}
                outgoing.save(update_fields=["raw_payload"])
        response = dict(responses[-1])
        if len(responses) > 1:
            response["delivery_parts"] = [item["result"]["message_id"] for item in responses]
        return response

    def send_processing_notice(
        self,
        update: dict,
    ) -> dict:
        if not self._is_admitted_private_update(update):
            return {"action": "processing_notice_ignored"}
        telegram_message = update.get("message")

        if telegram_message is None:
            return {
                "action": "processing_notice_ignored",
            }

        chat = telegram_message.get("chat", {})

        external_chat_id = str(
            chat.get("id", "")
        ).strip()

        telegram_message_id = telegram_message.get(
            "message_id"
        )

        text = telegram_message.get("text")

        if (
            not external_chat_id
            or telegram_message_id is None
            or not isinstance(text, str)
            or not text.strip()
            or text in {"/start", "/cancelar"}
        ):
            return {
                "action": "processing_notice_ignored",
            }

        external_message_id = (
            f"{external_chat_id}:{telegram_message_id}"
        )

        incoming_message = (
            ChannelMessage.objects.select_related(
                "channel_account",
                "technician",
                "incident",
                "draft",
            )
            .filter(
                provider=self.PROVIDER,
                external_message_id=external_message_id,
                direction=ChannelMessage.Direction.INBOUND,
            )
            .first()
        )

        if incoming_message is None:
            return {
                "action": "processing_notice_ignored",
            }

        if incoming_message.status == ChannelMessage.Status.PROCESSED or incoming_message.processing_attempts >= 3:
            return {
                "action": "processing_notice_ignored",
            }

        reply_text = self.PROCESSING_NOTICE
        if (
            incoming_message.error_message
            and self.incoming_service.incident_processor._is_confirmation(incoming_message.content)
        ):
            reply_text = (
                "El registro de la incidencia todavía no está confirmado. "
                "Guardé tu mensaje para reintentar; no necesitas repetir la confirmación."
            )

        outgoing_message, delivered = self._deliver_reply(incoming_message, reply_text, "notice")
        if not delivered:
            return {"action": "processing_notice_failed", "message_id": incoming_message.pk}

        return {
            "action": "processing_notice_sent",
            "incoming_message_id": incoming_message.pk,
            "outgoing_message_id": outgoing_message.pk,
        }

    def recover_pending(self, limit=10):
        # Solo actualizaciones durables recibidas por el handler privado. Los
        # registros históricos sin contexto verificable no se reproducen.
        inbound = IncomingMessageService.recoverable_messages().filter(provider=self.PROVIDER).order_by("pk")
        results = []
        deferred_reporters = set()
        for message in inbound.iterator(chunk_size=100):
            if len(results) >= limit:
                break
            if message.technician_id in deferred_reporters:
                continue
            if message.processing_result.get("_group_member_ids") or message.processing_result.get("_queued_for_group"):
                from incidents.services.telegram_message_groups import TelegramMessageGroups
                result = TelegramMessageGroups(self).process_reporter(message.technician_id)
                results.append(result)
                deferred_reporters.add(message.technician_id)
                continue
            if (message.processing_attempts < self.incoming_service.MAX_PROCESSING_ATTEMPTS
                    and not self.incoming_service.retry_is_due(message)):
                deferred_reporters.add(message.technician_id)
                continue
            payload = message.raw_payload.get("message", {})
            canonical_id = f"{message.external_chat_id}:{payload.get('message_id')}" if isinstance(payload, dict) else ""
            if (isinstance(payload, dict) and message.external_message_id == canonical_id and payload.get("text") == message.content
                    and self._is_admitted_private_update(message.raw_payload)):
                # La posesión del lock identifica una ejecución activa. No
                # adivinar por antigüedad ni esperar a otro receptor.
                with conversation_lock(f"reporter:{message.technician_id}", wait=False) as acquired:
                    if not acquired:
                        deferred_reporters.add(message.technician_id)
                        continue
                    self.send_processing_notice(message.raw_payload)
                    result = self.handle(message.raw_payload)
                    results.append(result)
                    if result.get("action") == "processing_pending":
                        deferred_reporters.add(message.technician_id)
        return results

    def _is_admitted_private_update(self, update):
        if not isinstance(update, dict) or not isinstance(update.get("message"), dict):
            return False
        message = update["message"]
        sender, chat = message.get("from"), message.get("chat")
        if not isinstance(sender, dict) or not isinstance(chat, dict):
            return False
        user_id, chat_id = str(sender.get("id", "")), str(chat.get("id", ""))
        if not IncomingMessageService.is_private_telegram_context(update, user_id, chat_id):
            return False
        account = TechnicianChannel.objects.filter(provider=self.PROVIDER, external_user_id=user_id).first()
        return account is None or not account.external_chat_id or account.external_chat_id == chat_id

    def _process_command(self, message):
        if getattr(message, "processing_text", message.content) == "/start":
            return {
                "action": "welcome_sent", "incident_created": False,
                "reply": (
                    "Hola. Soy el asistente de Gestión de Incidencias "
                    "Operativas de Secuoya Chile.\n\n"
                    "Cuéntame qué está fallando, dónde ocurre y cómo "
                    "afecta la operación. Puedes dar varios datos juntos "
                    "o enviarlos de a poco; te preguntaré lo que falte.\n\n"
                    "Antes de registrar el incidente revisaremos un "
                    "resumen y te pediré confirmación. Para cancelar "
                    "un reporte pendiente, escribe /cancelar."
                ),
            }
        draft = self.draft_service.cancel_active_draft(technician=message.technician)
        message.draft = draft
        return {
            "action": "draft_cancelled" if draft else "no_active_draft", "incident_created": False,
            "draft_id": draft.pk if draft else None,
            "reply": "El reporte pendiente fue cancelado." if draft else "No tienes ningún reporte pendiente.",
        }
