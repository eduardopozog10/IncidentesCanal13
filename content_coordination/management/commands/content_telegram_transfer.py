"""Operaciones locales; nunca llama a Telegram ni descarta mensajes."""
import json
from uuid import UUID
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from content_coordination.models import TelegramLinkTransfer
from content_coordination.transfers import guard_demo, administrator, preview, transfer_link, restore_link
from .run_secuoya_demo import receiver_lock, check_other_receivers


class Command(BaseCommand):
    help='Planificar, transferir o restaurar un vínculo Telegram en demo con auditoría e idempotencia.'

    def add_arguments(self, parser):
        parser.add_argument('action',choices=['plan','transfer','restore','show'])
        parser.add_argument('--source')
        parser.add_argument('--target')
        parser.add_argument('--telegram-id')
        parser.add_argument('--operator',required=True,help='Administrador local que declara y audita la operación.')
        parser.add_argument('--transfer-id',type=UUID)
        parser.add_argument('--consent-confirmed',action='store_true')
        parser.add_argument('--remote-queue-empty-confirmed',action='store_true')

    def handle(self,*args,**options):
        guard_demo();administrator(options['operator'])
        action=options['action']
        if action in {'plan','transfer'}:
            if not options['source'] or not options['target'] or not (options['telegram_id'] or '').isascii() or not (options['telegram_id'] or '').isdecimal() or int(options['telegram_id'])<=0:
                raise CommandError('Indica --source, --target y el ID Telegram personal real que esperas transferir.')
        if action=='plan':
            self.stdout.write(json.dumps(preview(options['source'],options['target'],options['telegram_id'],options['operator']),ensure_ascii=True))
            return
        if action=='show' and options['transfer_id'] is None:
            rows=TelegramLinkTransfer.objects.order_by('-created_at')
            if options['source']:rows=rows.filter(source__user__username=options['source'])
            if options['target']:rows=rows.filter(target__user__username=options['target'])
            self.stdout.write(json.dumps(list(rows.values('id','state','telegram_id','source__user__username','target__user__username','created_at','restored_at')[:20]),default=str,ensure_ascii=True))
            return
        if options['transfer_id'] is None:raise CommandError('Indica un --transfer-id UUID estable para evitar operaciones duplicadas.')
        if action=='show':
            row=TelegramLinkTransfer.objects.filter(pk=options['transfer_id']).select_related('source__user','target__user').first()
            if row is None:raise CommandError('No existe esa transferencia.')
            self.stdout.write(json.dumps({'transfer_id':str(row.pk),'state':row.state,'telegram_id':row.telegram_id,
                'source':row.source.user.username,'target':row.target.user.username,
                'source_link':row.source.telegram_id,'target_link':row.target.telegram_id},ensure_ascii=True))
            return
        if not options['consent_confirmed'] or not options['remote_queue_empty_confirmed']:
            raise CommandError('Revisa consentimiento y cola remota vacía antes de usar ambas confirmaciones explícitas.')
        if not settings.TELEGRAM_BOT_TOKEN:raise CommandError('Se requiere token configurado para compartir el bloqueo del receptor, sin realizar llamadas externas.')
        check_other_receivers()
        with receiver_lock():
            kwargs={'consent_confirmed':True,'remote_queue_empty':True}
            if action=='transfer':
                row,changed=transfer_link(options['source'],options['target'],options['telegram_id'],options['operator'],options['transfer_id'],**kwargs)
            else:row,changed=restore_link(options['transfer_id'],options['operator'],**kwargs)
        self.stdout.write(json.dumps({'transfer_id':str(row.pk),'state':row.state,'changed':changed,
            'notice':'No se envió ningún mensaje. El vínculo activado necesita un /start privado nuevo para verificarse.' if changed else 'Operación ya registrada; no se repitió ni reinició la verificación.'},ensure_ascii=True))
