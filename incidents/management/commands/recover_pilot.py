"""Inspección sin envíos por defecto; reintentos solo por opción explícita."""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from incidents.models import ChannelMessage, IncidentNotification, TechnicianChannel
from incidents.services.conversation_lock import conversation_lock
from incidents.services.incident_notification_service import send_incident_created_email


class Command(BaseCommand):
    help = "Lista pendientes del piloto. --run reanuda recuperables; los reenvíos inciertos requieren reconocimiento explícito."

    def add_arguments(self, parser):
        parser.add_argument("--run", action="store_true")
        parser.add_argument("--message-id", type=int)
        parser.add_argument("--reply-id", type=int)
        parser.add_argument("--notification-id", type=int)
        parser.add_argument("--acknowledge-duplicate-risk", action="store_true")

    def handle(self, *args, **options):
        choices = sum(bool(options[key]) for key in ("run", "message_id", "reply_id", "notification_id"))
        if choices > 1:
            raise CommandError("Selecciona una única operación.")
        if options["notification_id"]:
            notification = IncidentNotification.objects.get(pk=options["notification_id"])
            with conversation_lock(f"notification:{notification.incident_id}"):
                notification.refresh_from_db()
                if notification.status == "sent":
                    self.stdout.write("La notificación ya está enviada; no se repite.")
                    return
                if notification.status in ("sending", "uncertain") and not options["acknowledge_duplicate_risk"]:
                    raise CommandError("Verifica la entrega y reconoce el riesgo de duplicado antes de reenviar.")
                notification.status, notification.attempts = "pending", 0
                notification.save(update_fields=["status", "attempts"])
                self.stdout.write(f"Envío confirmado: {send_incident_created_email(notification.incident_id)}")
            return
        if choices:
            from incidents.messaging.telegram_update_handler import TelegramUpdateHandler
            handler = TelegramUpdateHandler()
            if options["run"]:
                results = handler.recover_pending()
                self.stdout.write(f"Mensajes revisados: {len(results)}")
                return
            if options["message_id"]:
                message = ChannelMessage.objects.get(pk=options["message_id"], direction="inbound")
                with conversation_lock(f"reporter:{message.technician_id}"):
                    message.refresh_from_db()
                    if not message.received_via_polling or not handler._is_admitted_private_update(message.raw_payload):
                        raise CommandError("Contexto no autorizado; no se reproduce.")
                    if message.status in ("failed", "processing") and message.processing_attempts >= 3:
                        if message.incident_id or ChannelMessage.objects.filter(technician_id=message.technician_id, direction="inbound", pk__gt=message.pk).exists():
                            raise CommandError("La conversación avanzó o existe una incidencia: requiere revisión manual, no reprocesamiento.")
                        with transaction.atomic():
                            for reply in message.replies.filter(reply_purpose="reply"):
                                reply.reply_purpose = f"{reply.pk:x}"
                                reply.save(update_fields=["reply_purpose"])
                            message.status, message.processing_attempts, message.processing_result = "received", 0, {}
                            message.save(update_fields=["status", "processing_attempts", "processing_result"])
                    self.stdout.write(handler.handle(message.raw_payload)["action"])
                return
            reply = ChannelMessage.objects.get(pk=options["reply_id"], direction="outbound", in_reply_to__isnull=False)
            with conversation_lock(f"reply:{reply.in_reply_to_id}:{reply.reply_purpose}"):
                reply.refresh_from_db()
                if reply.status == "sent":
                    self.stdout.write("La respuesta ya está enviada; no se repite.")
                    return
                if reply.status in ("processing", "uncertain") and not options["acknowledge_duplicate_risk"]:
                    raise CommandError("Verifica la entrega y reconoce el riesgo de duplicado antes de reenviar.")
                incoming = reply.in_reply_to
                if not incoming.received_via_polling or not handler._is_admitted_private_update(incoming.raw_payload):
                    raise CommandError("Contexto no autorizado; no se reproduce.")
                account = TechnicianChannel.objects.filter(
                    pk=incoming.channel_account_id, provider=incoming.provider, external_user_id=incoming.external_sender_id,
                    is_active=True, technician__is_active=True,
                ).first()
                if account is None:
                    raise CommandError("Reportante o canal inactivo; no se reproduce.")
                handler.incoming_service._validate_message_owner(incoming, account, incoming.external_chat_id)
                if not incoming.incident_id and ChannelMessage.objects.filter(
                    technician_id=incoming.technician_id, direction="inbound", pk__gt=incoming.pk,
                ).exists():
                    raise CommandError("La conversación avanzó: revisar antes de reenviar un resumen antiguo.")
                reply.status, reply.processing_attempts = "failed", 0
                reply.save(update_fields=["status", "processing_attempts"])
                _, sent = handler._deliver_reply(incoming, reply.content, reply.reply_purpose)
                self.stdout.write(f"Entrega confirmada: {sent}")
            return
        # Solo IDs y estados, sin textos de conversaciones ni credenciales.
        for row in ChannelMessage.objects.exclude(status__in=["sent", "processed"]).order_by("pk")[:100]:
            self.stdout.write(f"Mensaje {row.pk}: {row.direction}, {row.status}, intentos={row.processing_attempts}")
        for row in IncidentNotification.objects.exclude(status="sent").order_by("pk")[:100]:
            self.stdout.write(f"Notificación {row.pk}: {row.status}, intentos={row.attempts}")
