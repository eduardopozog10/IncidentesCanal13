"""Cambio reversible de vínculo en demo. No mueve conversaciones ni roles."""
from django.core.management.base import CommandError
from django.db import transaction
from django.utils import timezone

from .models import Participant, TelegramConsent, TelegramLinkTransfer, ParticipantAccessEvent, Message, Outbox
from .access import telegram_allowed


def guard_demo():
    from .management.commands.run_secuoya_demo import ensure_demo
    ensure_demo()


def administrator(username):
    actor = Participant.objects.filter(user__username=username, active=True, role='admin', user__is_active=True).first()
    if actor is None:
        raise CommandError('El operador debe ser un administrador activo de la demo con usuario Django activo.')
    return actor


def participants(source, target, *, lock=False):
    qs = Participant.objects.select_related('user').filter(user__username__in=[source,target]).order_by('pk')
    if lock:qs=qs.select_for_update(of=('self',))
    people = {p.user.username:p for p in qs}
    if source == target or len(people) != 2:
        raise CommandError('Origen y destino deben ser dos participantes existentes y distintos.')
    a,b=people[source],people[target]
    if a.role != 'admin' or b.role != 'coordinator':
        raise CommandError('Este procedimiento solo transfiere temporalmente de administrador a coordinador; no cambia roles.')
    if not (a.active and a.user.is_active and b.active and b.user.is_active):
        raise CommandError('Ambos participantes y usuarios Django deben estar activos.')
    return a,b


def snapshot(actor):
    consent = TelegramConsent.objects.filter(participant=actor).first()
    return {'telegram_id':actor.telegram_id,'telegram_authorized':actor.telegram_authorized,
            'role':actor.role,'username':actor.user.username,
            'consent': {key:(value.isoformat() if hasattr(value,'isoformat') else value)
                        for key,value in vars(consent).items() if key in
                        {'telegram_id','consent_at','verified_at','revoked_at','bound_at','verified_update_id'}} if consent else None}


def assert_idle(people):
    ids=[p.pk for p in people]
    inbox=list(Message.objects.filter(participant_id__in=ids,status__in=['pending','retry','processing']).values_list('pk',flat=True))
    outbox=list(Outbox.objects.filter(recipient_id__in=ids,status__in=['pending','retry','sending','uncertain']).values_list('pk',flat=True))
    if inbox or outbox:
        raise CommandError(f'Hay mensajes pendientes o envíos inciertos. Revisa sin descartar: inbox={inbox[:20]}, outbox={outbox[:20]}.')


def new_binding(actor, identity, now):
    # La prueba de identidad NO se copia del participante anterior.
    TelegramConsent.objects.update_or_create(participant=actor, defaults={
        'telegram_id':identity,'consent_at':now,'verified_at':None,'revoked_at':None,
        'bound_at':now,'verified_update_id':None})
    actor.telegram_id=identity;actor.telegram_authorized=True
    actor.save(update_fields=['telegram_id','telegram_authorized'])


def audit(transfer, actor, operation, before, after, operator):
    ParticipantAccessEvent.objects.create(participant=actor,action=operation,details={
        'transfer_id':str(transfer.pk),'operator_id':operator.pk,'operator_username':operator.user.username,
        'before':before,'after':after})


def preview(source, target, identity, operator):
    guard_demo();administrator(operator)
    a,b=participants(source,target)
    return {'source':snapshot(a),'target':snapshot(b),'requested_id':identity,
            'source_delivery_eligible':telegram_allowed(a),
            'notice':'Solo lectura. Detén el receptor y revisa las colas antes de transferir. Cada cambio requiere un /start nuevo.'}


@transaction.atomic
def transfer_link(source, target, identity, operator, transfer_id, *, consent_confirmed=False, remote_queue_empty=False):
    guard_demo()
    op=administrator(operator)
    if not consent_confirmed or not remote_queue_empty:
        raise CommandError('Se requieren consentimiento explícito y confirmación de cola remota vacía revisada con el receptor detenido.')
    a,b=participants(source,target,lock=True)
    existing=TelegramLinkTransfer.objects.select_for_update().filter(pk=transfer_id).first()
    if existing:
        if (existing.source_id,existing.target_id,existing.telegram_id) != (a.pk,b.pk,identity):
            raise CommandError('Ese transfer-id pertenece a otra operación.')
        if existing.state == 'active' and (a.telegram_id is not None or b.telegram_id != identity):
            raise CommandError('El vínculo cambió fuera de la transferencia; requiere revisión manual.')
        return existing,False
    if a.telegram_id != identity or not telegram_allowed(a):
        raise CommandError('El origen no tiene ese vínculo autorizado y habilitado. No se modificó nada.')
    if b.telegram_id or b.telegram_authorized:
        raise CommandError('El destino ya tiene un vínculo o autorización; no se sobrescribe.')
    if TelegramConsent.objects.filter(participant=b,revoked_at__isnull=True).exists():
        raise CommandError('El destino tiene un consentimiento activo previo; revísalo antes de transferir.')
    if TelegramLinkTransfer.objects.filter(state='active',telegram_id=identity).exists():
        raise CommandError('Ya hay una transferencia activa para esa cuenta; reutiliza su ID.')
    assert_idle([a,b])
    sa,sb=snapshot(a),snapshot(b);now=timezone.now()
    row=TelegramLinkTransfer.objects.create(id=transfer_id,source=a,target=b,telegram_id=identity,operator=op,
                                          source_snapshot=sa,target_snapshot=sb)
    TelegramConsent.objects.filter(participant=a,revoked_at__isnull=True).update(revoked_at=now)
    a.telegram_id=None;a.telegram_authorized=False
    a.save(update_fields=['telegram_id','telegram_authorized'])
    new_binding(b,identity,now)
    audit(row,a,'transfer_out',sa,snapshot(a),op)
    audit(row,b,'transfer_in',sb,snapshot(b),op)
    return row,True


@transaction.atomic
def restore_link(transfer_id, operator, *, consent_confirmed=False, remote_queue_empty=False):
    guard_demo();op=administrator(operator)
    if not consent_confirmed or not remote_queue_empty:
        raise CommandError('La restauración requiere consentimiento y cola remota vacía revisada con el receptor detenido.')
    record=TelegramLinkTransfer.objects.filter(pk=transfer_id).select_related('source__user','target__user').first()
    if record is None:raise CommandError('No existe esa transferencia.')
    # Mismo orden de bloqueo que en la transferencia y registro/revocación.
    a,b=participants(record.source.user.username,record.target.user.username,lock=True)
    record=TelegramLinkTransfer.objects.select_for_update().get(pk=transfer_id)
    if record.state == 'restored':return record,False
    if a.telegram_id is not None or a.telegram_authorized or b.telegram_id != record.telegram_id:
        raise CommandError('Los vínculos cambiaron fuera del procedimiento. No se sobrescribieron; revisa la auditoría.')
    assert_idle([a,b])
    sa,sb=snapshot(a),snapshot(b);now=timezone.now()
    TelegramConsent.objects.filter(participant=b,revoked_at__isnull=True).update(revoked_at=now)
    b.telegram_id=record.target_snapshot['telegram_id']
    b.telegram_authorized=record.target_snapshot['telegram_authorized']
    b.save(update_fields=['telegram_id','telegram_authorized'])
    new_binding(a,record.telegram_id,now)
    record.state='restored';record.restored_at=now;record.restore_operator=op
    record.save(update_fields=['state','restored_at','restore_operator'])
    audit(record,b,'transfer_return',sb,snapshot(b),op)
    audit(record,a,'transfer_restore',sa,snapshot(a),op)
    return record,True
