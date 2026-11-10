"""Entrega durable. Un resultado incierto nunca se reenvía automáticamente."""
from datetime import timedelta
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from incidents.messaging.telegram_provider import TelegramProvider, TelegramRejectedError
from .models import Outbox, Participant, ContentVersion, Task, ProgressQuery
from .access import telegram_allowed
from incidents.messaging.presentation import split_text


def obsolete(out):
    """No enviar una convocatoria a una etapa que ya terminó."""
    parts = out.key.split(':')
    if parts[0] == 'query':
        query = ProgressQuery.objects.filter(notification=out).first()
        if query:
            tasks = list(query.tasks.select_related('request'))
            return bool(query.answered_at or not tasks or any(t.assignee_id != out.recipient_id
                        or t.state not in {'pending','working','review'} or t.request.state in {'cancelled','emitted'} for t in tasks))
    if parts[0] == 'query-answer' and out.request_id:
        from .services import visible
        return not visible(out.recipient).filter(pk=out.request_id).exists()
    if out.request_id and parts[0] in {'assign', 'assign_review', 'assign_edit'}:
        return not Task.objects.filter(request_id=out.request_id, kind={'assign_review':'review','assign_edit':'editing'}.get(parts[0], 'design'),
                                       assignee_id=out.recipient_id, state='pending').exists()
    if out.request_id and parts[0] == 'new':
        return Task.objects.filter(request_id=out.request_id, kind='design', assignee__isnull=False).exists()
    if len(parts) > 1 and parts[0] in {'review', 'approved', 'edited'} and parts[1].isdigit():
        version = ContentVersion.objects.filter(pk=int(parts[1])).first()
        if version is None:
            return True
        return {'review': version.state != 'review', 'approved': bool(version.edited_at),
                'edited': bool(version.received_at)}[parts[0]]
    if parts[0] == 'deadline' and len(parts) > 1 and parts[1].isdigit():
        return not Task.objects.filter(pk=int(parts[1]), state__in=['pending', 'working'],
                                       request__state__in=['pending', 'assigned', 'production']).exists()
    return False


def deliver_one(provider=None):
    if not settings.CONTENT_LIVE_TELEGRAM:
        return None
    with transaction.atomic():
        out = (Outbox.objects.select_for_update(skip_locked=True)
               .filter(status__in=['pending', 'retry'], retry_at__lte=timezone.now())
               .order_by('pk').first())
        if out is None:
            return None
        if obsolete(out):
            out.status = 'superseded'
            out.save(update_fields=['status'])
            return out
        actor = Participant.objects.get(pk=out.recipient_id)
        if not telegram_allowed(actor):
            out.status = 'blocked'
            out.error_type = 'RecipientNotAuthorized'
            out.save(update_fields=['status', 'error_type'])
            return out
        out.status = 'sending'
        out.attempts += 1
        out.save(update_fields=['status', 'attempts'])
    try:
        sender = provider or TelegramProvider()
        parts = split_text(out.text)
        for part in parts[len(out.delivery_parts):]:
            # Revalidar la autorización antes de cada parte, también tras un reintento.
            actor.refresh_from_db()
            if not telegram_allowed(actor):
                raise TelegramRejectedError(retryable=False)
            result = sender.send_text(actor.telegram_id, part)
            message_id = result.get('result', {}).get('message_id')
            if not message_id:raise ValueError('MissingReceipt')
            out.delivery_parts.append({'message_id':str(message_id), 'at':timezone.now().isoformat()})
            out.save(update_fields=['delivery_parts'])
    except TelegramRejectedError as exc:
        out.status = 'retry' if exc.retryable and out.attempts < settings.CONTENT_MAX_ATTEMPTS else 'failed'
        out.retry_at = timezone.now() + timedelta(seconds=settings.CONTENT_RETRY_SECONDS)
        out.error_type = type(exc).__name__
    except Exception as exc:
        out.status = 'uncertain'
        out.error_type = type(exc).__name__
    else:
        out.status = 'sent'
        out.sent_at = timezone.now()
        out.telegram_message_id = out.delivery_parts[-1]['message_id']
    out.save(update_fields=['status', 'retry_at', 'error_type', 'sent_at', 'telegram_message_id'])
    return out


def recover_delivery_interruptions():
    # Solo al arrancar bajo el bloqueo exclusivo del receptor.
    return Outbox.objects.filter(status='sending').update(status='uncertain', error_type='InterruptedDelivery')
