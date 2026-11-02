"""Receptor temporal exclusivo para la cuenta QA autorizada.

No cambia .env ni config.settings. Detener con Ctrl+C al terminar.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DJANGO_SETTINGS_MODULE"] = "config.qa"
import django
django.setup()

from django.conf import settings
from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_GROUP_WINDOW_SECONDS
from incidents.models import Technician, TechnicianChannel
from incidents.management.commands import run_telegram_bot

if settings.DATABASES["default"]["NAME"] != "qa_secuoya_20261019" or not settings.EMAIL_BACKEND.endswith("locmem.EmailBackend"):
    raise SystemExit("QA requiere la base aislada y correo en memoria.")
settings.TELEGRAM_BOT_TOKEN = TELEGRAM_BOT_TOKEN
settings.AI_PROVIDER = "gemini"
settings.TELEGRAM_GROUP_WINDOW_SECONDS = TELEGRAM_GROUP_WINDOW_SECONDS
AUTHORIZED_ID = os.environ.get("SECUOYA_QA_REPORTER_ID", "").strip()
if not AUTHORIZED_ID:
    raise SystemExit("Define SECUOYA_QA_REPORTER_ID con la cuenta autorizada antes de iniciar QA.")

import httpx
try:
    with httpx.Client(timeout=12) as client:
        base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
        identity = client.get(base + "/getMe").json()
        webhook = client.get(base + "/getWebhookInfo").json()
except Exception:
    # Las excepciones HTTP pueden contener la URL con el token del bot.
    raise SystemExit("No fue posible verificar Telegram. Revisa conectividad y configuración local.") from None
if identity.get("result", {}).get("username") != "secuoyachile_incidentes_bot":
    raise SystemExit("El bot configurado no es Secuoya; no se inicia QA.")
if webhook.get("result", {}).get("url"):
    raise SystemExit("Hay un webhook activo; no se inicia QA.")

account = TechnicianChannel.objects.filter(provider="telegram", external_user_id=AUTHORIZED_ID).first()
if account is None:
    reporter = Technician.objects.create(full_name="Reportante autorizado QA Secuoya")
    account = TechnicianChannel.objects.create(technician=reporter, provider="telegram", external_user_id=AUTHORIZED_ID, external_chat_id=AUTHORIZED_ID)


class RestrictedQAHandler(run_telegram_bot.TelegramUpdateHandler):
    def __init__(self):
        if "--timeout-next" in sys.argv:
            from incidents.ai.gemini_provider import GeminiAIProvider
            from incidents.services.incident_processor import IncidentProcessor
            from incidents.services.incoming_message_service import IncomingMessageService

            class FaultOnce:
                def __init__(self):
                    self.provider = GeminiAIProvider()
                    self.pending = True

                def extract_incident(self, message, previous_data=None):
                    if self.pending:
                        self.pending = False
                        raise TimeoutError("Interrupción QA controlada de una sola llamada")
                    return self.provider.extract_incident(message, previous_data)

            super().__init__(incoming_service=IncomingMessageService(IncidentProcessor(FaultOnce(), require_delivered_summary=True)))
        else:
            super().__init__()

    def handle(self, update, **kwargs):
        message = update.get("message", {})
        if str(message.get("from", {}).get("id")) != AUTHORIZED_ID or message.get("chat", {}).get("type") != "private":
            # No responder ni procesar reportes de otra persona en QA.
            raise SystemExit("Se recibió un mensaje de otra cuenta: detener QA y revisar manualmente.")
        return super().handle(update, **kwargs)


run_telegram_bot.TelegramUpdateHandler = RestrictedQAHandler
print("QA SECUOYA: PostgreSQL aislado, Gemini real, correo en memoria, un reportante autorizado.", flush=True)
run_telegram_bot.Command().handle(discard_pending=False)
