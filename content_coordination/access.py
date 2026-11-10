"""Consentimiento y prueba de identidad separados de permisos de negocio."""
from django.conf import settings
from .models import TelegramConsent, TelegramLinkTransfer
import math


def telegram_allowed(actor, *, inbound_start=False):
    if not (actor.active and actor.telegram_authorized and actor.telegram_id):
        return False
    if actor.user_id and not actor.user.is_active:
        return False
    consent = TelegramConsent.objects.filter(participant=actor).first()
    if consent:
        return bool(consent.telegram_id == actor.telegram_id and not consent.revoked_at
                    and (consent.verified_at or inbound_start))
    # Conserva exclusivamente el consentimiento histórico autorizado por el usuario.
    return actor.telegram_id in {str(v) for v in settings.CONTENT_ALLOWED_TELEGRAM_IDS}


def binding_allows_update(actor, update):
    consent = TelegramConsent.objects.filter(participant=actor).first()
    if not consent or consent.bound_at is None:return True
    date=update.get('message',{}).get('date')
    identity=update.get('message',{}).get('from',{}).get('id')
    if str(identity) != actor.telegram_id or type(date) is not int or date < math.ceil(consent.bound_at.timestamp()):
        return False
    update_id=update.get('update_id')
    if consent.verified_update_id is not None and (type(update_id) is not int or update_id < consent.verified_update_id):
        return False
    return True


def message_binding_valid(actor, message):
    consent=TelegramConsent.objects.filter(participant=actor).first()
    if not consent or consent.bound_at is None:return True
    return (message.payload.get('binding_epoch') == consent.bound_at.isoformat()
            and binding_allows_update(actor,message.payload.get('update',{})))


def content_only(actor):
    return bool(actor.telegram_id and TelegramLinkTransfer.objects.filter(target=actor,telegram_id=actor.telegram_id,state='active').exists())
