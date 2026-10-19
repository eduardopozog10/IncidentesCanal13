from unittest.mock import Mock

from django.contrib.auth import get_user_model
from django.test import TestCase, SimpleTestCase, override_settings
from django.core import mail
from django.urls import reverse

from incidents.ai.schemas import IncidentExtractionResult
from incidents.models import Incident, IncidentDraft, Technician, TechnicianChannel
from incidents.services.incident_processor import IncidentProcessor
from incidents.services.incoming_message_service import IncomingMessageService
from incidents.services.incident_notification_service import build_incident_url, send_incident_created_email


AUDIO = dict(unit="Estudio 2", location_context="Estudio 2", area="Audio", equipment="Consola de audio", asset_category="audio", failure_type="Un canal de audio no funciona", description="La consola de audio tiene un canal sin funcionar.", operational_impact="Solo podemos usar un canal de audio", priority="high")


class MultipleCorrectionTests(TestCase):
    def setUp(self):
        self.reporter = Technician.objects.create(full_name="QA")
        TechnicianChannel.objects.create(technician=self.reporter, provider="telegram", external_user_id="correction-qa")
        self.ai = Mock()
        self.processor = IncidentProcessor(self.ai)
        self.service = IncomingMessageService(self.processor)
        self.number = 0

    def send(self, text):
        self.number += 1
        return self.service.process_text(provider="telegram", external_user_id="correction-qa", external_chat_id="correction-qa", external_message_id=str(self.number), text=text, raw_payload={})["processing"]

    def make_draft(self, priority="high", question=None):
        data = {**AUDIO, "priority": priority}
        return IncidentDraft.objects.create(technician=self.reporter, extracted_data=data, status="ready" if priority else "collecting", missing_fields=[] if priority else ["priority"], last_question=question or self.processor._build_confirmation_reply(data))

    def test_stadium_corrections_invalidate_audio_and_require_current_confirmation(self):
        draft = self.make_draft()
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**AUDIO, "equipment": "Cámara 3", "location_context": "Estadio Monumental"})
        first = self.send("Era en el Estadio Monumental y lo que falla es la cámara 3")
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data["location_context"], "Estadio Monumental")
        self.assertEqual(draft.extracted_data["equipment"], "Cámara 3")
        self.assertEqual(draft.extracted_data["asset_category"], "video_cameras")
        for key in ("failure_type", "description", "operational_impact"):
            self.assertIsNone(draft.extracted_data[key])
        self.assertEqual(draft.extracted_data["priority"], "high")
        self.assertIn("failure_type", first["missing_fields"])
        self.assertEqual(draft.status, "collecting")
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**draft.extracted_data, "equipment": "Cámara 1", "location_context": "Estadio Nacional"})
        self.send("Perdón, nada que ver, estamos en el Estadio Nacional y era la cámara 1")
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data["location_context"], "Estadio Nacional")
        self.assertEqual(draft.extracted_data["equipment"], "Cámara 1")
        self.assertEqual(Incident.objects.count(), 0)
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**draft.extracted_data, "failure_type": "No enciende", "description": "La Cámara 1 no enciende", "operational_impact": "No podemos grabar"})
        self.send("La Cámara 1 no enciende y no podemos grabar")
        draft.refresh_from_db()
        self.assertEqual(draft.status, "ready")
        self.assertIn("Estadio Nacional", draft.last_question)
        self.assertNotIn("canal de audio", draft.last_question)
        self.assertEqual(Incident.objects.count(), 0)
        self.assertTrue(self.send("confirmo")["incident_created"])
        self.assertEqual(Incident.objects.get().equipment, "Cámara 1")

    def test_multiple_correction_while_waiting_priority(self):
        draft = self.make_draft(priority=None, question=self.processor.FALLBACK_QUESTIONS["priority"])
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**AUDIO, "priority": None, "equipment": "Cámara 3", "location_context": "Parque del Río"})
        self.send("Era en Parque del Río y lo que falla es la cámara 3")
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data["location_context"], "Parque del Río")
        self.assertIsNone(draft.extracted_data["priority"])
        self.assertEqual(draft.extracted_data["equipment"], "Cámara 3")

    def test_new_explicit_details_survive_family_change(self):
        draft = self.make_draft()
        updated = {**AUDIO, "equipment": "Cámara 1", "failure_type": "No enciende", "description": "La Cámara 1 no enciende", "operational_impact": "No podemos grabar"}
        self.ai.extract_incident.return_value = IncidentExtractionResult(**updated)
        self.send("No era la consola, era la Cámara 1. La Cámara 1 no enciende. No podemos grabar")
        draft.refresh_from_db()
        for key in ("failure_type", "description", "operational_impact"):
            self.assertEqual(draft.extracted_data[key], updated[key])
        self.assertEqual(draft.status, "ready")
        self.assertEqual(Incident.objects.count(), 0)

    def test_single_equipment_correction_is_not_answer_to_priority(self):
        draft = self.make_draft(priority=None, question=self.processor.FALLBACK_QUESTIONS["priority"])
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**AUDIO, "priority": None, "equipment": "Cámara 1"})
        self.send("No era la consola, era la cámara 1")
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data["equipment"], "Cámara 1")
        self.assertIsNone(draft.extracted_data["priority"])

    def test_explicit_location_beats_previous_correction_question_without_ai(self):
        draft = self.make_draft(question=self.processor.CORRECTION_QUESTIONS["equipment"])
        draft.status = "collecting"
        draft.save()
        self.send("La ubicación es el Control Central")
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data["location_context"], "Control Central")
        self.ai.extract_incident.assert_not_called()

    def test_explicit_priority_is_preserved_without_ai(self):
        draft = self.make_draft()
        self.send("En realidad la prioridad es crítica")
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data["priority"], "critical")
        self.ai.extract_incident.assert_not_called()


