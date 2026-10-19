"""Entorno local aislado. Nunca utiliza la base principal ni envía correos."""
from copy import deepcopy

from .settings import *  # noqa: F403

DATABASES = deepcopy(DATABASES)  # noqa: F405
DATABASES["default"]["NAME"] = "qa_secuoya_20261019"
DATABASES["default"]["TEST"] = {"NAME": "test_qa_secuoya_20261019"}
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
EMAIL_HOST_PASSWORD = ""
INCIDENT_NOTIFICATION_EMAIL = "qa@example.invalid"
DEFAULT_FROM_EMAIL = "secuoya-qa@example.invalid"
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_AUTO_REGISTER = False
AI_PROVIDER = "mock"
DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]
