"""Admisión independiente por módulo, solo tras polling del bot verificado."""
import re

from django.core.exceptions import PermissionDenied

from incidents.models import TechnicianChannel
from content_coordination.models import Participant, TelegramConsent
from content_coordination.access import telegram_allowed, binding_allows_update


def command(text, bot_username):
    value = text.strip()
    if not value.startswith('/'):
        return None
    match = re.fullmatch(r'/([a-zA-Z_]+)(?:@([a-zA-Z0-9_]+))?', value)
    if not match or (match[2] and match[2].lower() != bot_username.lower()):
        return 'unknown'
    return match[1].lower()


def private_message(update):
    if (not isinstance(update, dict) or set(update) - {'update_id', 'message'}
            or type(update.get('update_id')) is not int or update['update_id'] < 0):
        raise PermissionDenied('Actualización no admitida; no se confirmó su recepción.')
    msg = update.get('message')
    if not isinstance(msg, dict):
        raise PermissionDenied('Se requiere un mensaje privado de texto.')
    sender, chat = msg.get('from'), msg.get('chat')
    if (not isinstance(sender, dict) or not isinstance(chat, dict)
            or type(sender.get('id')) is not int or sender['id'] <= 0
            or sender.get('is_bot') or chat.get('type') != 'private' or type(chat.get('id')) is not int
            or chat.get('id') != sender['id'] or type(msg.get('message_id')) is not int
            or msg['message_id'] <= 0 or not isinstance(msg.get('text'), str)
            or not 0 < len(msg['text'].strip()) <= 4000):
        raise PermissionDenied('Remitente, chat o texto no admitido.')
    return msg


def incident_account(identity):
    return TechnicianChannel.objects.select_related('technician').filter(
        provider='telegram', external_user_id=identity, is_active=True,
        technician__is_active=True).filter(external_chat_id__in=['', identity]).first()


def content_actor(update, *, inbound_start=False):
    identity = str(update['message']['from']['id'])
    actor = Participant.objects.select_related('user').filter(
        telegram_id=identity, telegram_authorized=True, active=True).first()
    if actor and telegram_allowed(actor, inbound_start=inbound_start) and binding_allows_update(actor, update):
        return actor
    return None


def content_binding(actor):
    consent = TelegramConsent.objects.filter(participant=actor).first()
    return {'participant_id': actor.pk, 'epoch': consent.bound_at.isoformat() if consent and consent.bound_at else None}


def capabilities(update, *, inbound_start=False):
    msg = private_message(update)
    account = incident_account(str(msg['from']['id']))
    actor = content_actor(update, inbound_start=inbound_start)
    result = {}
    if account:
        result['incident'] = {'channel_id': account.pk, 'technician_id': str(account.technician_id)}
    if actor:
        result['content'] = content_binding(actor)
    return result


def still_authorized(receipt, module):
    current = capabilities(receipt.payload)
    return bool(module in current and current[module] == receipt.authorization.get(module))
