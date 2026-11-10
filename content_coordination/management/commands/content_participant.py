"""Administración local explícita de consentimiento, sin llamadas al bot."""
import json
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction, IntegrityError
from django.utils import timezone
from content_coordination.models import Participant, TelegramConsent, ParticipantAccessEvent, Outbox, Conversation, TelegramLinkTransfer
from django.db.models import Q
from content_coordination.access import telegram_allowed
from .run_secuoya_demo import ensure_demo
from content_coordination.review_notices import inspect_review_notice, release_review_notice

REGISTRATION_ROLES = ('coordinator','designer','reviewer')


class Command(BaseCommand):
    help = 'Registrar, consultar o revocar cuentas Telegram consentidas, solo en demo.'

    def add_arguments(self, parser):
        parser.add_argument('action', choices=['register','show','revoke','review-notices','release-review'])
        parser.add_argument('--username', required=True)
        parser.add_argument('--name')
        parser.add_argument('--telegram-id')
        parser.add_argument('--role', choices=REGISTRATION_ROLES)
        parser.add_argument('--consent-confirmed', action='store_true')
        parser.add_argument('--notice-id',type=int)
        parser.add_argument('--operator',help='Administrador local declarado en auditoría; no es autenticación por contraseña.')
        parser.add_argument('--release-confirmed',action='store_true')

    @transaction.atomic
    def register(self, username, options):
        identity = options['telegram_id'] or ''
        if (not options['consent_confirmed'] or not identity.isascii() or not identity.isdecimal()
                or int(identity) <= 0 or len(identity)>30 or identity!=str(int(identity))
                or options['role'] not in REGISTRATION_ROLES):
            raise CommandError('Se requiere ID personal positivo, un rol habilitado y --consent-confirmed tras obtener consentimiento.')
        existing = Participant.objects.select_for_update().filter(user__username=username).first()
        if existing and TelegramLinkTransfer.objects.filter(Q(source=existing)|Q(target=existing),state='active').exists():
            raise CommandError('Hay una transferencia temporal activa. No registres otro vínculo; usa su restauración auditada.')
        if existing and (existing.role != options['role'] or existing.telegram_id not in {None, '', identity}):
            raise CommandError('Ese participante tiene otro rol o vínculo. No se sobrescribió; utiliza un participante nuevo para otra identidad.')
        if existing and (not existing.active or not existing.user.is_active):
            raise CommandError('El participante y su usuario Django deben estar activos. El registro no reactiva cuentas.')
        if (Participant.objects.filter(telegram_id=identity).exclude(pk=existing.pk if existing else None).exists()
                or TelegramConsent.objects.filter(telegram_id=identity,revoked_at__isnull=True)
                    .exclude(participant=existing).exists()):
            raise CommandError('El ID ya pertenece a otro participante o consentimiento activo.')
        if existing is None:
            if not options['name'] or get_user_model().objects.filter(username=username).exists():
                raise CommandError('Para un usuario nuevo proporciona --name y un username disponible.')
            user = get_user_model().objects.create_user(username=username)
            existing = Participant.objects.create(user=user, name=options['name'], role=options['role'])
        consent = TelegramConsent.objects.filter(participant=existing).first()
        if consent and consent.revoked_at is None and consent.telegram_id == identity:
            if existing.telegram_id != identity or not existing.telegram_authorized:
                raise CommandError('Vínculo y consentimiento no coinciden. Revisa su contexto; no se reactivó la cuenta.')
            self.stdout.write('El consentimiento ya está registrado; no se modificó ni se reactivaron avisos.')
            return
        # El alta no activa envíos antiguos ni hereda una prueba de identidad.
        Outbox.objects.filter(recipient=existing, status__in=['pending','retry']).update(status='blocked', error_type='NewConsentReviewRequired')
        existing.telegram_id = identity
        existing.telegram_authorized = True
        existing.save(update_fields=['telegram_id','telegram_authorized'])
        Conversation.objects.filter(participant=existing).update(pending={}, context={})
        now=timezone.now()
        TelegramConsent.objects.update_or_create(participant=existing, defaults={
            'telegram_id':identity,'consent_at':now,'verified_at':None,'revoked_at':None,
            'bound_at':now,'verified_update_id':None})
        ParticipantAccessEvent.objects.create(participant=existing, action='consent', details={
            'telegram_id':identity,'role':existing.role,'operator':'local-command','bound_at':now.isoformat()})
        self.stdout.write('Consentimiento registrado. Falta un /start privado NUEVO de esa cuenta. No se envió ni reactivó ningún aviso.')

    def handle(self, *args, **options):
        ensure_demo()
        username = options['username']
        if options['action'] == 'register':
            try:self.register(username,options)
            except IntegrityError:
                raise CommandError('Conflicto concurrente de identidad o usuario. No se modificó el registro; consulta el estado antes de repetir.') from None
        elif options['action'] in {'review-notices','release-review'}:
            actor=Participant.objects.filter(user__username=username).first()
            if actor is None:raise CommandError('No existe ese participante.')
            if options['action']=='review-notices':
                rows=Outbox.objects.filter(recipient=actor,key__startswith='assign_review:').order_by('-pk')[:30]
                self.stdout.write(json.dumps([inspect_review_notice(actor,row) for row in rows],default=str,ensure_ascii=True))
            else:
                if not options['notice_id'] or not options['operator'] or not options['release_confirmed']:
                    raise CommandError('Indica --notice-id, --operator y --release-confirmed después de revisar ese aviso.')
                row,changed=release_review_notice(actor,options['notice_id'],options['operator'])
                self.stdout.write(json.dumps({'notice_id':row.pk,'status':row.status,'changed':changed,
                    'notice':'Aviso encolado; no es una entrega confirmada. Solo el receptor puede registrar su acuse.' if changed
                        else 'Operación ya registrada. No se reencoló ni se alteró la entrega.'},ensure_ascii=True))
        elif options['action'] == 'revoke':
            with transaction.atomic():
                actor = Participant.objects.select_for_update().filter(user__username=username).first()
                if actor is None:raise CommandError('No existe ese participante.')
                actor.telegram_authorized = False
                actor.save(update_fields=['telegram_authorized'])
                TelegramConsent.objects.filter(participant=actor).update(revoked_at=timezone.now())
                Conversation.objects.filter(participant=actor).update(pending={}, context={})
                Outbox.objects.filter(recipient=actor, status__in=['pending','retry']).update(status='blocked', error_type='AuthorizationRevoked')
                ParticipantAccessEvent.objects.create(participant=actor, action='revoke', details={'operator':'local-command'})
            self.stdout.write('Acceso Telegram revocado. Historial, mensajes y asignaciones conservados; no se modificó el acceso web.')
        else:
            actor = Participant.objects.filter(user__username=username).first()
            if actor is None:raise CommandError('No existe ese participante.')
            consent = TelegramConsent.objects.filter(participant=actor).first()
            self.stdout.write(json.dumps({'username':username,'name':actor.name,'role':actor.role,'telegram_id':actor.telegram_id,
                'authorized':actor.telegram_authorized,'verified':bool(consent and consent.verified_at),
                'consent_at':consent.consent_at if consent else None,'revoked':bool(consent and consent.revoked_at),
                'bound_at':consent.bound_at if consent else None,'active':actor.active,
                'authorization_source':'recorded-consent' if consent else 'historical-allowlist',
                'delivery_eligible':telegram_allowed(actor),'last_delivery':Outbox.objects.filter(recipient=actor,status='sent').order_by('-sent_at').values('sent_at','telegram_message_id').first()},default=str,ensure_ascii=True))
