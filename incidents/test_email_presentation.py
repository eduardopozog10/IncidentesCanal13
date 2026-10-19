from unittest.mock import patch

from django.core import mail
from django.template.loader import render_to_string
from django.test import TestCase, SimpleTestCase, override_settings

from incidents.models import Incident, Technician
from incidents.services.incident_notification_service import send_incident_created_email


class EmailTemplateTests(SimpleTestCase):
    def test_optional_fields_and_button(self):
        html = render_to_string("incidents/email/incident_created.html", {})
        self.assertIn("No informada", html)
        self.assertIn("No informado", html)
        self.assertNotIn("Ver reporte", html)
        self.assertNotIn('href=', html)
        # Context-only example; no public link is configured in the sender.
        html = render_to_string("incidents/email/incident_created.html", {
            "incident_url": "https://example.org/reportes/38/",
        })
        self.assertIn('href="https://example.org/reportes/38/"', html)


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    INCIDENT_NOTIFICATION_EMAIL="qa@example.invalid",
)
class EmailPresentationTests(TestCase):
    def setUp(self):
        self.incident = Incident.objects.create(
            technician=Technician.objects.create(full_name="Reportante QA"),
            unit="Producción", area="Producción", location_context="Móvil",
            equipment="Monitores", failure_type="No encienden",
            description='Texto original <script>alert("x")</script>\nSegunda línea.',
            operational_impact="No es posible monitorear las demás cámaras.",
            priority=Incident.Priority.HIGH,
        )

    def test_send_preserves_plain_text_and_adds_escaped_html(self):
        self.assertTrue(send_incident_created_email(self.incident.pk))
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["qa@example.invalid"])
        self.assertIn(self.incident.description, message.body)
        self.assertIn(self.incident.code, message.subject)
        html, mime = message.alternatives[0]
        self.assertEqual(mime, "text/html")
        for value in (self.incident.code, "Producción", "Móvil", "Monitores", "Alta", "Reportado", self.incident.operational_impact):
            self.assertIn(value, html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("<br>", html)
        self.assertNotIn("Ver reporte", html)

    def test_send_failure_still_returns_false(self):
        with patch("incidents.services.incident_notification_service.send_mail", side_effect=RuntimeError("QA")):
            self.assertFalse(send_incident_created_email(self.incident.pk))
