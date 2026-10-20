"""Usuarios de gestión y separación de reportantes. Solo base de tests QA."""
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from incidents.access import ROLE_GROUPS, ROLE_PERMISSIONS, assigned_role, can, management_role
from incidents.models import Incident, Technician, TechnicianChannel
from incidents.services.management_user_service import save_management_user


PASSWORD = "Piloto-Secuoya-QA-6248!"
SETTINGS = dict(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
                EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")


def initialize():
    import_module("incidents.migrations.0012_management_roles").initialize_roles(apps, SimpleNamespace(connection=connection))


def account(role, username=None, **kwargs):
    user = get_user_model().objects.create_user(username=username or role, password=PASSWORD,
        email=f"{username or role}@example.invalid", first_name="Persona", last_name="QA",
        is_staff=role in {"Administrador", "Supervisor"}, **kwargs)
    user.groups.add(Group.objects.get(name=ROLE_GROUPS[role]))
    return user


def user_data(username="nuevo", role="Visualizador", active=True):
    data = dict(username=username, first_name="Ana", last_name="Pérez", email=f"{username}@example.invalid", role=role)
    if active:
        data["is_active"] = "on"
    return data


@override_settings(**SETTINGS)
class ManagementAccessTests(TestCase):
    def setUp(self):
        initialize()
        self.users = {role: account(role) for role in ROLE_PERMISSIONS}
        reporter = Technician.objects.create(full_name="Reportante separado")
        self.incident = Incident.objects.create(technician=reporter, area="Audio", location_context="Estudio 2", unit="Estudio 2",
            equipment="Micrófono 1", asset_category="audio", failure_type="Sin audio", description="No entrega audio",
            operational_impact="Interrumpe la emisión", priority="medium", original_message="QA")

    def test_login_and_deep_link_for_every_role(self):
        for role, user in self.users.items():
            with self.subTest(role=role):
                client = Client()
                target = reverse("dashboard-incident-detail", args=[self.incident.pk])
                response = client.post("/admin/login/?next=" + target, {"username": user.username, "password": PASSWORD, "next": target})
                self.assertRedirects(response, target)
                self.assertContains(client.get(target), f"const linkedIncidentId = {self.incident.pk};")

    def test_read_and_analytics_matrix_on_server(self):
        for role, user in self.users.items():
            with self.subTest(role=role):
                self.client.force_login(user)
                for path in ("/dashboard/", "/api/v1/incidents/", "/api/v1/incidents/summary/", f"/api/v1/incidents/{self.incident.pk}/"):
                    self.assertEqual(self.client.get(path).status_code, 200, path)
                expected = 403 if role == "Operador" else 200
                self.assertEqual(self.client.get("/api/v1/incidents/analytics/").status_code, expected)
                self.assertEqual(self.client.get("/dashboard/?view=analytics").status_code, expected)

    def test_state_changes_matrix_and_priority_injection(self):
        path = f"/api/v1/incidents/{self.incident.pk}/"
        for role, user in self.users.items():
            with self.subTest(role=role):
                self.client.force_login(user)
                response = self.client.patch(path, {"status": "in_progress"}, content_type="application/json")
                self.assertEqual(response.status_code, 403 if role == "Visualizador" else 200)
                response = self.client.patch(path, {"status": "closed", "priority": "critical"}, content_type="application/json")
                self.assertEqual(response.status_code, 400 if role in {"Administrador", "Supervisor"} else 403)
                self.incident.refresh_from_db()
                self.assertEqual(self.incident.priority, "medium")

    def test_native_priority_operation_remains_authorized_for_supervisor(self):
        self.client.force_login(self.users["Supervisor"])
        path = reverse("admin:incidents_incident_change", args=[self.incident.pk])
        self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.post(path, {"status": "under_review", "priority": "critical", "_save": "Guardar"}).status_code, 302)
        self.incident.refresh_from_db()
        self.assertEqual(self.incident.priority, "critical")
        response = self.client.post(path, {"status": "closed", "priority": "low", "equipment": "Equipo falsificado", "_save": "Guardar"})
        self.assertEqual(response.status_code, 403)
        self.incident.refresh_from_db()
        self.assertEqual(self.incident.equipment, "Micrófono 1")

    def test_lower_roles_cannot_write_native_admin(self):
        for role in ("Operador", "Visualizador"):
            self.client.force_login(self.users[role])
            response = self.client.post(reverse("admin:incidents_incident_change", args=[self.incident.pk]), {"priority": "critical"})
            self.assertIn(response.status_code, (302, 403))
        self.incident.refresh_from_db()
        self.assertEqual(self.incident.priority, "medium")

    def test_staff_flag_does_not_override_official_operator_permissions(self):
        user = self.users["Operador"]
        user.is_staff = True
        user.save(update_fields=["is_staff"])
        self.client.force_login(user)
        self.assertEqual(self.client.post(reverse("admin:incidents_incident_change", args=[self.incident.pk]),
            {"priority": "critical", "status": "closed", "_save": "Guardar"}).status_code, 403)
        self.assertEqual(self.client.get(reverse("dashboard-users")).status_code, 403)
        self.incident.refresh_from_db()
        self.assertEqual(self.incident.priority, "medium")

    def test_incident_patch_requires_csrf_with_a_real_session(self):
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.users["Operador"])
        path = f"/api/v1/incidents/{self.incident.pk}/"
        self.assertEqual(browser.patch(path, {"status": "in_progress"}, content_type="application/json").status_code, 403)
        browser.get(reverse("dashboard"))
        token = browser.cookies["csrftoken"].value
        self.assertEqual(browser.patch(path, {"status": "in_progress"}, content_type="application/json", HTTP_X_CSRFTOKEN=token).status_code, 200)

    def test_users_section_and_all_its_post_actions_are_admin_only(self):
        target = self.users["Visualizador"]
        for role in ("Supervisor", "Operador", "Visualizador"):
            self.client.force_login(self.users[role])
            for path in (reverse("dashboard-users"), reverse("dashboard-user-create"), reverse("dashboard-user-edit", args=[target.pk]), reverse("dashboard-user-handoff")):
                self.assertEqual(self.client.get(path).status_code, 403)
                self.assertEqual(self.client.post(path, user_data("intruso", "Administrador")).status_code, 403)
            self.assertEqual(self.client.post(reverse("dashboard-user-password", args=[target.pk])).status_code, 403)
        self.assertFalse(get_user_model().objects.filter(username="intruso").exists())

    def test_navigation_and_controls_match_roles(self):
        for role, user in self.users.items():
            self.client.force_login(user)
            response = self.client.get(reverse("dashboard"))
            if role == "Administrador":
                self.assertContains(response, reverse("dashboard-users"))
            else:
                self.assertNotContains(response, reverse("dashboard-users"))
            if role == "Visualizador":
                self.assertNotContains(response, 'id="incident-status-select"')
            if role == "Operador":
                self.assertNotContains(response, 'data-view="analytics"')
                self.assertContains(response, "const canViewAnalytics = false;")

    def test_anonymous_ungrouped_and_inactive_accounts_have_no_access(self):
        for path in (reverse("dashboard"), reverse("dashboard-users")):
            self.assertEqual(self.client.get(path).status_code, 302)
        self.assertIn(self.client.get("/api/v1/incidents/").status_code, (401, 403))
        plain = get_user_model().objects.create_user(username="sin-perfil", password=PASSWORD)
        self.client.force_login(plain)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 302)
        inactive = self.users["Supervisor"]
        inactive.is_active = False
        inactive.save()
        self.assertFalse(can(inactive, "view_incident"))
        self.assertFalse(Client().login(username=inactive.username, password=PASSWORD))

    def test_codex1_message_test_entry_is_still_admin_only(self):
        self.client.force_login(self.users["Supervisor"])
        self.assertEqual(self.client.post("/api/v1/messages/test/", {}, content_type="application/json").status_code, 403)

    def test_direct_permissions_or_mixed_groups_do_not_escalate_role(self):
        user = self.users["Operador"]
        user.user_permissions.add(Permission.objects.get(content_type__app_label="incidents", codename="manage_dashboard_users"))
        self.assertFalse(can(user, "manage_dashboard_users"))
        user.groups.add(Group.objects.get(name=ROLE_GROUPS["Administrador"]))
        self.assertIsNone(management_role(user))
        self.assertFalse(can(user, "view_incident"))


