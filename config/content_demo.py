"""Demo independiente. Nunca selecciona la base operativa ni envía SMTP."""
from .qa import *  # noqa: F403
from .settings import GEMINI_MODEL, GEMINI_API_KEY, TELEGRAM_BOT_TOKEN

CONTENT_COORDINATION_ENABLED = True
INSTALLED_APPS = [*INSTALLED_APPS, 'content_coordination.apps.ContentCoordinationConfig',
                  'telegram_routing.apps.TelegramRoutingConfig']  # noqa: F405
DATABASES['default']['NAME'] = 'demo_secuoya_contenidos'  # noqa: F405
DATABASES['default']['TEST'] = {'NAME': 'test_demo_secuoya_contenidos'}  # noqa: F405
AI_PROVIDER = 'gemini'
CONTENT_REMINDER_MINUTES = 20
CONTENT_TICK_SECONDS = 5
CONTENT_MAX_ATTEMPTS = 3
CONTENT_RETRY_SECONDS = 60
CONTENT_LIVE_TELEGRAM = False  # Solo el comando --live permite entregar mensajes.
CONTENT_ALLOWED_TELEGRAM_IDS = ['6690350935']  # Único consentimiento vigente.
CONTENT_BOT_USERNAME = 'secuoyachile_incidentes_bot'
SESSION_COOKIE_NAME = 'secuoya_content_demo_session'
CSRF_COOKIE_NAME = 'secuoya_content_demo_csrf'
