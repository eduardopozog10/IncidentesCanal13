"""Un solo receptor para ambos módulos, exclusivamente en la base demo."""
import os
import subprocess
import time

import httpx
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from incidents.services.notification_worker import drain_notifications
from content_coordination.delivery import deliver_one, recover_delivery_interruptions
from content_coordination.models import Message
from content_coordination.services import schedule_reminders
from content_coordination.telegram_polling import TelegramPollingClient, inspect_pending
from telegram_routing.authorization import capabilities, command
from telegram_routing.models import TelegramReceipt
from telegram_routing.router import TelegramRouter, RoutingBlocked, check_legacy_pending
from telegram_routing.receiver_lock import receiver_lock


def ensure_demo():
    if (settings.DATABASES['default']['NAME'] != 'demo_secuoya_contenidos'
            or settings.EMAIL_BACKEND != 'django.core.mail.backends.locmem.EmailBackend'
            or not settings.CONTENT_COORDINATION_ENABLED):
        raise CommandError('Se requiere config.content_demo, base demo y correo en memoria.')


def check_other_receivers():
    if os.name != 'nt':
        raise CommandError('Este arranque seguro está preparado para Windows; comprueba receptores antes de portarlo.')
    script = ("$ErrorActionPreference='Stop'; Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^(python|pythonw|uv)([.]exe)?$' "
              "-and $_.CommandLine -match 'run_telegram_bot|qa_telegram_receiver' } | Select-Object -ExpandProperty ProcessId")
    check = subprocess.run(['powershell', '-NoProfile', '-Command', script], capture_output=True, text=True, timeout=15)
    if check.returncode != 0:
        raise CommandError('No se pudo verificar la ausencia de otro receptor.')
    if check.stdout.strip():
        raise CommandError('Hay receptores existentes; identifica su función y detenlos antes de continuar. PID: ' + check.stdout.strip())