@override_settings(**SETTINGS)
class ManagementUserTests(TestCase):
    def setUp(self):
        initialize()
        self.admin = account("Administrador", "admin-qa")
        self.client.force_login(self.admin)

    def test_create_from_dashboard_uses_unusable_password_and_no_email(self):
        self.assertEqual(self.client.get(reverse("dashboard-user-create")).context["form"]["role"].value(), "Visualizador")
        response = self.client.post(reverse("dashboard-user-create"), user_data())
        self.assertRedirects(response, reverse("dashboard-user-handoff"), fetch_redirect_response=False)
        user = get_user_model().objects.get(username="nuevo")
        self.assertFalse(user.has_usable_password())
        self.assertEqual(assigned_role(user), "Visualizador")
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.is_staff)
        self.assertEqual(len(mail.outbox), 0)

    def test_edit_profile_and_account_fields(self):
        target = account("Visualizador", "editor")
        response = self.client.post(reverse("dashboard-user-edit", args=[target.pk]), user_data("editor", "Supervisor"))
        self.assertRedirects(response, reverse("dashboard-users"))
        target.refresh_from_db()
        self.assertEqual(assigned_role(target), "Supervisor")
        self.assertEqual(target.first_name, "Ana")
        self.assertEqual(target.groups.filter(name__in=ROLE_GROUPS.values()).count(), 1)

    def test_deactivation_invalidates_existing_browser_session(self):
        target = account("Visualizador", "desactivar")
        browser = Client()
        browser.force_login(target)
        self.assertEqual(browser.get(reverse("dashboard")).status_code, 200)
        self.client.post(reverse("dashboard-user-edit", args=[target.pk]), user_data("desactivar", active=False))
        self.assertEqual(browser.get(reverse("dashboard")).status_code, 302)
        self.assertIn(browser.get("/api/v1/incidents/").status_code, (401, 403))
        target.refresh_from_db()
        self.assertEqual(assigned_role(target), "Visualizador")

    def test_reactivation_preserves_assigned_profile(self):
        target = account("Supervisor", "reactivar", is_active=False)
        response = self.client.get(reverse("dashboard-user-edit", args=[target.pk]))
        self.assertEqual(response.context["form"]["role"].value(), "Supervisor")
        self.client.post(reverse("dashboard-user-edit", args=[target.pk]), user_data("reactivar", "Supervisor"))
        target.refresh_from_db()
        self.assertTrue(target.is_active)
        self.assertEqual(assigned_role(target), "Supervisor")

    def test_invalid_forms_and_case_insensitive_duplicates_do_not_create_accounts(self):
        account("Visualizador", "existente")
        invalid = [user_data("existente"), {**user_data("otro"), "email": "EXISTENTE@example.invalid"},
            {**user_data("otro"), "username": "EXISTENTE"}, {**user_data("otro"), "email": "incorrecto"},
            {**user_data("otro"), "first_name": ""}, {**user_data("otro"), "role": "Superadministrador"}]
        before = get_user_model().objects.count()
        for payload in invalid:
            response = self.client.post(reverse("dashboard-user-create"), payload)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context["form"].errors)
        self.assertEqual(get_user_model().objects.count(), before)

    def test_privilege_flags_injected_in_form_are_rejected(self):
        for key in ("is_superuser", "is_staff", "groups", "user_permissions", "password"):
            response = self.client.post(reverse("dashboard-user-create"), {**user_data("inyeccion"), key: "1"})
            self.assertEqual(response.status_code, 403)
        self.assertFalse(get_user_model().objects.filter(username="inyeccion").exists())

    def test_ordinary_admin_cannot_edit_or_reset_superuser(self):
        tech = get_user_model().objects.create_superuser(username="tecnico", email="tecnico@example.invalid", password=PASSWORD)
        for method, path in (("get", reverse("dashboard-user-edit", args=[tech.pk])),
                             ("post", reverse("dashboard-user-edit", args=[tech.pk])),
                             ("post", reverse("dashboard-user-password", args=[tech.pk]))):
            response = getattr(self.client, method)(path, user_data("tecnico", "Operador"))
            self.assertEqual(response.status_code, 403)
        tech.refresh_from_db()
        self.assertTrue(tech.check_password(PASSWORD))

    def test_last_administrator_cannot_be_disabled_or_demoted(self):
        for data in (user_data("admin-qa", "Visualizador"), user_data("admin-qa", "Administrador", active=False)):
            response = self.client.post(reverse("dashboard-user-edit", args=[self.admin.pk]), data)
            self.assertContains(response, "último administrador activo")
            self.admin.refresh_from_db()
            self.assertTrue(self.admin.is_active)
            self.assertEqual(management_role(self.admin), "Administrador")

    def test_another_active_administrator_allows_explicit_demotion(self):
        account("Administrador", "respaldo")
        response = self.client.post(reverse("dashboard-user-edit", args=[self.admin.pk]), user_data("admin-qa", "Visualizador"))
        self.assertEqual(response.status_code, 302)
        self.admin.refresh_from_db()
        self.assertEqual(management_role(self.admin), "Visualizador")
        self.assertFalse(self.admin.is_staff)
        self.assertEqual(self.client.get(reverse("dashboard-users")).status_code, 403)

    def test_native_user_admin_cannot_bypass_last_admin_or_superuser_guards(self):
        tech = get_user_model().objects.create_superuser(username="tecnico", email="tecnico@example.invalid", password=PASSWORD)
        self.client.force_login(tech)
        target = reverse("admin:auth_user_change", args=[self.admin.pk])
        self.assertEqual(self.client.post(target, {"is_superuser": "on", "is_active": ""}).status_code, 403)
        self.assertEqual(self.client.post(reverse("admin:auth_user_delete", args=[self.admin.pk]), {"post": "yes"}).status_code, 403)
        self.assertEqual(self.client.post(reverse("admin:auth_user_password_change", args=[self.admin.pk]), {"password1": PASSWORD, "password2": PASSWORD}).status_code, 403)
        self.admin.refresh_from_db()
        self.assertFalse(self.admin.is_superuser)

    def test_official_group_permissions_cannot_be_modified_through_native_admin(self):
        tech = get_user_model().objects.create_superuser(username="tecnico", email="tecnico@example.invalid", password=PASSWORD)
        self.client.force_login(tech)
        group = Group.objects.get(name=ROLE_GROUPS["Administrador"])
        self.assertEqual(self.client.post(reverse("admin:auth_group_change", args=[group.pk]), {"name": "Otro"}).status_code, 403)
        self.assertEqual(self.client.post(reverse("admin:auth_group_delete", args=[group.pk]), {"post": "yes"}).status_code, 403)

    def test_search_and_filters_include_inactive_profiles(self):
        target = account("Supervisor", "buscar", is_active=False)
        response = self.client.get(reverse("dashboard-users"), {"q": "buscar@example.invalid", "role": "Supervisor", "state": "inactive"})
        self.assertContains(response, target.email)
        self.assertEqual(len(response.context["rows"]), 1)
        self.assertEqual(response.context["rows"][0]["profile"], "Supervisor")
        response = self.client.get(reverse("dashboard-users"), {"q": "Persona QA", "role": "Supervisor", "state": "inactive"})
        self.assertContains(response, target.email)

    def test_legacy_staff_transition_preserves_access_until_explicit_role_assignment(self):
        legacy = get_user_model().objects.create_user(username="anterior", password=PASSWORD, is_staff=True)
        self.assertFalse(can(legacy, "manage_dashboard_users"))
        self.assertTrue(can(legacy, "change_incident_status"))
        self.assertEqual(assigned_role(legacy), "Supervisor")
        initialize()
        legacy.refresh_from_db()
        self.assertEqual(legacy.groups.count(), 0)
        self.assertTrue(legacy.is_staff)
        self.client.post(reverse("dashboard-user-edit", args=[legacy.pk]), user_data("anterior", "Operador"))
        legacy.refresh_from_db()
        self.assertFalse(legacy.is_staff)
        self.assertEqual(management_role(legacy), "Operador")

    def test_legacy_user_administrator_keeps_access_without_data_migration(self):
        legacy = get_user_model().objects.create_user(username="admin-anterior", password=PASSWORD, is_staff=True)
        legacy.user_permissions.add(Permission.objects.get(content_type__app_label="auth", codename="change_user"))
        self.assertTrue(can(legacy, "manage_dashboard_users"))
        self.assertEqual(assigned_role(legacy), "Administrador")
        self.assertEqual(legacy.groups.count(), 0)

    def test_roles_initialization_is_repeatable_and_does_not_touch_accounts(self):
        before = list(get_user_model().objects.values())
        initialize()
        initialize()
        self.assertEqual(Group.objects.filter(name__in=ROLE_GROUPS.values()).count(), 4)
        self.assertEqual(list(get_user_model().objects.values()), before)
        for role, codes in ROLE_PERMISSIONS.items():
            self.assertEqual(set(Group.objects.get(name=ROLE_GROUPS[role]).permissions.values_list("codename", flat=True)), codes)

    def test_telegram_reporter_does_not_become_dashboard_user(self):
        reporter = Technician.objects.create(full_name="Solo reportante")
        TechnicianChannel.objects.create(technician=reporter, provider="telegram", external_user_id="202", external_chat_id="202")
        before = get_user_model().objects.count()
        initialize()
        self.assertEqual(get_user_model().objects.count(), before)
        self.assertNotContains(self.client.get(reverse("dashboard-users")), "Solo reportante")

    def test_migration_refuses_conflicting_existing_group_with_real_members(self):
        group = Group.objects.get(name=ROLE_GROUPS["Administrador"])
        group.permissions.clear()
        before = list(get_user_model().objects.values())
        with self.assertRaises(RuntimeError):
            initialize()
        self.assertEqual(list(get_user_model().objects.values()), before)

    def test_csrf_required_for_user_writes(self):
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.admin)
        self.assertEqual(browser.post(reverse("dashboard-user-create"), user_data()).status_code, 403)
        target = account("Visualizador", "csrf-target")
        self.assertEqual(browser.post(reverse("dashboard-user-password", args=[target.pk])).status_code, 403)
        browser.get(reverse("dashboard-user-create"))
        token = browser.cookies["csrftoken"].value
        self.assertEqual(browser.post(reverse("dashboard-user-create"), {**user_data(), "csrfmiddlewaretoken": token}).status_code, 302)


