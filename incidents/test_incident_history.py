"""Trazabilidad de gestión. Ejecutar únicamente con config.qa."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.db import close_old_connections, transaction
from django.db.models.deletion import ProtectedError
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from incidents.models import Incident, IncidentChange, Technician, TechnicianChannel
from incidents.test_user_management import SETTINGS, account, initialize


def incident_fixture():
    return Incident.objects.create(technician=Technician.objects.create(full_name="Reportante QA"),
        unit="Estudio 2", area="Audio", location_context="Estudio 2", equipment="Micrófono QA",
        failure_type="Sin audio", description="No entrega señal", original_message="Reporte QA")


@override_settings(**SETTINGS)
class IncidentHistoryTests(TestCase):
    def setUp(self):
        initialize()
        self.users = {role: account(role, "historial-" + role) for role in
                      ("Administrador", "Supervisor", "Operador", "Visualizador")}
        self.incident = incident_fixture()
        self.path = reverse("incidents:incident-detail", args=[self.incident.pk])
        self.history = reverse("incidents:incident-history", args=[self.incident.pk])
        self.client.force_login(self.users["Operador"])

    def change(self, status="in_progress", **extra):
        return self.client.patch(self.path, {"status": status, **extra}, content_type="application/json")

    def admin_change(self, **data):
        self.client.force_login(self.users["Supervisor"])
        return self.client.post(reverse("admin:incidents_incident_change", args=[self.incident.pk]),
            {"status": self.incident.status, "priority": self.incident.priority, "_save": "Guardar", **data})

    def test_status_records_values_actor_date_observation_and_origin(self):
        now = timezone.now()
        with patch("django.utils.timezone.now", return_value=now):
            self.assertEqual(self.change(observation="Se inició la revisión.").status_code, 200)
        event = self.incident.changes.get()
        self.assertEqual((event.field, event.old_value, event.new_value), ("status", "reported", "in_progress"))
        self.assertEqual(event.action, IncidentChange.Action.STATUS)
        self.assertEqual(event.actor_id, self.users["Operador"].pk)
        self.assertIn(self.users["Operador"].username, event.actor_label)
        self.assertEqual(event.created_at, now)
        self.assertEqual(str(timezone.localtime(event.created_at).tzinfo), "America/Santiago")
        self.assertEqual(event.observation, "Se inició la revisión.")
        self.assertEqual(event.source, "dashboard")
        event_data = self.client.get(self.history).json()["results"][0]
        self.assertEqual((event_data["old_display"], event_data["new_display"]), ("Reportado", "En atención"))

    def test_resolved_observation_remains_optional_and_existing_dates_work(self):
        self.assertEqual(self.change("resolved").status_code, 200)
        self.incident.refresh_from_db()
        resolved = self.incident.resolved_at
        self.assertIsNotNone(resolved)
        self.assertEqual(self.incident.changes.get().observation, "")
        self.change("resolved", observation="Reintento sin cambio")
        self.incident.refresh_from_db()
        self.assertEqual(self.incident.resolved_at, resolved)
        self.assertEqual(self.incident.changes.count(), 1)
        self.change("under_review")
        self.incident.refresh_from_db()
        self.assertIsNone(self.incident.resolved_at)
        self.assertEqual(self.incident.changes.count(), 2)

    def test_reads_and_repeated_or_invalid_requests_do_not_create_events(self):
        self.client.get(self.path)
        self.client.get(self.history)
        self.client.get("/dashboard/?view=management")
        self.assertEqual(self.change("reported", observation="Sin modificación").status_code, 200)
        self.assertEqual(self.change("inventado").status_code, 400)
        self.assertFalse(self.incident.changes.exists())

    def test_observation_length_is_validated_server_side(self):
        self.assertEqual(self.change(observation="x" * 1001).status_code, 400)
        self.assertFalse(self.incident.changes.exists())
        self.assertEqual(self.change(observation="x" * 1000).status_code, 200)
        self.assertEqual(len(self.incident.changes.get().observation), 1000)

    def test_historical_metadata_cannot_be_supplied_by_client(self):
        for name in ("actor", "actor_label", "created_at", "old_value", "new_value", "source", "action", "field"):
            with self.subTest(field=name):
                self.assertEqual(self.change(**{name: "falsificado"}).status_code, 400)
        self.assertFalse(self.incident.changes.exists())

    def test_all_profiles_can_read_but_viewer_cannot_change_status(self):
        for role, user in self.users.items():
            with self.subTest(role=role):
                self.client.force_login(user)
                self.assertEqual(self.client.get(self.history).status_code, 200)
                response = self.change("in_progress")
                self.assertEqual(response.status_code, 403 if role == "Visualizador" else 200)
        self.assertEqual(self.incident.changes.count(), 1)

    def test_priority_keeps_current_permissions_and_api_does_not_gain_priority_editing(self):
        for role, user in self.users.items():
            self.client.force_login(user)
            response = self.change(priority="critical")
            self.assertEqual(response.status_code, 400 if role in {"Administrador", "Supervisor"} else 403)
        self.assertFalse(self.incident.changes.exists())
        for role in ("Operador", "Visualizador"):
            self.client.force_login(self.users[role])
            response = self.client.post(reverse("admin:incidents_incident_change", args=[self.incident.pk]),
                                        {"status": "closed", "priority": "critical"})
            self.assertIn(response.status_code, (302, 403))
        self.incident.refresh_from_db()
        self.assertEqual(self.incident.priority, "medium")

    def test_anonymous_and_telegram_reporters_do_not_get_dashboard_access(self):
        TechnicianChannel.objects.create(technician=self.incident.technician, provider="telegram", external_user_id="12345")
        browser = Client()
        self.assertFalse(browser.login(username="12345", password="irrelevante"))
        self.assertEqual(browser.get(self.history).status_code, 403)
        self.assertEqual(browser.patch(self.path, {"status": "closed"}, content_type="application/json").status_code, 403)
        self.assertEqual(get_user_model().objects.count(), 4)

    def test_history_exposes_no_write_or_delete_routes_even_to_admin(self):
        self.change()
        event = self.incident.changes.get()
        for role in self.users:
            self.client.force_login(self.users[role])
            for method in ("post", "put", "patch", "delete"):
                with self.subTest(role=role, method=method):
                    self.assertEqual(getattr(self.client, method)(self.history, {}, content_type="application/json").status_code, 405)
            self.assertEqual(self.client.get(f"{self.history}{event.pk}/").status_code, 404)
        self.assertFalse(admin.site.is_registered(IncidentChange))
        self.assertEqual(self.incident.changes.count(), 1)

    def test_real_csrf_and_cache_controls_remain_enabled(self):
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.users["Operador"])
        data = {"status": "resolved", "observation": "Revisado"}
        self.assertEqual(browser.patch(self.path, data, content_type="application/json").status_code, 403)
        self.assertFalse(self.incident.changes.exists())
        browser.get("/dashboard/")
        token = browser.cookies["csrftoken"].value
        self.assertEqual(browser.patch(self.path, data, content_type="application/json", HTTP_X_CSRFTOKEN=token,
                                      HTTP_ORIGIN="https://outside.invalid").status_code, 403)
        self.assertEqual(browser.patch(self.path, data, content_type="application/json", HTTP_X_CSRFTOKEN=token,
                                      HTTP_ORIGIN="http://testserver").status_code, 200)
        self.assertIn("no-store", browser.get(self.history).headers["Cache-Control"])
        self.assertEqual(self.incident.changes.count(), 1)

    def test_pagination_is_limited_ordered_and_scoped_to_incident(self):
        for index in range(25):
            self.incident.status = "in_progress" if index % 2 == 0 else "reported"
            self.incident.save(update_fields=["status"])
        other = incident_fixture()
        other.priority = "critical"
        other.save(update_fields=["priority"])
        first = self.client.get(self.history).json()
        second = self.client.get(self.history + "?page=2").json()
        expected = list(self.incident.changes.values_list("pk", flat=True))
        self.assertEqual(first["count"], 25)
        self.assertEqual(len(first["results"]), 20)
        self.assertEqual(len(second["results"]), 5)
        self.assertEqual([row["id"] for row in first["results"] + second["results"]], expected)
        self.assertTrue(all(row["incident_id"] == self.incident.pk for row in first["results"] + second["results"]))
        self.assertEqual(self.client.get(self.history + "?page=3").status_code, 404)
        self.assertEqual(self.client.get(reverse("incidents:incident-history", args=[999999])).status_code, 404)

    def test_deactivated_actor_preserves_history(self):
        self.change()
        user = self.users["Operador"]
        user.is_active = False
        user.save(update_fields=["is_active"])
        event = self.incident.changes.get()
        self.assertEqual(event.actor_id, user.pk)
        self.assertIn(user.username, event.actor_label)

    def test_deleted_or_renamed_actor_preserves_textual_identity(self):
        self.change()
        user = self.users["Operador"]
        label = self.incident.changes.get().actor_label
        user.username = "nombre-renovado"
        user.save(update_fields=["username"])
        self.assertEqual(self.incident.changes.get().actor_label, label)
        user.delete()
        event = self.incident.changes.get()
        self.assertIsNone(event.actor_id)
        self.assertEqual(event.actor_label, label)

    def test_admin_records_priority_and_status_once_each_with_authenticated_actor(self):
        response = self.admin_change(status="under_review", priority="critical", observation="Afecta transmisión.")
        self.assertEqual(response.status_code, 302)
        events = {event.field: event for event in self.incident.changes.all()}
        self.assertEqual(set(events), {"status", "priority"})
        self.assertEqual((events["priority"].old_value, events["priority"].new_value), ("medium", "critical"))
        for event in events.values():
            self.assertEqual(event.actor_id, self.users["Supervisor"].pk)
            self.assertEqual(event.source, "admin")
            self.assertEqual(event.observation, "Afecta transmisión.")
        self.assertEqual(events["priority"].action, "priority_changed")
        self.incident.refresh_from_db()
        self.assertEqual(self.admin_change(observation="Sin cambio real").status_code, 302)
        self.assertEqual(self.incident.changes.count(), 2)

    def test_admin_rejects_oversized_observation_without_changes(self):
        response = self.admin_change(status="resolved", observation="x" * 1001)
        self.assertEqual(response.status_code, 200)
        self.assertIn("observation", response.context["adminform"].form.errors)
        self.incident.refresh_from_db()
        self.assertEqual(self.incident.status, "reported")
        self.assertFalse(self.incident.changes.exists())

    def test_existing_admin_operational_edits_are_recorded_without_copying_raw_message(self):
        self.client.force_login(self.users["Administrador"])
        response = self.client.post(reverse("admin:incidents_incident_change", args=[self.incident.pk]), {
            "technician": self.incident.technician_id, "unit": "Estudio 4", "equipment": self.incident.equipment,
            "failure_type": self.incident.failure_type, "description": "Descripción revisada", "original_message": "Texto original QA",
            "status": "reported", "priority": "medium", "observation": "Ubicación corregida", "_save": "Guardar",
        })
        self.assertEqual(response.status_code, 302)
        events = {row.field: row for row in self.incident.changes.all()}
        self.assertEqual(set(events), {"unit", "description"})
        self.assertEqual((events["unit"].old_value, events["unit"].new_value), ("Estudio 2", "Estudio 4"))
        self.assertEqual(events["description"].source, "admin")

    def test_internal_save_and_update_fields_track_only_persisted_changes(self):
        self.incident.status = "in_progress"
        self.incident.priority = "critical"
        self.incident.save(update_fields=["status"])
        event = self.incident.changes.get()
        self.assertEqual(event.field, "status")
        self.assertEqual(event.source, "internal")
        self.assertIsNone(event.actor)
        self.assertEqual(event.actor_label, "Sistema / proceso interno")
        self.incident.refresh_from_db()
        self.assertEqual(self.incident.priority, "medium")

    def test_reporter_identity_is_unambiguous_even_when_names_match(self):
        original = self.incident.technician_id
        replacement = Technician.objects.create(full_name=self.incident.technician.full_name)
        self.incident.technician = replacement
        self.incident.save(update_fields=["technician"])
        event = self.incident.changes.get()
        self.assertIn(str(original), event.old_value)
        self.assertIn(str(replacement.pk), event.new_value)
        self.assertNotEqual(event.old_value, event.new_value)

    def test_admin_priority_save_preserves_status_saved_since_form_was_loaded(self):
        stale = Incident.objects.get(pk=self.incident.pk)
        self.change("in_progress")
        stale.priority = "critical"
        request = SimpleNamespace(user=self.users["Supervisor"])
        form = SimpleNamespace(changed_data=["priority"], cleaned_data={"observation": "Prioridad revisada"})
        admin.site._registry[Incident].save_model(request, stale, form, change=True)
        self.incident.refresh_from_db()
        self.assertEqual((self.incident.status, self.incident.priority), ("in_progress", "critical"))
        self.assertEqual(self.incident.changes.count(), 2)

    def test_failed_incident_save_does_not_leave_an_event(self):
        self.incident.status = "in_progress"
        with patch("django.db.models.Model.save", side_effect=RuntimeError("No se pudo guardar QA")):
            with self.assertRaises(RuntimeError):
                self.incident.save(update_fields=["status"])
        self.incident.refresh_from_db()
        self.assertEqual(self.incident.status, "reported")
        self.assertFalse(self.incident.changes.exists())

    def test_creation_has_no_fictitious_events_and_numbering_is_unchanged(self):
        self.assertFalse(IncidentChange.objects.exists())
        self.assertEqual(self.incident.code, f"SC-{self.incident.pk:06d}")
        self.assertEqual(self.client.get(self.history).json()["results"], [])

    def test_history_insertion_failure_rolls_back_incident_update(self):
        with patch("django.db.models.query.QuerySet.bulk_create", side_effect=RuntimeError("Fallo QA")):
            with self.assertRaises(RuntimeError):
                self.change("resolved")
        self.incident.refresh_from_db()
        self.assertEqual(self.incident.status, "reported")
        self.assertIsNone(self.incident.resolved_at)
        self.assertFalse(self.incident.changes.exists())

    def test_outer_transaction_rollback_removes_both_events_and_changes(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.incident.status = "in_progress"
                self.incident.priority = "critical"
                self.incident.save()
                raise RuntimeError("Rollback QA")
        self.incident.refresh_from_db()
        self.assertEqual((self.incident.status, self.incident.priority), ("reported", "medium"))
        self.assertFalse(self.incident.changes.exists())

    def test_incident_with_history_cannot_be_deleted_accidentally(self):
        self.change()
        with self.assertRaises(ProtectedError):
            self.incident.delete()
        self.assertEqual(self.incident.changes.count(), 1)

    def test_html_observation_is_stored_as_text_and_renderer_uses_safe_dom(self):
        text = '<script>alert("QA")</script><img src=x onerror=alert(1)>'
        self.assertEqual(self.change(observation=text).status_code, 200)
        self.assertEqual(self.incident.changes.get().observation, text)
        self.assertEqual(self.client.get(self.history).json()["results"][0]["observation"], text)
        for role, user in self.users.items():
            self.client.force_login(user)
            page = self.client.get(reverse("dashboard-incident-detail", args=[self.incident.pk]))
            self.assertContains(page, "Historial de cambios")
            self.assertContains(page, "No se han registrado cambios de gestión para esta incidencia")
            self.assertContains(page, "node.textContent = text")
            self.assertContains(page, 'timeZone: "America/Santiago"')
            if role == "Visualizador":
                self.assertNotContains(page, 'id="incident-observation"')


@override_settings(**SETTINGS)
class IncidentHistoryConcurrencyTests(TransactionTestCase):
    def setUp(self):
        initialize()
        self.user = account("Operador", "historial-concurrente")
        self.incident = incident_fixture()

    def run_concurrent(self, statuses):
        barrier = Barrier(2)

        def change(status):
            close_old_connections()
            try:
                browser = Client()
                browser.force_login(get_user_model().objects.get(pk=self.user.pk))
                barrier.wait(timeout=5)
                return browser.patch(reverse("incidents:incident-detail", args=[self.incident.pk]),
                    {"status": status, "observation": status}, content_type="application/json").status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(change, status) for status in statuses]
            self.assertEqual([future.result(timeout=12) for future in futures], [200, 200])

    def test_concurrent_identical_requests_create_one_event(self):
        self.run_concurrent(["in_progress", "in_progress"])
        self.assertEqual(self.incident.changes.count(), 1)
        event = self.incident.changes.get()
        self.assertEqual((event.old_value, event.new_value), ("reported", "in_progress"))

    def test_concurrent_different_requests_preserve_real_chain_of_values(self):
        self.run_concurrent(["in_progress", "resolved"])
        events = list(self.incident.changes.order_by("pk"))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].old_value, "reported")
        self.assertEqual(events[1].old_value, events[0].new_value)
        self.incident.refresh_from_db()
        self.assertEqual(self.incident.status, events[1].new_value)
        for event in events:
            self.assertEqual(event.actor_id, self.user.pk)
            self.assertEqual(event.observation, event.new_value)