class Command(BaseCommand):
    help = 'Receptor único de contenidos/incidencias en PostgreSQL demo, sin SMTP.'

    def add_arguments(self, parser):
        modes = parser.add_mutually_exclusive_group()
        modes.add_argument('--live', action='store_true', help='Habilita Telegram real para destinatarios autorizados.')
        modes.add_argument('--preflight', action='store_true', help='Consulta bot y cola remota sin confirmar ni procesar mensajes.')
        modes.add_argument('--inspect-pending-id', type=int, help='Inspecciona un ID pendiente sin autorizarlo, procesarlo ni confirmarlo.')
        parser.add_argument('--accept-pending-ids', default='', help='IDs revisados explícitamente; nunca descarta mensajes.')
        parser.add_argument('--commands-only', action='store_true', help='Solo controles del router: sin agentes, recuperación de negocio, recordatorios ni avisos históricos.')

    def handle(self, *args, **options):
        ensure_demo()
        required = {TelegramReceipt._meta.db_table, 'telegram_routing_routingsession'}
        if not required.issubset(connection.introspection.table_names()):
            raise CommandError('Falta la migración del router. Respalda y revisa el plan antes de aplicarla en demo. No se consultó Telegram.')
        check_other_receivers()
        if not settings.TELEGRAM_BOT_TOKEN:
            raise CommandError('Falta la configuración de Telegram.')
        if not options['live'] and not options['preflight'] and options['inspect_pending_id'] is None:
            raise CommandError('Usa --preflight, --inspect-pending-id o --live.')
        try:
            accepted = {int(v) for v in options['accept_pending_ids'].split(',') if v.strip()}
            if any(value < 0 for value in accepted):
                raise ValueError()
        except ValueError:
            raise CommandError('Los IDs autorizados deben ser enteros separados por comas.') from None
        api = f'https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}'
        with receiver_lock(), httpx.Client(timeout=httpx.Timeout(35, connect=10)) as client:
            call = TelegramPollingClient(client, api, self.stdout.write).call

            bot = call('getMe')
            if bot.get('username', '').lower() != settings.CONTENT_BOT_USERNAME.lower():
                raise CommandError('El bot no coincide con Secuoya Chile.')
            if call('getWebhookInfo').get('url'):
                raise CommandError('El bot tiene webhook; no se modificó su configuración.')
            bot_id = bot.get('id')
            if type(bot_id) is not int or bot_id <= 0:
                raise CommandError('El bot no devolvió un ID válido; no se confirmó ninguna actualización.')
            # Sin offset: leer no confirma ni descarta las actualizaciones.
            pending = call('getUpdates', {'timeout': 0, 'limit': 100})
            if options['inspect_pending_id'] is not None:
                self.stdout.write(inspect_pending(pending, options['inspect_pending_id'], bot_id=str(bot_id)))
                return
            unknown = []
            for update in pending:
                try:
                    auth = capabilities(update, inbound_start=command(update.get('message', {}).get('text', ''), settings.CONTENT_BOT_USERNAME) == 'start')
                    if not auth:
                        raise PermissionDenied()
                except PermissionDenied:
                    raise CommandError(f"Actualización {update.get('update_id')} no admitida. Detenido sin confirmarla.") from None
                if not TelegramReceipt.objects.filter(bot_id=str(bot_id), update_id=update['update_id']).exists() and update['update_id'] not in accepted:
                    unknown.append((update['update_id'], update.get('message', {}).get('date')))
            self.stdout.write(f"DEMO | PostgreSQL={settings.DATABASES['default']['NAME']} | IA={settings.GEMINI_MODEL} | correo=memoria | bot=@{bot['username']}")
            self.stdout.write(f'Actualizaciones remotas visibles: {len(pending)}. Sin confirmar: {unknown}')
            if options['preflight']:
                return
            if unknown or len(pending) == 100:
                raise CommandError('Revisa el contexto de la cola remota antes de iniciar. No se descartó ni confirmó ningún mensaje.')
            try:
                check_legacy_pending()
            except RoutingBlocked as exc:
                raise CommandError(str(exc)) from None
            settings.CONTENT_LIVE_TELEGRAM = True
            commands_only = options['commands_only']
            if not commands_only:
                recover_delivery_interruptions()
            router = TelegramRouter(bot_id, commands_only=commands_only, threaded=True)
            offset = None
            last_tick = 0
            self.stdout.write('RECEPTOR DEMO ACTIVO | consola compacta | Ctrl+C para detener')
            self.stdout.write('ROUTER | comandos solamente, sin IA ni avisos históricos' if commands_only else 'ROUTER | Incidencias + Contenidos, selección explícita')
            try:
                while True:
                    try:
                        router.tick()  # Recuperación durable antes de confirmar nuevas actualizaciones.
                    except RoutingBlocked as exc:
                        raise CommandError(str(exc)) from None
                    waiting = TelegramReceipt.objects.filter(bot_id=str(bot_id), status='queued').exists()
                    updates = call('getUpdates', {'timeout': 1 if waiting else 25, 'limit': 20, **({'offset': offset} if offset else {})})
                    for update in sorted(updates, key=lambda item: item.get('update_id', 0)):
                        # Persistencia confirmada antes de adelantar el offset remoto.
                        try:
                            if Message.objects.filter(external_id=f"telegram:{update['update_id']}").exists() and not TelegramReceipt.objects.filter(bot_id=str(bot_id), update_id=update['update_id']).exists():
                                raise RoutingBlocked('Actualización histórica en el inbox anterior: revisar su contexto sin reprocesarla.')
                            receipt = router.receive(update)
                            if receipt.status == 'blocked':
                                raise RoutingBlocked(f'Actualización {receipt.update_id}: {receipt.reason}. Recepción guardada; despacho detenido.')
                        except PermissionDenied:
                            raise CommandError('Actualización no autorizada; receptor detenido sin descartarla.') from None
                        except RoutingBlocked as exc:
                            raise CommandError(str(exc)) from None
                        offset = update['update_id'] + 1
                        self.stdout.write(f'actualización {receipt.update_id} | destino={receipt.destination} | {receipt.status}')
                    try:
                        router.tick()
                    except RoutingBlocked as exc:
                        raise CommandError(str(exc)) from None
                    if not commands_only and time.monotonic() - last_tick >= settings.CONTENT_TICK_SECONDS:
                        schedule_reminders(settings.CONTENT_REMINDER_MINUTES)
                        drain_notifications()
                        last_tick = time.monotonic()
                    if not commands_only:
                        for _ in range(20):
                            out = deliver_one()
                            if out is None:
                                break
                            self.stdout.write(f'aviso {out.pk} | {out.status}')
            except KeyboardInterrupt:
                self.stdout.write('RECEPTOR DEMO DETENIDO. Los mensajes y avisos permanecen guardados.')
            finally:
                router.close()
