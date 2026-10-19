"""Render synthetic email examples without database access or sending mail.

uv run python scripts/preview_incident_email.py
"""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.qa")

import django
django.setup()
from django.template.loader import render_to_string

output = ROOT / ".qa" / "email-preview"
output.mkdir(parents=True, exist_ok=True)
context = {
    "incident": {
        "code": "SC-000038", "area": "Producción", "location_context": "Móvil",
        "equipment": "Monitores", "failure_type": "No encienden",
        "description": "Los monitores de monitoreo en el móvil no encienden.",
        "operational_impact": "No es posible monitorear las demás cámaras.",
    },
    "priority": "Alta", "status": "Reportado", "reporter": "Reportante QA",
    "created_at": "19/10/2026 10:30", "asset_category": "Móviles / Terreno",
}
for name, data in (
    ("correo.html", context),
    ("campos-opcionales.html", {}),
    ("boton-condicional.html", {**context, "incident_url": "https://example.org/dashboard/incidents/38/"}),
):
    (output / name).write_text(render_to_string("incidents/email/incident_created.html", data), encoding="utf-8")
print(output)
