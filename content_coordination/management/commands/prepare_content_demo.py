"""Preparación aditiva: únicamente la base de contenidos y usuarios de demo."""
import psycopg
from psycopg import sql
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from content_coordination.models import Participant
from incidents.models import Technician, TechnicianChannel
from .run_secuoya_demo import ensure_demo


class Command(BaseCommand):
    help = 'Crea exclusivamente demo_secuoya_contenidos, aplica migraciones y prepara usuarios sin contraseña.'

    def handle(self, *args, **options):
        ensure_demo()
        db = settings.DATABASES['default']
        try:
            with psycopg.connect(dbname='postgres', user=db['USER'], password=db['PASSWORD'],
                                  host=db['HOST'], port=db['PORT'], autocommit=True, connect_timeout=5) as conn:
                exists = conn.execute('SELECT 1 FROM pg_database WHERE datname=%s', [db['NAME']]).fetchone()
                if not exists:
                    conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(db['NAME'])))
        except psycopg.Error as exc:
            raise CommandError(f'No se pudo preparar PostgreSQL ({type(exc).__name__}). No se modificó la base operativa.') from None
        call_command('migrate', interactive=False, verbosity=1, stdout=self.stdout)
        users = [
            ('demo_admin', 'Eduardo Pozo', 'admin', '6690350935'),
            ('demo_periodista', 'Periodista', 'journalist', None),
            ('demo_fernando', 'Fernando', 'coordinator', None),
            ('demo_daniela', 'Daniela', 'designer', None),
            ('demo_felipe', 'Felipe', 'designer', None),
            ('demo_camila', 'Camila', 'designer', None),
            ('demo_revisor', 'Revisor', 'reviewer', None),
            ('demo_editor', 'Editor', 'editor', None),
            ('demo_switch', 'Switch', 'switch', None),
        ]
        with transaction.atomic():
            for username, name, role, telegram_id in users:
                user, created = get_user_model().objects.get_or_create(username=username)
                if created:
                    user.set_unusable_password()
                    user.first_name = name
                    user.save()
                participant, created = Participant.objects.get_or_create(user=user, defaults={
                    'name': name, 'role': role, 'telegram_id': telegram_id,
                    'telegram_authorized': telegram_id is not None,
                })
                if not created and (participant.role != role or (telegram_id is not None and participant.telegram_id != telegram_id)):
                    raise CommandError(f'{username} ya tiene otra configuración. No se sobrescribió.')
            if not TechnicianChannel.objects.filter(provider='telegram', external_user_id='6690350935').exists():
                reporter = Technician.objects.create(full_name='Eduardo Pozo · Demo contenidos')
                TechnicianChannel.objects.create(technician=reporter, provider='telegram',
                                                 external_user_id='6690350935', external_chat_id='6690350935')
        self.stdout.write('DEMO preparada. Se conservaron los vínculos autorizados existentes. No se crearon solicitudes ni incidencias.')
        self.stdout.write('Configura el acceso web: uv run python manage.py changepassword demo_admin --settings=config.content_demo')
