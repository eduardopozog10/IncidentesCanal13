"""15 recorridos con Gemini real y PostgreSQL QA; sin transporte Telegram.

uv run python scripts/qa_scenarios.py --round initial
uv run python scripts/qa_scenarios.py --round retest --cases 11 12
La creación de la base se hace una vez con --prepare. No elimina datos.
"""
import argparse
import json
import os
import sys
import unicodedata
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["DJANGO_SETTINGS_MODULE"] = "config.qa"

import django
django.setup()

from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.test import Client
from incidents.ai.gemini_provider import GeminiAIProvider
from incidents.models import ChannelMessage, Incident, IncidentDraft, Technician, TechnicianChannel
from incidents.services.incident_processor import IncidentProcessor
from incidents.services.incoming_message_service import IncomingMessageService


CASES = [
    ("Cámara completa", ["En Producción, Cámara 3 del Móvil 2: la imagen se corta y queda negra cada pocos segundos, no podemos usarla durante la producción en vivo. Prioridad alta."], "Producción", "Móvil 2", "Cámara 3", "video_cameras", "high"),
    ("Consola de audio", ["Área Audio, consola Yamaha CL5 del Estudio 2 tiene cortes intermitentes de audio durante el programa. Obliga a usar la consola de respaldo. Prioridad media."], "Audio", "Estudio 2", "Yamaha CL5", "audio", "medium"),
    ("Micrófono incompleto", ["El micrófono no funciona"], "Audio", "Estudio 3", "Micrófono 2", "audio", "medium"),
    ("Señal caída", ["Área Transmisión: el enlace principal de Control Central quedó sin señal. La emisión en vivo está interrumpida y no tenemos respaldo. Prioridad crítica."], "Transmisión", "Control Central", "enlace principal", "signal_transmission", "critical"),
    ("Móvil en terreno", ["En Operaciones, el intercom del Móvil 4 en cobertura exterior no transmite retorno. No podemos coordinar a los camarógrafos, estamos usando teléfonos. Prioridad media."], "Operaciones", "Móvil 4", "intercom", "audio", "medium"),
    ("Switcher", ["En Producción, switcher ATEM del Control de realización: conmuta solo y corta la imagen cada minuto. Impide realizar el programa, no hay alternativa. Prioridad alta."], "Producción", "Control de realización", "ATEM", "video_cameras", "high"),
    ("Red", ["En Redes, router principal de Sala técnica sin conexión. Se perdió acceso a los servicios de producción y hay que usar un respaldo. Prioridad media."], "Redes", "Sala técnica", "router", "networks", "medium"),
    ("Streaming", ["En Digital, el encoder de streaming de Plataforma digital deja de enviar video. Se interrumpe la transmisión digital en vivo. Prioridad crítica."], "Digital", "Plataforma digital", "encoder", "digital_streaming", "critical"),
    ("Eléctrico", ["Área Infraestructura: UPS 2 de Sala eléctrica no entrega alimentación. Apagó los equipos técnicos de respaldo, la operación sigue con el circuito principal. Prioridad media."], "Infraestructura", "Sala eléctrica", "UPS 2", "infrastructure", "medium"),
    ("Fragmentados", ["Tenemos una falla en la cámara del móvil", "Es el Móvil 2", "Cámara 3", "Se va a negro cada pocos segundos", "Área Producción; no podemos usarla en el programa y usamos respaldo. Prioridad media."], "Producción", "Móvil 2", "Cámara 3", "video_cameras", "medium"),
    ("Corrección múltiple", ["En Producción, Cámara 3 del Móvil 2 pierde imagen a ratos. No podemos usarla en el programa. Prioridad crítica.", "Me equivoqué: es Cámara 4, ocurre en Estudio 8 y la prioridad es alta, no crítica."], "Producción", "Estudio 8", "Cámara 4", "video_cameras", "high"),
    ("Contradicción", ["En Producción, puede ser Cámara 3 o Cámara 4 del Estudio 2; no sé cuál es. La imagen se corta y obliga a usar respaldo. Prioridad media.", "Revisé: es la Cámara 4. No está completamente caída la imagen, es intermitente."], "Producción", "Estudio 2", "Cámara 4", "video_cameras", "medium"),
    ("Coloquial", ["Oye en produccion la kamara 3 del movil 2 se va a negro a cada rato 😕; no la podemos ocupar pal programa en vivo. Prioridad alta, porfa."], "Producción", "Móvil 2", "Cámara 3", "video_cameras", "high"),
    ("Confirmaciones", ["sí", "En Sistemas, servidor de playout 1 de Sala técnica no arranca. No podemos cargar material para el programa y no hay respaldo. Prioridad alta."], "Sistemas", "Sala técnica", "playout", "systems", "high"),
    ("Recuperación", ["En Redes, router principal de Sala técnica sin conexión", "Además impide enviar material de producción; usamos un respaldo. Prioridad media.", "continuar"], "Redes", "Sala técnica", "router", "networks", "medium"),
]


def normalized(value):
    return "".join(c for c in unicodedata.normalize("NFD", str(value).casefold()) if unicodedata.category(c) != "Mn")


class ControlledProvider:
    def __init__(self, recover=False):
        self.provider = GeminiAIProvider()
        self.recover = recover
        self.calls = 0

    def extract_incident(self, message, previous_data=None):
        self.calls += 1
        if self.recover and self.calls == 2:
            raise TimeoutError("Interrupción QA controlada")
        return self.provider.extract_incident(message, previous_data)


