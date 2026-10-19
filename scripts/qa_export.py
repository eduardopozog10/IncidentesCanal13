"""Exporta evidencia QA sin tokens, raw_payload ni datos de la base principal."""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["DJANGO_SETTINGS_MODULE"] = "config.qa"
import django
django.setup()
from django.conf import settings
from incidents.models import ChannelMessage, Incident, IncidentDraft, TechnicianChannel

parser = argparse.ArgumentParser()
parser.add_argument("name")
args = parser.parse_args()
if settings.DATABASES["default"]["NAME"] != "qa_secuoya_20261019":
    raise SystemExit("Solo se permite exportar la base QA aislada.")
reporter_id = os.environ.get("SECUOYA_QA_REPORTER_ID", "").strip()
if not reporter_id:
    raise SystemExit("Define SECUOYA_QA_REPORTER_ID con la cuenta autorizada.")
account = TechnicianChannel.objects.get(provider="telegram", external_user_id=reporter_id)
messages = list(ChannelMessage.objects.filter(channel_account=account).order_by("pk").values("pk", "direction", "content", "status", "draft_id", "incident_id", "created_at"))
drafts = list(IncidentDraft.objects.filter(technician=account.technician).order_by("pk").values("pk", "status", "extracted_data", "missing_fields", "last_question", "incident_id"))
incidents = list(Incident.objects.filter(technician=account.technician).order_by("pk").values("pk", "area", "location_context", "asset_category", "equipment", "failure_type", "description", "operational_impact", "priority", "status", "created_at"))
destination = ROOT / "docs/qa-2026-10-19" / f"{args.name}.json"
if destination.exists():
    raise SystemExit("Conservar evidencia anterior; elegir otro nombre.")
destination.write_text(json.dumps(dict(level="A: Telegram y Gemini reales; fallos externos inyectados se indican aparte", database=settings.DATABASES["default"]["NAME"], messages=messages, drafts=drafts, incidents=incidents), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print(f"Evidencia: {len(messages)} mensajes, {len(drafts)} borradores, {len(incidents)} incidentes.")
