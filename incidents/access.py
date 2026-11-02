"""Perfiles de gestión. Los reportantes de Telegram no participan aquí."""
from functools import wraps
from collections.abc import Mapping

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission


ROLE_PERMISSIONS = {
    "Administrador": {"access_dashboard", "view_incident", "view_incident_analytics", "change_incident_status", "change_incident_priority", "manage_dashboard_users", "change_incident"},
    "Supervisor": {"access_dashboard", "view_incident", "view_incident_analytics", "change_incident_status", "change_incident_priority", "change_incident"},
    "Operador": {"access_dashboard", "view_incident", "change_incident_status"},
    "Visualizador": {"access_dashboard", "view_incident", "view_incident_analytics"},
}
ROLE_GROUPS = {role: f"Secuoya - {role}" for role in ROLE_PERMISSIONS}


def assigned_role(user):
    if user.is_superuser:
        return "Administrador"
    groups = {group.name for group in user.groups.all()}
    roles = [role for role, group in ROLE_GROUPS.items() if group in groups]
    if len(roles) == 1:
        return roles[0]
    # Transición explícita: no editar cuentas históricas automáticamente.
    # La primera asignación de un perfil desde Usuarios termina esta excepción.
    if not roles and user.is_staff:
        return "Administrador" if user.has_perm("auth.change_user") else "Supervisor"
    return None


def management_role(user):
    return assigned_role(user) if user.is_authenticated and user.is_active else None


def can(user, permission):
    role = management_role(user)
    if not role or permission not in ROLE_PERMISSIONS[role]:
        return False
    if user.is_superuser or (user.is_staff and not user.groups.filter(name__in=ROLE_GROUPS.values()).exists()):
        return True
    return user.has_perm(f"incidents.{permission}")


def dashboard_context(request):
    permissions = {name: can(request.user, name) for name in ROLE_PERMISSIONS["Administrador"]}
    nav = [("control", "◉", "Centro de control"), ("incidents", "△", "Incidentes")]
    if permissions["view_incident_analytics"]:
        nav.append(("analytics", "▥", "Analítica"))
    if permissions["change_incident_status"]:
        nav.append(("management", "✓", "Gestión"))
    return {"management_permissions": permissions, "management_role": management_role(request.user), "dashboard_navigation": nav}


def require_management(permission="access_dashboard"):
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path(), login_url="admin:login")
            if not can(request.user, permission):
                if assigned_role(request.user) is None:
                    # Compatibilidad de enlaces privados para cuentas sin
                    # acceso al panel: conservar su redirección histórica.
                    return redirect_to_login(request.get_full_path(), login_url="admin:login")
                raise PermissionDenied("Tu perfil no permite realizar esta operación.")
            return view(request, *args, **kwargs)
        return wrapped
    return decorate


class CanViewIncidents(BasePermission):
    def has_permission(self, request, view):
        return can(request.user, "view_incident")


class CanViewAnalytics(BasePermission):
    def has_permission(self, request, view):
        return can(request.user, "view_incident_analytics")


class CanManageUsers(BasePermission):
    def has_permission(self, request, view):
        return can(request.user, "manage_dashboard_users")


class CanUseMessageTest(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        # Preservar la autorización staff anterior de Codex 1 sin extenderla
        # a los nuevos supervisores por el mero flag técnico is_staff.
        return can(user, "manage_dashboard_users") or (user.is_authenticated and user.is_active
            and user.is_staff and not user.groups.filter(name__in=ROLE_GROUPS.values()).exists())


class CanUseIncidentDetail(CanViewIncidents):
    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        if request.method == "PATCH":
            # No se añade una operación de prioridad a esta API: la operación
            # existente permanece en IncidentAdmin, con permisos por campo.
            if isinstance(request.data, Mapping) and "priority" in request.data and not can(request.user, "change_incident_priority"):
                return False
            return can(request.user, "change_incident_status")
        return True