def prepare():
    import psycopg
    from psycopg import sql
    db = settings.DATABASES["default"]
    with psycopg.connect(dbname="postgres", user=db["USER"], password=db["PASSWORD"], host=db["HOST"], port=db["PORT"], autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", [db["NAME"]]).fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {} ").format(sql.Identifier(db["NAME"])))
    call_command("migrate", verbosity=0, interactive=False)


def run_case(number, spec, round_name):
    title, texts, area, location, equipment, family, priority = spec
    reporter = Technician.objects.create(full_name=f"QA {round_name} {number:02d}")
    identity = f"qa-{uuid4()}"
    TechnicianChannel.objects.create(technician=reporter, provider="telegram", external_user_id=identity, external_chat_id=identity)
    service = IncomingMessageService(IncidentProcessor(ControlledProvider(number == 15)))
    transcript, issues = [], []
    expected = dict(area=area, location_context=location, equipment=equipment, asset_category=family, priority=priority)
    result = {"id": f"{number:02d}", "title": title, "level": "B (Gemini real; timeout simulado)" if number == 15 else "B", "expected": expected, "transcript": transcript, "issues": issues}

    def send(text):
        response = service.process_text(provider="telegram", external_user_id=identity, external_chat_id=identity, external_message_id=f"{identity}:{len(transcript)+1}", text=text, raw_payload={"qa": True})
        processing = response["processing"]
        transcript.append({"user": text, "bot": processing["reply"], "action": processing["action"], "data": processing.get("extracted_data"), "missing": processing.get("missing_fields")})
        return processing

    try:
        for index, text in enumerate(texts):
            processing = send(text)
            if index == 0 and number in {1, 2, 4, 6, 7, 8, 9, 13} and processing.get("missing_fields"):
                issues.append(f"Mensaje completo: pregunta datos entregados: {processing['missing_fields']}")
            if index == 0 and number == 12 and processing.get("extracted_data", {}).get("equipment"):
                issues.append("Equipo ambiguo aceptado sin aclaración")
            if Incident.objects.filter(technician=reporter).exists():
                issues.append("Incidente creado antes de confirmar resumen")

        answers = dict(area=area, location_context=location, equipment="Micrófono 2" if number == 3 else equipment, failure_type="No entrega audio" if number == 3 else "Pérdida intermitente de conexión", description="El micrófono 2 no entrega audio al hablar durante el programa." if number == 3 else "La señal del equipo se interrumpe durante la producción.", operational_impact="Obliga a usar respaldo durante el programa", priority={"high": "Alta", "medium": "Media", "critical": "Crítica"}[priority])
        if number == 5:
            answers["equipment"] = "Intercom Riedel 1"
        for _ in range(9):
            draft = IncidentDraft.objects.filter(technician=reporter, status__in=["collecting", "ready"]).first()
            if draft is None or draft.status == "ready":
                break
            missing = draft.missing_fields
            if not missing:
                break
            processing = send(answers[missing[0]])

        draft = IncidentDraft.objects.filter(technician=reporter).first()
        result["extracted"] = draft.extracted_data if draft else {}
        for field, value in expected.items():
            if normalized(value) not in normalized(result["extracted"].get(field, "")):
                issues.append(f"{field}: esperado {value!r}; observado {result['extracted'].get(field)!r}")
        if draft and draft.status == "ready":
            processing = send("confirmo")
            draft.refresh_from_db()
            if number == 14:
                send("confirmo")
            incidents = list(Incident.objects.filter(technician=reporter))
            if len(incidents) != 1:
                issues.append(f"Confirmación: {len(incidents)} incidentes")
            if incidents:
                incident = incidents[0]
                result["code"] = incident.code
                result["incident_id"] = incident.pk
                result["persisted"] = {field: getattr(incident, field) for field in expected}
                result["status"] = incident.status
                result["created_at"] = incident.created_at.isoformat()
                api = Client().get(f"/api/v1/incidents/{incident.pk}/")
                result["dashboard_api_status"] = api.status_code
                result["trace_messages"] = ChannelMessage.objects.filter(draft=draft, incident=incident).count()
                if api.status_code != 200 or incident.status != "reported" or not draft.incident_id:
                    issues.append("Persistencia, trazabilidad o API incorrecta")
        else:
            issues.append("No llegó a resumen válido; no se creó incidente manualmente")
    except Exception as error:
        issues.append(f"Fallo integración: {type(error).__name__}")
    result["result"] = "Aprobado" if not issues else "Fallo conversacional/funcional"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--round", default="initial")
    parser.add_argument("--cases", nargs="+", type=int)
    args = parser.parse_args()
    if connection.settings_dict["NAME"] != "qa_secuoya_20261019" or not settings.EMAIL_BACKEND.endswith("locmem.EmailBackend") or settings.TELEGRAM_BOT_TOKEN:
        raise SystemExit("QA requiere base aislada, correo en memoria y Telegram deshabilitado.")
    if args.prepare:
        prepare()
    folder = ROOT / "docs" / "qa-2026-10-19"
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / f"{args.round}.json"
    if destination.exists():
        raise SystemExit("Ese informe ya existe; use otro nombre de ronda para conservar evidencia.")
    results = []
    for number, spec in enumerate(CASES, 1):
        if args.cases and number not in args.cases:
            continue
        result = run_case(number, spec, args.round)
        results.append(result)
        destination.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{number:02d}: {result['result']} {result.get('code', '')}; {result['issues']}", flush=True)


if __name__ == "__main__":
    main()