@override_settings(**SETTINGS)
class LocalPasswordTests(TestCase):
    def setUp(self):
        initialize()
        self.admin = account("Administrador", "claves-admin")
        self.target = account("Operador", "claves-operador")
        self.client.force_login(self.admin)

    def link(self):
        self.client.post(reverse("dashboard-user-password", args=[self.target.pk]))
        handoff = self.client.session["password_handoff"]
        return urlsplit(handoff["url"]).path

    def test_handoff_is_single_view_private_and_never_cached(self):
        link = self.link()
        response = self.client.get(reverse("dashboard-user-handoff"))
        self.assertContains(response, link)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(response.headers["Referrer-Policy"], "same-origin")
        self.assertNotIn("password_handoff", self.client.session)
        self.assertEqual(self.client.get(reverse("dashboard-user-handoff")).status_code, 302)
        self.assertEqual(len(mail.outbox), 0)

    def assert_form_policy(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Referrer-Policy"], "same-origin")
        self.assertIn("no-store", response.headers["Cache-Control"])
        # No meta que sustituya la cabecera ni recursos/enlaces/formularios
        # externos en las páginas que contienen credenciales.
        attributes = []

        class PageParser(HTMLParser):
            def handle_starttag(self, tag, attrs):
                attributes.append((tag, dict(attrs)))

        PageParser().feed(response.content.decode())
        for tag, attrs in attributes:
            self.assertFalse(tag == "meta" and attrs.get("name", "").lower() == "referrer")
            for name in ("src", "href", "action"):
                url = urlsplit(attrs.get(name) or "")
                self.assertIn(url.scheme, ("", "http"))
                self.assertIn(url.netloc, ("", "testserver"))

    def test_login_with_real_csrf_for_both_login_urls(self):
        for path in (reverse("dashboard-login"), "/admin/login/"):
            with self.subTest(path=path):
                browser = Client(enforce_csrf_checks=True)
                self.assert_form_policy(browser.get(path))
                response = browser.post(path, {
                    "username": self.target.username, "password": PASSWORD,
                    "csrfmiddlewaretoken": browser.cookies["csrftoken"].value,
                }, HTTP_ORIGIN="http://testserver")
                self.assertRedirects(response, reverse("dashboard"))
                self.assertEqual(int(browser.session["_auth_user_id"]), self.target.pk)

    def test_login_rejects_missing_invalid_csrf_and_untrusted_origins(self):
        browser = Client(enforce_csrf_checks=True)
        path = reverse("dashboard-login")
        browser.get(path)
        token = browser.cookies["csrftoken"].value
        cases = [(None, "http://testserver"), ("invalid", "http://testserver"),
                 ("a" * 32, "http://testserver"), (token, "https://outside.invalid"), (token, "null")]
        for csrf, origin in cases:
            with self.subTest(csrf=csrf is not None, origin=origin):
                data = {"username": self.target.username, "password": PASSWORD}
                if csrf is not None:
                    data["csrfmiddlewaretoken"] = csrf
                self.assertEqual(browser.post(path, data, HTTP_ORIGIN=origin).status_code, 403)
                self.assertNotIn("_auth_user_id", browser.session)

    def test_set_password_with_real_csrf_hides_token_and_is_single_use(self):
        link = self.link()
        browser = Client(enforce_csrf_checks=True)
        redirect = browser.get(link)
        self.assertEqual(redirect.status_code, 302)
        self.assertEqual(redirect.headers["Referrer-Policy"], "no-referrer")
        self.assertIn("no-store", redirect.headers["Cache-Control"])
        destination = redirect["Location"]
        self.assertTrue(destination.endswith("/set-password/"))
        raw_token = link.rstrip("/").rsplit("/", 1)[1]
        self.assertNotIn(raw_token, destination)
        page = browser.get(destination)
        self.assert_form_policy(page)
        self.assertNotContains(page, raw_token)
        self.assertFalse(Client(enforce_csrf_checks=True).get(destination).context["validlink"])
        new_password = PASSWORD + "Nueva"
        result = browser.post(destination, {
            "new_password1": new_password, "new_password2": new_password,
            "csrfmiddlewaretoken": browser.cookies["csrftoken"].value,
        }, HTTP_ORIGIN="http://testserver")
        self.assertRedirects(result, reverse("dashboard-password-complete"))
        self.target.refresh_from_db()
        self.assertTrue(self.target.check_password(new_password))
        self.assertNotIn("_password_reset_token", browser.session)
        self.assertFalse(browser.get(destination).context["validlink"])
        self.assertFalse(Client().get(link).context["validlink"])

    def test_set_password_rejects_missing_invalid_csrf_and_untrusted_origins(self):
        browser = Client(enforce_csrf_checks=True)
        destination = browser.get(self.link())["Location"]
        browser.get(destination)
        token = browser.cookies["csrftoken"].value
        self.target.refresh_from_db()
        original_password = self.target.password
        cases = [(None, "http://testserver"), ("invalid", "http://testserver"),
                 ("a" * 32, "http://testserver"), (token, "https://outside.invalid"), (token, "null")]
        for csrf, origin in cases:
            with self.subTest(csrf=csrf is not None, origin=origin):
                data = {"new_password1": PASSWORD + "Nueva", "new_password2": PASSWORD + "Nueva"}
                if csrf is not None:
                    data["csrfmiddlewaretoken"] = csrf
                self.assertEqual(browser.post(destination, data, HTTP_ORIGIN=origin).status_code, 403)
                self.target.refresh_from_db()
                self.assertEqual(self.target.password, original_password)

    def test_create_handoff_and_logout_with_real_csrf(self):
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.admin)
        path = reverse("dashboard-user-create")
        self.assert_form_policy(browser.get(path))
        token = browser.cookies["csrftoken"].value
        response = browser.post(path, {**user_data("csrf-handoff"), "csrfmiddlewaretoken": token},
                                HTTP_ORIGIN="http://testserver")
        self.assertRedirects(response, reverse("dashboard-user-handoff"), fetch_redirect_response=False)
        self.assert_form_policy(browser.get(response["Location"]))
        self.assertNotIn("password_handoff", browser.session)
        logout = reverse("dashboard-logout")
        self.assertEqual(browser.post(logout, HTTP_ORIGIN="http://testserver").status_code, 403)
        self.assertEqual(browser.post(logout, {"csrfmiddlewaretoken": token}, HTTP_ORIGIN="null").status_code, 403)
        result = browser.post(logout, {"csrfmiddlewaretoken": token}, HTTP_ORIGIN="http://testserver")
        self.assertRedirects(result, reverse("admin:login"))
        self.assertNotIn("_auth_user_id", browser.session)

    def test_invalid_raw_token_page_keeps_no_referrer_and_no_form(self):
        link = self.link()
        with override_settings(PASSWORD_RESET_TIMEOUT=-1):
            response = Client(enforce_csrf_checks=True).get(link)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["validlink"])
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertNotContains(response, 'name="new_password1"')
        self.assertNotContains(response, link.rstrip("/").rsplit("/", 1)[1])

    def test_password_change_with_real_csrf_and_same_origin_policy(self):
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.admin)
        path = reverse("dashboard-password-change")
        self.assert_form_policy(browser.get(path))
        data = {"old_password": PASSWORD, "new_password1": PASSWORD + "Nueva", "new_password2": PASSWORD + "Nueva"}
        self.assertEqual(browser.post(path, data, HTTP_ORIGIN="http://testserver").status_code, 403)
        result = browser.post(path, {**data, "csrfmiddlewaretoken": browser.cookies["csrftoken"].value},
                              HTTP_ORIGIN="http://testserver")
        self.assertRedirects(result, reverse("dashboard"))
        self.assertEqual(browser.get(reverse("dashboard-users")).status_code, 200)

    def test_password_link_is_redacted_from_development_access_log(self):
        import logging
        from incidents.sensitive_logging import PasswordLinkFilter
        record = logging.LogRecord("django.server", logging.INFO, "", 0, '%s',
            ('GET /dashboard/password/Mg/token-secreto/ HTTP/1.1',), None)
        PasswordLinkFilter().filter(record)
        self.assertNotIn("token-secreto", record.getMessage())
        self.assertIn("[enlace-privado]", record.getMessage())

    def test_native_set_password_flow_is_single_use_and_validates_strength(self):
        link = self.link()
        browser = Client()
        response = browser.get(link)
        self.assertEqual(response.status_code, 302)
        destination = response["Location"]
        weak = browser.post(destination, {"new_password1": "123", "new_password2": "123"})
        self.assertTrue(weak.context["form"].errors)
        result = browser.post(destination, {"new_password1": PASSWORD, "new_password2": PASSWORD})
        self.assertRedirects(result, reverse("dashboard-password-complete"))
        self.target.refresh_from_db()
        self.assertTrue(self.target.check_password(PASSWORD))
        self.assertTrue(browser.login(username=self.target.username, password=PASSWORD))
        self.assertFalse(Client().get(link).context["validlink"])

    def test_regenerating_link_revokes_old_link_and_existing_sessions(self):
        browser = Client()
        browser.force_login(self.target)
        first = self.link()
        second = self.link()
        self.assertNotEqual(first, second)
        self.assertFalse(Client().get(first).context["validlink"])
        self.assertEqual(browser.get(reverse("dashboard")).status_code, 302)

    def test_inactive_user_and_expired_token_cannot_set_password(self):
        link = self.link()
        with override_settings(PASSWORD_RESET_TIMEOUT=-1):
            self.assertFalse(Client().get(link).context["validlink"])
        self.target.is_active = False
        self.target.save()
        self.assertFalse(Client().get(link).context["validlink"])
        response = self.client.post(reverse("dashboard-user-password", args=[self.target.pk]))
        self.assertEqual(response.status_code, 302)

    def test_no_public_reset_request_or_registration_and_no_unsafe_login_redirect(self):
        self.assertEqual(Client().post(reverse("dashboard-user-create"), user_data()).status_code, 302)
        self.assertEqual(Client().get(reverse("dashboard-user-handoff")).status_code, 302)
        result = Client().post(reverse("dashboard-login"), {"username": self.target.username, "password": PASSWORD, "next": "https://example.invalid/phishing"})
        self.assertRedirects(result, reverse("dashboard"))

    def test_self_reset_does_not_lock_out_the_only_administrator(self):
        response = self.client.post(reverse("dashboard-user-password", args=[self.admin.pk]))
        self.assertEqual(response.status_code, 302)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.check_password(PASSWORD))
        self.assertEqual(self.client.get(reverse("dashboard-users")).status_code, 200)

    def test_self_password_change_keeps_current_session_and_revokes_other_sessions(self):
        other = Client()
        other.force_login(self.admin)
        new_password = PASSWORD + "Renovada"
        response = self.client.post(reverse("dashboard-password-change"), {"old_password": PASSWORD, "new_password1": new_password, "new_password2": new_password})
        self.assertRedirects(response, reverse("dashboard"))
        self.assertEqual(self.client.get(reverse("dashboard-users")).status_code, 200)
        self.assertEqual(other.get(reverse("dashboard")).status_code, 302)


@override_settings(**SETTINGS)
class AdministratorConcurrencyTests(TransactionTestCase):
    def setUp(self):
        initialize()

    def test_two_concurrent_deactivations_cannot_remove_all_administrators(self):
        first, second = account("Administrador", "concurrente-a"), account("Administrador", "concurrente-b")

        def deactivate(actor, target):
            close_old_connections()
            try:
                try:
                    save_management_user(actor, user_data(target.username, "Administrador", active=False), target.pk)
                    return True
                except (PermissionDenied, ValidationError):
                    return False
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda pair: deactivate(*pair), [(first, second), (second, first)]))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(get_user_model().objects.filter(is_active=True, groups__name=ROLE_GROUPS["Administrador"]).count(), 1)
