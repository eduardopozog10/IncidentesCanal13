from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse


class DashboardSessionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="staff-session-qa", first_name="Ana", last_name="Pérez", is_staff=True,
        )
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.user)

    def test_welcome_uses_full_name_and_shared_controls(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Ana Pérez", count=1)
        self.assertContains(response, 'action="/dashboard/logout/"', count=1)
        self.assertContains(response, 'method="post"', count=1)
        self.assertContains(response, "Bienvenido,", count=1)
        self.assertNotContains(response, "Sincronizado")
        self.assertContains(response, 'name="csrfmiddlewaretoken"')

    def test_welcome_falls_back_to_username_and_escapes_name(self):
        self.user.first_name = ""
        self.user.last_name = ""
        self.user.save()
        self.assertContains(self.client.get(reverse("dashboard")), self.user.username)
        self.user.first_name = "<script>"
        self.user.save()
        self.assertContains(self.client.get(reverse("dashboard")), "&lt;script&gt;")

    def test_logout_requires_post_and_valid_csrf(self):
        self.assertEqual(self.client.get(reverse("dashboard-logout")).status_code, 405)
        self.assertEqual(self.client.post(reverse("dashboard-logout")).status_code, 403)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
        token = self.client.cookies["csrftoken"].value
        response = self.client.post(reverse("dashboard-logout"), {"csrfmiddlewaretoken": token})
        self.assertRedirects(response, reverse("admin:login"))
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 302)
        self.assertIn(self.client.get("/api/v1/incidents/").status_code, (401, 403))

    def test_dashboard_cannot_be_cached_and_restored_without_revalidation(self):
        response = self.client.get(reverse("dashboard"))
        for directive in ("no-store", "no-cache", "must-revalidate", "private"):
            self.assertIn(directive, response.headers["Cache-Control"])
        self.assertContains(response, 'if (event.persisted) window.location.reload();')
