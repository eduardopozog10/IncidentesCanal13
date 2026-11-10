"""Liberación explícita de un aviso de asignación nunca enviado, solo en demo.

No crea copias, no altera tareas/versiones y no llama a Telegram. No es un
reintento general para mensajes fallidos, parciales ni de entrega incierta.
"""
from django.core.management.base import CommandError
from django.db import transaction
from django.utils import timezone
from .models import Participant, ContentRequest, Outbox, ParticipantAccessEvent
from .access import telegram_allowed
from .delivery import obsolete

BLOCK_REASONS={'RecipientNotAuthorized','NewConsentReviewRequired','AuthorizationRevoked'}


def refusal(actor,row):
    if actor.role!='reviewer' or not telegram_allowed(actor):
        return 'Se requiere un revisor activo, autorizado y verificado mediante /start.'
    if row.recipient_id!=actor.pk or not row.request_id or not row.key.startswith(f'assign_review:{row.request_id}:'):
        return 'Solo se permite el aviso de asignación de revisión de este participante.'
    if row.status!='blocked' or row.error_type not in BLOCK_REASONS:
        return 'Solo se libera un bloqueo de autorización identificado; no se reenvían avisos enviados o inciertos.'
    if row.attempts or row.sent_at or row.telegram_message_id or row.delivery_parts:
        return 'Ese aviso tiene intentos o acuses previos. Requiere revisión manual; no se reenvía.'
    r=row.request
    task=r.tasks.filter(kind='review',assignee=actor,state='pending').first()
    versions=list(r.versions.filter(state='review',approved_at__isnull=True))
    if r.state!='review' or task is None or len(versions)!=1 or obsolete(row):
        return 'La revisión ya no está pendiente y asignada a esta persona.'
    if row.created_at<versions[0].created_at:
        return 'El aviso corresponde a una versión anterior; no se reutiliza para otra revisión.'
    latest=Outbox.objects.filter(recipient=actor,request=r,key__startswith=f'assign_review:{r.pk}:').order_by('-pk').first()
    if latest.pk!=row.pk:
        return 'Existe una asignación posterior. Revisa el aviso más reciente.'
    return None


def inspect_review_notice(actor,row):
    reason=refusal(actor,row)
    return {'notice_id':row.pk,'key':row.key,'request':row.request.code if row.request_id else None,
            'status':row.status,'attempts':row.attempts,'error_type':row.error_type,
            'sent_at':row.sent_at,'telegram_message_id':row.telegram_message_id,
            'delivery_parts':row.delivery_parts,
            'created_at':row.created_at,'text':row.text,'release_eligible':reason is None,'reason':reason}


@transaction.atomic
def release_review_notice(actor,notice_id,operator):
    from .management.commands.run_secuoya_demo import ensure_demo
    ensure_demo()
    op=Participant.objects.filter(user__username=operator,active=True,role='admin',user__is_active=True).first()
    if op is None:raise CommandError('La liberación requiere un administrador local activo, declarado en auditoría.')
    actor=Participant.objects.select_for_update().get(pk=actor.pk)
    row=Outbox.objects.select_for_update().filter(pk=notice_id,recipient=actor).first()
    if row is None:raise CommandError('No existe ese aviso para el participante indicado.')
    # Repetir la operación no reinicia entrega, contadores ni auditoría.
    recorded=ParticipantAccessEvent.objects.filter(participant=actor,action='review_notice_release',details__notice_id=row.pk).exists()
    if recorded and row.status in {'pending','retry','sending','sent','uncertain','failed','superseded'}:
        return row,False
    if row.request_id:
        ContentRequest.objects.select_for_update().get(pk=row.request_id)
    reason=refusal(actor,row)
    if reason:raise CommandError(reason)
    previous={'status':row.status,'error_type':row.error_type}
    row.status='pending';row.error_type='';row.retry_at=timezone.now()
    row.save(update_fields=['status','error_type','retry_at'])
    ParticipantAccessEvent.objects.create(participant=actor,action='review_notice_release',details={
        'notice_id':row.pk,'key':row.key,'request_id':row.request_id,'operator_id':op.pk,
        'operator_username':op.user.username,'before':previous,'after':{'status':'pending'}})
    return row,True
