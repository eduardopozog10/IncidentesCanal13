"""Admisión estricta de Telegram. La identidad nunca procede del texto/IA."""
from django.conf import settings
from django.core.exceptions import PermissionDenied

from .models import Participant, Conversation
from .processor import enqueue
from .access import telegram_allowed, binding_allows_update
from .models import TelegramConsent, ParticipantAccessEvent
from django.db import transaction
from django.utils import timezone


def admit(update):
    if set(update) - {'update_id', 'message'} or type(update.get('update_id')) is not int:
        raise PermissionDenied('Actualización no admitida; no se confirmó su recepción.')
    msg = update.get('message', {})
    sender = msg.get('from', {})
    chat = msg.get('chat', {})
    identity = str(sender.get('id', ''))
    if (chat.get('type') != 'private' or str(chat.get('id')) != identity or sender.get('is_bot')):
        raise PermissionDenied('Conversación no autorizada para la demo.')
    actor = Participant.objects.filter(telegram_id=identity, telegram_authorized=True, active=True).first()
    if not actor or not isinstance(msg.get('text'), str) or not telegram_allowed(actor, inbound_start=msg.get('text') == '/start'):
        raise PermissionDenied('Participante o contenido no admitido.')
    if not binding_allows_update(actor,update):
        raise PermissionDenied('Actualización anterior al vínculo actual o sin fecha confiable. Requiere revisión; no se confirma ni reprocesa.')
    return actor, msg['text']


@transaction.atomic
def queue_update(update):
    actor, text = admit(update)
    Participant.objects.select_for_update().get(pk=actor.pk)
    actor, text = admit(update)  # Revalidar bajo el mismo bloqueo que usa la revocación.
    conv, _ = Conversation.objects.get_or_create(participant=actor)
    consent = TelegramConsent.objects.select_for_update().filter(participant=actor, revoked_at__isnull=True).first()
    message = enqueue(actor, f"telegram:{update['update_id']}", text,
                      payload={'update': update, 'module_at_receipt': conv.module,
                               'binding_epoch':consent.bound_at.isoformat() if consent and consent.bound_at else None})
    if consent and consent.verified_at is None and text == '/start':
        consent.verified_at = timezone.now()
        consent.verified_update_id=update['update_id']
        consent.save(update_fields=['verified_at','verified_update_id'])
        ParticipantAccessEvent.objects.create(participant=actor, action='verified', details={'update_id':update['update_id']})
    return message
