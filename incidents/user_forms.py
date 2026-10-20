from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError

from .access import ROLE_PERMISSIONS, can


class DashboardAuthenticationForm(AuthenticationForm):
    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not can(user, "access_dashboard"):
            raise ValidationError("Esta cuenta no tiene acceso al panel de gestión.", code="no_dashboard")


class ManagementUserForm(forms.ModelForm):
    role = forms.ChoiceField(label="Perfil", choices=[(role, role) for role in ROLE_PERMISSIONS], initial="Visualizador")

    class Meta:
        model = get_user_model()
        fields = ("username", "first_name", "last_name", "email", "role", "is_active")
        labels = {"username": "Nombre de usuario", "first_name": "Nombre", "last_name": "Apellido", "email": "Correo", "is_active": "Cuenta activa"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].required = True
        self.fields["first_name"].required = True
        self.fields["last_name"].required = True
        if self.instance.pk:
            from .access import assigned_role
            self.fields["role"].initial = assigned_role(self.instance) or "Visualizador"
        if self.instance.is_superuser:
            self.fields["role"].disabled = True
            self.fields["role"].initial = "Administrador"

    def clean_email(self):
        value = self.cleaned_data["email"].strip().lower()
        if get_user_model().objects.filter(email__iexact=value).exclude(pk=self.instance.pk).exists():
            raise ValidationError("Ya existe una cuenta con este correo.")
        return value

    def clean_username(self):
        value = self.cleaned_data["username"].strip()
        if get_user_model().objects.filter(username__iexact=value).exclude(pk=self.instance.pk).exists():
            raise ValidationError("Ya existe una cuenta con este nombre de usuario.")
        return value
