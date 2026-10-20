from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.views import LoginView, PasswordResetConfirmView, PasswordResetCompleteView, PasswordChangeView
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.decorators import method_decorator
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST

from .access import ROLE_GROUPS, ROLE_PERMISSIONS, assigned_role, require_management
from .services.management_user_service import check_target, prepare_password_reset, save_management_user
from .user_forms import DashboardAuthenticationForm, ManagementUserForm


@method_decorator(never_cache, name="dispatch")
class DashboardLoginView(LoginView):
    authentication_form = DashboardAuthenticationForm
    template_name = "incidents/users/login.html"

    def get_default_redirect_url(self):
        return reverse("dashboard")


@method_decorator(never_cache, name="dispatch")
class LocalPasswordConfirmView(PasswordResetConfirmView):
    template_name = "incidents/users/password_confirm.html"
    success_url = reverse_lazy("dashboard-password-complete")

    def get_user(self, uidb64):
        user = super().get_user(uidb64)
        return user if user and user.is_active and assigned_role(user) else None

    def dispatch(self, *args, **kwargs):
        response = super().dispatch(*args, **kwargs)
        # La URL original contiene una credencial: tampoco debe enviarse como
        # Referer al redirigir o mostrar un enlace inválido. El formulario nativo
        # vive en /set-password/, sin el token, y necesita Origin en sus POST.
        response["Referrer-Policy"] = (
            "same-origin" if kwargs["token"] == self.reset_url_token else "no-referrer"
        )
        return response


class LocalPasswordCompleteView(PasswordResetCompleteView):
    template_name = "incidents/users/password_complete.html"


@method_decorator(never_cache, name="dispatch")
@method_decorator(require_management(), name="dispatch")
class DashboardPasswordChangeView(PasswordChangeView):
    template_name = "incidents/users/password_change.html"
    success_url = reverse_lazy("dashboard")


def save_handoff(request, user):
    path = reverse("dashboard-password-confirm", kwargs={"uidb64": urlsafe_base64_encode(force_bytes(user.pk)),
        "token": default_token_generator.make_token(user)})
    request.session["password_handoff"] = {"url": request.build_absolute_uri(path), "username": user.username}


@never_cache
@require_management("manage_dashboard_users")
def user_list(request):
    users = get_user_model().objects.all().prefetch_related("groups").order_by("last_name", "first_name", "username")
    search = request.GET.get("q", "").strip()
    role, state = request.GET.get("role", ""), request.GET.get("state", "")
    for term in search.split():
        users = users.filter(Q(username__icontains=term) | Q(first_name__icontains=term) | Q(last_name__icontains=term) | Q(email__icontains=term))
    if role in ROLE_GROUPS:
        # Incluir correctamente perfiles históricos virtuales y cuentas
        # inactivas, sin asignarles grupos ni tocar sus datos.
        users = users.filter(pk__in=[user.pk for user in users if assigned_role(user) == role])
    if state in {"active", "inactive"}:
        users = users.filter(is_active=state == "active")
    page = Paginator(users, 20).get_page(request.GET.get("page"))
    rows = [{"account": user, "profile": assigned_role(user) or "Sin perfil", "editable": request.user.is_superuser or not user.is_superuser} for user in page]
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "incidents/users/list.html", {"rows": rows, "page_obj": page, "roles": ROLE_PERMISSIONS,
        "search": search, "selected_role": role, "selected_state": state, "filter_query": params.urlencode(), "title": "Usuarios y permisos"})


@never_cache
@require_management("manage_dashboard_users")
@sensitive_post_parameters()
def user_edit(request, pk=None):
    account = get_object_or_404(get_user_model(), pk=pk) if pk else get_user_model()()
    check_target(request.user, account)
    form = ManagementUserForm(instance=account)
    if request.method == "POST":
        try:
            saved, form = save_management_user(request.user, request.POST, pk)
            if saved:
                messages.success(request, "Cuenta actualizada." if pk else "Cuenta creada. Falta establecer su contraseña.")
                if not pk and saved.is_active:
                    save_handoff(request, saved)
                    return redirect("dashboard-user-handoff")
                return redirect("dashboard-users")
        except ValidationError as error:
            form = ManagementUserForm(request.POST, instance=account)
            form.is_valid()
            form.add_error(None, error)
    return render(request, "incidents/users/form.html", {"form": form, "account": account, "title": "Editar usuario" if pk else "Crear usuario"})


@never_cache
@require_management("manage_dashboard_users")
@require_POST
def user_password_reset(request, pk):
    get_object_or_404(get_user_model(), pk=pk)
    try:
        user = prepare_password_reset(request.user, pk)
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
        return redirect("dashboard-user-edit", pk=pk)
    save_handoff(request, user)
    return redirect("dashboard-user-handoff")


@never_cache
@require_management("manage_dashboard_users")
def user_handoff(request):
    handoff = request.session.pop("password_handoff", None)
    if not handoff:
        return redirect("dashboard-users")
    response = render(request, "incidents/users/handoff.html", {"handoff": handoff, "title": "Establecer contraseña"})
    # El token está en el contenido, no en la URL de esta página. same-origin
    # evita referencias externas y permite el POST con CSRF de Cerrar sesión.
    response["Referrer-Policy"] = "same-origin"
    return response
