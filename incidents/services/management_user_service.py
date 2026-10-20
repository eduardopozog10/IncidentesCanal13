"""Único camino de escritura web de cuentas, con exclusión entre administradores."""
from contextlib import contextmanager

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction

from incidents.access import ROLE_GROUPS, can, management_role


@contextmanager
def account_changes(actor):
    with transaction.atomic():
        # Serializa también decisiones sobre dos administradores distintos.
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [736382017204])
        fresh_actor = get_user_model().objects.get(pk=actor.pk)
        if not can(fresh_actor, "manage_dashboard_users"):
            raise PermissionDenied("Tu cuenta ya no tiene permisos administrativos.")
        yield fresh_actor


def check_target(actor, target):
    if target.is_superuser and not actor.is_superuser:
        raise PermissionDenied("Solo un superusuario técnico puede modificar esta cuenta.")


def ensure_administrator_remains(target, *, active, role):
    if target.pk and management_role(target) == "Administrador" and (not active or role != "Administrador"):
        other_admin = any(management_role(user) == "Administrador" for user in
            get_user_model().objects.filter(is_active=True).exclude(pk=target.pk).prefetch_related("groups"))
        if not other_admin:
            raise ValidationError("No puedes desactivar ni quitar el perfil al último administrador activo.")


def save_management_user(actor, data, pk=None):
    from incidents.user_forms import ManagementUserForm
    with account_changes(actor) as fresh_actor:
        if {"is_staff", "is_superuser", "groups", "user_permissions", "password"} & set(data):
            raise PermissionDenied("No se permite modificar privilegios técnicos desde este formulario.")
        target = get_user_model().objects.select_for_update().get(pk=pk) if pk else get_user_model()()
        check_target(fresh_actor, target)
        form = ManagementUserForm(data, instance=target)
        if not form.is_valid():
            return None, form
        # ModelForm modifica la instancia durante is_valid; comprobar sobre
        # el estado original para proteger al último administrador.
        original = get_user_model().objects.get(pk=pk) if pk else target
        ensure_administrator_remains(original, active=form.cleaned_data["is_active"], role=form.cleaned_data["role"])
        try:
            role_group = Group.objects.get(name=ROLE_GROUPS[form.cleaned_data["role"]])
        except Group.DoesNotExist:
            raise ValidationError("Falta inicializar los perfiles. Un responsable técnico debe aplicar la migración 0012_management_roles antes de crear o editar cuentas.")
        user = form.save(commit=False)
        if not pk:
            user.set_unusable_password()
        if not user.is_superuser:
            user.is_staff = form.cleaned_data["role"] in {"Administrador", "Supervisor"}
        user.save()
        user.groups.remove(*Group.objects.filter(name__in=ROLE_GROUPS.values()))
        user.groups.add(role_group)
        return user, form


def prepare_password_reset(actor, pk):
    with account_changes(actor) as fresh_actor:
        user = get_user_model().objects.select_for_update().get(pk=pk)
        check_target(fresh_actor, user)
        if user.pk == fresh_actor.pk:
            raise ValidationError("Para tu propia cuenta utiliza Cambiar mi contraseña; así conservas tu sesión administrativa.")
        if not user.is_active or management_role(user) is None:
            raise ValidationError("Activa la cuenta y asigna un perfil antes de restablecer su contraseña.")
        # Hash inutilizable aleatorio: invalida enlaces previos y sesiones sin
        # generar, mostrar ni enviar una contraseña en texto plano.
        user.set_unusable_password()
        user.save(update_fields=["password"])
        return user
