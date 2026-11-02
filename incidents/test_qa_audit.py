"""Regresiones encontradas en auditoría; ejecutar con config.qa, nunca proveedores reales."""
import io
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib import admin
from django.test import TestCase, override_settings
from django.contrib.auth.models import Group
from django.urls import reverse

from incidents.ai.schemas import IncidentExtractionResult
from incidents.models import Incident, IncidentChange, IncidentDraft, TechnicianChannel
from incidents.services.incident_processor import IncidentProcessor
from incidents.services.incoming_message_service import IncomingMessageService
from incidents.test_incident_history import incident_fixture
from incidents.test_correction_feedback import BASE
from incidents.test_user_management import SETTINGS, account, initialize
from incidents.management.commands.run_telegram_bot import Command


@override_settings(**SETTINGS)
class AuditRegressionTests(TestCase):
    def setUp(self):
        initialize()
        self.user = account("Supervisor", "qa-auditor")
        self.client.force_login(self.user)
        self.incident = incident_fixture()

    def test_invalid_and_impossible_dates_are_validation_errors(self):
        for endpoint in ("/api/v1/incidents/", "/api/v1/incidents/summary/", "/api/v1/incidents/analytics/"):
            for params in ({"date_from": "2026-02-30"}, {"date_to": "garbage"},
                           {"date_from": "9999999999999999-01-01"}, {"date_from": "2026-10-20", "date_to": "2026-10-19"}):
                with self.subTest(endpoint=endpoint, params=params):
                    self.assertEqual(self.client.get(endpoint, params).status_code, 400)

    def test_admin_resolution_dates_match_dashboard_rules(self):
        path = reverse("admin:incidents_incident_change", args=[self.incident.pk])
        self.assertEqual(self.client.post(path, {"status": "resolved", "priority": "medium", "_save": "Guardar"}).status_code, 302)
        self.incident.refresh_from_db()
        self.assertIsNotNone(self.incident.resolved_at)
        self.assertEqual(self.incident.changes.count(), 1)
        self.client.post(path, {"status": "in_progress", "priority": "medium", "_save": "Guardar"})
        self.incident.refresh_from_db()
        self.assertIsNone(self.incident.resolved_at)
        self.assertEqual(self.incident.changes.count(), 2)

    def test_generator_update_fields_cannot_create_false_history(self):
        self.incident.status = "in_progress"
        self.incident.priority = "critical"
        self.incident.save(update_fields=(name for name in ("status", "priority")))
        self.incident.refresh_from_db()
        self.assertEqual((self.incident.status, self.incident.priority), ("in_progress", "critical"))
        self.assertEqual(set(self.incident.changes.values_list("field", flat=True)), {"status", "priority"})

    def test_positional_update_fields_does_not_audit_unsaved_fields(self):
        self.incident.status = "in_progress"
        self.incident.priority = "critical"
        self.incident.save(False, False, None, ["status"])
        self.incident.refresh_from_db()
        self.assertEqual(self.incident.priority, "medium")
        self.assertEqual(list(self.incident.changes.values_list("field", flat=True)), ["status"])

    def test_correction_keeps_description_when_priority_also_changes(self):
        ai = Mock()
        ai.extract_incident.return_value = IncidentExtractionResult(**{**BASE,
            "description": "El audio se corta completamente durante la salida al aire", "priority": "critical"},
            update_kind="correction", field_evidence={"priority": "crítica", "description": "El audio se corta completamente durante la salida al aire"})
        draft = IncidentDraft.objects.create(technician=self.incident.technician, extracted_data=BASE, status="ready")
        TechnicianChannel.objects.create(technician=self.incident.technician, provider="telegram", external_user_id="audit", external_chat_id="audit")
        service = IncomingMessageService(IncidentProcessor(ai))
        result = service.process_text(provider="telegram", external_user_id="audit", external_chat_id="audit",
            external_message_id="audit:1", text="Corrijo la prioridad y la descripción: prioridad crítica. El audio se corta completamente durante la salida al aire", raw_payload={})
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data["description"], ai.extract_incident.return_value.description)
        self.assertEqual(draft.extracted_data["priority"], "critical")
        self.assertFalse(result["processing"]["incident_created"])

    def test_polling_notice_logs_do_not_leak_exception_contents(self):
        stream = io.StringIO()
        command = Command(stderr=stream)
        handler = Mock()
        handler.send_processing_notice.side_effect = RuntimeError("botQA_TOKEN_SECRET/private?password=QA")
        with patch("incidents.management.commands.run_telegram_bot.close_old_connections"):
            command._send_processing_notice(handler=handler, update={})
        self.assertNotIn("QA_TOKEN_SECRET", stream.getvalue())
        self.assertIn("RuntimeError", stream.getvalue())

    def test_explicit_assignment_has_precedence_over_reported_priority(self):
        processor = IncidentProcessor(Mock())
        self.assertEqual(processor._extract_explicit_priority("Dicen crítica, pero asigno prioridad alta, no crítica."), "high")
        self.assertIsNone(processor._extract_explicit_priority("Prioridad alta o crítica"))
        self.assertIsNone(processor._extract_explicit_priority("No confirmo prioridad crítica"))

    def test_area_and_asset_family_can_be_corrected_together_with_evidence(self):
        base = {**BASE, "equipment":"Encoder 1", "asset_category":"digital_streaming"}
        ai = Mock()
        ai.extract_incident.return_value = IncidentExtractionResult(**{**base, "area":"Transmisión", "asset_category":"signal_transmission"},
            update_kind="correction", field_evidence={"area":"Transmisión", "asset_category":"Señal / Transmisión"})
        draft = IncidentDraft.objects.create(technician=self.incident.technician, extracted_data=base, status="ready")
        TechnicianChannel.objects.create(technician=self.incident.technician, provider="telegram", external_user_id="audit-family", external_chat_id="audit-family")
        service = IncomingMessageService(IncidentProcessor(ai))
        service.process_text(provider="telegram", external_user_id="audit-family", external_chat_id="audit-family",
            external_message_id="family:1", text="Corrijo el área a Transmisión y la familia de activo a Señal / Transmisión", raw_payload={})
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data["area"], "Transmisión")
        self.assertEqual(draft.extracted_data["asset_category"], "signal_transmission")

    def test_role_change_during_existing_session_takes_effect_on_next_request(self):
        from incidents.access import ROLE_GROUPS
        self.user.groups.clear()
        self.user.groups.add(Group.objects.get(name=ROLE_GROUPS["Visualizador"]))
        path = f"/api/v1/incidents/{self.incident.pk}/"
        self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.patch(path, {"status":"closed"}, content_type="application/json").status_code, 403)
        self.assertEqual(self.client.get("/dashboard/users/").status_code, 403)
        self.assertFalse(self.incident.changes.exists())

    def test_malformed_and_mass_assignment_requests_do_not_change_incident(self):
        path = f"/api/v1/incidents/{self.incident.pk}/"
        for body in ('[{}]', 'null', '{', '{"status":{"$ne":null}}', '{"status":"closed","technician":1}', '{"status":"closed","actor":1}'):
            with self.subTest(body=body):
                self.assertEqual(self.client.patch(path, body, content_type="application/json").status_code, 400)
        self.incident.refresh_from_db()
        self.assertEqual(self.incident.status, "reported")
        self.assertFalse(self.incident.changes.exists())

    def test_sql_payloads_are_data_and_security_headers_are_preserved(self):
        for query in ("' OR 1=1 --", "<script>alert('QA')</script>", "'); DROP TABLE incidents; --"):
            response = self.client.get("/api/v1/incidents/", {"search":query, "ordering":query})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["count"], 0)
        page = self.client.get("/dashboard/")
        self.assertEqual(page.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(page.headers["X-Frame-Options"], "DENY")
        self.assertIn("no-store", page.headers["Cache-Control"])

    def test_gemini_asset_family_evidence_is_part_of_the_contract(self):
        import json
        from incidents.ai.gemini_provider import GeminiAIProvider
        provider = object.__new__(GeminiAIProvider); provider.model = "qa-no-network"
        output = {**BASE, "intent":"incident_followup", "asset_category":"signal_transmission",
                  "field_evidence":{"asset_category":"Señal / Transmisión"}}
        with patch.object(provider, "_create_interaction_with_retry", return_value=SimpleNamespace(output_text=json.dumps(output))):
            result = provider.extract_incident("La familia es Señal / Transmisión", BASE)
        self.assertEqual(result.field_evidence, {"asset_category":"Señal / Transmisión"})
