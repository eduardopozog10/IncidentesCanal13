"""Una sola llamada real al contrato de contenidos, sin escribir datos."""
import json
import time
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from content_coordination.ai import GeminiContentInterpreter, validate_plan


class Command(BaseCommand):
    help = 'Diagnóstico del contrato de contenidos: una petición, sin reintentos ni escrituras.'

    def handle(self, *args, **options):
        text = 'Necesito una gráfica de encuesta, solicitada por Prensa y preparada por Gráfica, sin plazo.'
        provider = GeminiContentInterpreter()
        started = time.perf_counter()
        try:
            result = provider._request_with_deadline(
                model=provider.model, input='Extrae una solicitud de contenido. Usa action create y evidence literal. Mensaje: '+text,
                response_format={'type': 'text', 'mime_type': 'application/json', 'schema': provider.RESPONSE_SCHEMA},
                timeout=30,
            )
            data = validate_plan(json.loads(result.output_text), text)
        except Exception as exc:
            code = getattr(exc, 'code', None) or getattr(exc, 'status_code', None)
            raise CommandError(f'Modelo={settings.GEMINI_MODEL}; tiempo={time.perf_counter()-started:.3f}s; error={type(exc).__name__}; HTTP={code or "sin código"}') from None
        self.stdout.write(f'Modelo={settings.GEMINI_MODEL}; tiempo={time.perf_counter()-started:.3f}s; JSON válido; acciones={[u["action"] for u in data["updates"]]}; reintentos=0')
