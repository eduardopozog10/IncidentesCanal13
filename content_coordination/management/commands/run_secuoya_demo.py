"""Un solo receptor para ambos módulos, exclusivamente en la base demo."""
import hashlib
import os
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from incidents.messaging.telegram_update_handler import TelegramUpdateHandler
from incidents.services.notification_worker import drain_notifications
from content_coordination.delivery import deliver_one, recover_delivery_interruptions
from content_coordination.models import Conversation, Message
from content_coordination.processor import Processor
from content_coordination.routing import admit, queue_update
from content_coordination.services import schedule_reminders
from content_coordination.telegram_polling import TelegramPollingClient, inspect_pending
from content_coordination.access import content_only


def ensure_demo():
    if (settings.DATABASES['default']['NAME'] != 'demo_secuoya_contenidos'
            or settings.EMAIL_BACKEND != 'django.core.mail.backends.locmem.EmailBackend'
            or not settings.CONTENT_COORDINATION_ENABLED):
        raise CommandError('Se requiere config.content_demo, base demo y correo en memoria.')


@contextmanager
def receiver_lock():
    token_hash = hashlib.sha256(settings.TELEGRAM_BOT_TOKEN.encode()).hexdigest()[:20]
    lock_path = Path(tempfile.gettempdir()) / f'secuoya-receiver-{token_hash}.lock'
    with lock_path.open('a+b') as file:
        file.seek(0)
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise CommandError('Ya existe un receptor de la demo.') from None
        else:
            import fcntl
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise CommandError('Ya existe un receptor de la demo.') from None
        try:
            yield
        finally:
            if os.name == 'nt':
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)


def check_other_receivers():
    if os.name != 'nt':
        raise CommandError('Este arranque seguro está preparado para Windows; comprueba receptores antes de portarlo.')
    script = ("$ErrorActionPreference='Stop'; Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^(python|pythonw|uv)(\\.exe)?$' "
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

    def handle(self, *args, **options):
        ensure_demo()
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
            # Sin offset: leer no confirma ni descarta las actualizaciones.
            pending = call('getUpdates', {'timeout': 0, 'limit': 100})
            if options['inspect_pending_id'] is not None:
                self.stdout.write(inspect_pending(pending, options['inspect_pending_id']))
                return
            unknown = []
            for update in pending:
                try:
                    admit(update)
                except PermissionDenied:
                    raise CommandError(f"Actualización {update.get('update_id')} no admitida. Detenido sin confirmarla.") from None
                key = f"telegram:{update['update_id']}"
                if not Message.objects.filter(external_id=key).exists() and update['update_id'] not in accepted:
                    unknown.append((update['update_id'], update.get('message', {}).get('date')))
            self.stdout.write(f"DEMO | PostgreSQL={settings.DATABASES['default']['NAME']} | IA={settings.GEMINI_MODEL} | correo=memoria | bot=@{bot['username']}")
            self.stdout.write(f'Actualizaciones remotas visibles: {len(pending)}. Sin confirmar: {unknown}')
            if options['preflight']:
                return
            if unknown or len(pending) == 100:
                raise CommandError('Revisa el contexto de la cola remota antes de iniciar. No se descartó ni confirmó ningún mensaje.')
            settings.CONTENT_LIVE_TELEGRAM = True
            recover_delivery_interruptions()
            processor = Processor()
            incident_handler = None
            offset = None
            last_tick = 0
            self.stdout.write('RECEPTOR DEMO ACTIVO | consola compacta | Ctrl+C para detener')
            try:
                while True:
                    updates = call('getUpdates', {'timeout': 25, 'limit': 20, **({'offset': offset} if offset else {})})
                    for update in updates:
                        # Persistencia confirmada antes de adelantar el offset remoto.
                        try:
                            message = queue_update(update)
                        except PermissionDenied:
                            raise CommandError('Actualización no autorizada; receptor detenido sin descartarla.') from None
                        offset = update['update_id'] + 1
                    candidates = Message.objects.filter(status__in=['pending', 'retry', 'processing'], retry_at__lte=timezone.now()).order_by('pk')[:10]
                    for message in candidates:
                        if Message.objects.filter(participant=message.participant, pk__lt=message.pk,
                                                  status__in=['pending', 'retry', 'processing']).exists():
                            continue
                        conv, _ = Conversation.objects.get_or_create(participant=message.participant)
                        if message.channel == 'telegram' and conv.module == 'incident' and not message.text.startswith('/') and not content_only(message.participant):
                            if incident_handler is None:
                                incident_handler = TelegramUpdateHandler()
                            # La ruta de incidencias conserva su propio inbox y deduplicación.
                            result = incident_handler.handle(message.payload['update'])
                            if result.get('action') == 'processing_failed':
                                raise CommandError('Incidencias no confirmó persistencia. Mensaje conservado en el inbox demo; revisa antes de reiniciar.')
                            message.status = 'done'
                            message.result = {'module': 'incident'}
                            message.save(update_fields=['status', 'result'])
                        else:
                            processor.process(message)
                        message.refresh_from_db()
                        self.stdout.write(f'mensaje {message.pk} | {message.status} | intentos={message.attempts}')
                    if time.monotonic() - last_tick >= settings.CONTENT_TICK_SECONDS:
                        schedule_reminders(settings.CONTENT_REMINDER_MINUTES)
                        if incident_handler is not None:
                            incident_handler.recover_pending(limit=1)
                            drain_notifications()
                        last_tick = time.monotonic()
                    for _ in range(20):
                        out = deliver_one()
                        if out is None:
                            break
                        self.stdout.write(f'aviso {out.pk} | {out.status}')
            except KeyboardInterrupt:
                self.stdout.write('RECEPTOR DEMO DETENIDO. Los mensajes y avisos permanecen guardados.')
