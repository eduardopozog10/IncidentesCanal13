"""Solo evaluación offline: ninguna conexión a bases operativas o de demo."""
from copy import deepcopy

from .qa import *  # noqa: F403

DATABASES = deepcopy(DATABASES)  # noqa: F405
# Nombre deliberadamente inexistente: DiscoverRunner usa PostgreSQL de
# mantenimiento para crear TEST.NAME antes de abrir la conexión de la aplicación.
DATABASES["default"]["NAME"] = "secuoya_evaluation_unused"
DATABASES["default"]["TEST"] = {"NAME": "test_secuoya_incident_evaluation"}
INCIDENT_EVALUATION_ONLY = True
GEMINI_API_KEY = ""
EMAIL_HOST_USER = ""
INCIDENT_PUBLIC_BASE_URL = ""