class PublicURLTests(SimpleTestCase):
    @override_settings(INCIDENT_PUBLIC_BASE_URL="http://127.0.0.1:8000")
    def test_local_pilot_requires_debug(self):
        with override_settings(DEBUG=True):
            self.assertEqual(build_incident_url(40), "http://127.0.0.1:8000/dashboard/incidents/40/")
        with override_settings(DEBUG=False):
            self.assertIsNone(build_incident_url(40))

    def test_rejects_unusable_or_local_urls(self):
        for url in ("", "http://public.example.org", "https://localhost", "https://localhost.", "https://127.0.0.1", "https://10.0.0.1", "javascript:alert(1)", "https://user:secret@example.org", "https://example.org?x=1"):
            with self.subTest(url=url), override_settings(INCIDENT_PUBLIC_BASE_URL=url):
                self.assertIsNone(build_incident_url(38))

    @override_settings(INCIDENT_PUBLIC_BASE_URL="https://incidencias.example.org/")
    def test_url_uses_real_incident_id(self):
        self.assertEqual(build_incident_url(38), "https://incidencias.example.org/dashboard/incidents/38/")
        self.assertNotEqual(build_incident_url(38), build_incident_url(39))


@override_settings(INCIDENT_PUBLIC_BASE_URL="https://incidencias.example.org", EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", INCIDENT_NOTIFICATION_EMAIL="qa@example.invalid")
class SecureIncidentLinkTests(TestCase):
    def setUp(self):
        self.incident = Incident.objects.create(technician=Technician.objects.create(full_name="QA"), **AUDIO)
        self.path = reverse("dashboard-incident-detail", kwargs={"pk": self.incident.pk})

    def test_email_has_correct_button_and_plain_link(self):
        self.assertTrue(send_incident_created_email(self.incident.pk))
        url = build_incident_url(self.incident.pk)
        self.assertIn(url, mail.outbox[0].body)
        self.assertIn(f'href="{url}"', mail.outbox[0].alternatives[0].content)
        self.assertIn("Ver incidencia", mail.outbox[0].alternatives[0].content)

    @override_settings(DEBUG=True, INCIDENT_PUBLIC_BASE_URL="http://127.0.0.1:8000")
    def test_local_pilot_email_button_uses_actual_id(self):
        self.assertTrue(send_incident_created_email(self.incident.pk))
        url = f"http://127.0.0.1:8000/dashboard/incidents/{self.incident.pk}/"
        self.assertIn(url, mail.outbox[0].body)
        self.assertIn(f'href="{url}"', mail.outbox[0].alternatives[0].content)

    def test_anonymous_and_nonstaff_cannot_access_incidents(self):
        response = self.client.get(self.path)
        self.assertRedirects(response, "/admin/login/?next=" + self.path, fetch_redirect_response=False)
        paths = ("/api/v1/incidents/", f"/api/v1/incidents/{self.incident.pk}/", "/api/v1/incidents/summary/", "/api/v1/incidents/analytics/")
        for path in paths:
            self.assertIn(self.client.get(path).status_code, (401, 403))
        self.client.force_login(get_user_model().objects.create_user(username="viewer"))
        self.assertEqual(self.client.get(self.path).status_code, 302)
        for path in paths:
            self.assertEqual(self.client.get(path).status_code, 403)

    def test_staff_reaches_selected_incident_and_missing_returns_404(self):
        self.client.force_login(get_user_model().objects.create_user(username="manager", is_staff=True))
        response = self.client.get(self.path)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"const linkedIncidentId = {self.incident.pk};")
        self.assertEqual(self.client.get(f"/api/v1/incidents/{self.incident.pk}/").status_code, 200)
        self.assertEqual(self.client.get("/dashboard/incidents/999999/ ".strip()).status_code, 404)

    def test_login_returns_staff_to_requested_incident(self):
        get_user_model().objects.create_user(username="qa-login", password="test-only-password", is_staff=True)
        response = self.client.post("/admin/login/?next=" + self.path, {"username": "qa-login", "password": "test-only-password", "next": self.path})
        self.assertRedirects(response, self.path)
