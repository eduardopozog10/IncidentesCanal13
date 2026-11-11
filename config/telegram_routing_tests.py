"""Pruebas offline del receptor combinado; nunca abre la base demo/operativa."""
from .incident_evaluation import *  # noqa: F403

INSTALLED_APPS = [*INSTALLED_APPS, 'content_coordination.apps.ContentCoordinationConfig',
                  'telegram_routing.apps.TelegramRoutingConfig']  # noqa: F405
DATABASES['default']['NAME'] = 'secuoya_routing_unused'  # noqa: F405
DATABASES['default']['TEST'] = {'NAME': 'test_secuoya_telegram_routing'}  # noqa: F405
CONTENT_COORDINATION_ENABLED = True
CONTENT_ALLOWED_TELEGRAM_IDS = ['6690350935']
CONTENT_BOT_USERNAME = 'secuoyachile_incidentes_bot'
CONTENT_LIVE_TELEGRAM = False
CONTENT_MAX_ATTEMPTS = 3
CONTENT_RETRY_SECONDS = 60
CONTENT_REMINDER_MINUTES = 20
CONTENT_TICK_SECONDS = 5
TELEGRAM_BOT_TOKEN = 'offline-test-token'
TEST_RUNNER = 'config.offline_test_runner.OfflineTestRunner'
