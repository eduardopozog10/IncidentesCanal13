from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin, GroupAdmin
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied

from .access import ROLE_GROUPS, can

from .models import (
    ChannelMessage,
    Incident,
    IncidentDraft,
    Technician,
    TechnicianChannel,
)


class TechnicianChannelInline(admin.TabularInline):
    model = TechnicianChannel
    extra = 0
    fields = (
        "provider",
        "external_user_id",
        "external_chat_id",
        "username",
        "is_active",
    )


@admin.register(Technician)
class TechnicianAdmin(admin.ModelAdmin):
    list_display = (
        "full_name",
        "whatsapp_number",
        "is_active",
        "created_at",
    )
    list_filter = ("is_active",)
    search_fields = (
        "full_name",
        "whatsapp_number",
        "channels__external_user_id",
        "channels__username",
    )
    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
    )
    ordering = ("full_name",)
    list_per_page = 25
    inlines = (TechnicianChannelInline,)


@admin.register(TechnicianChannel)
class TechnicianChannelAdmin(admin.ModelAdmin):
    list_display = (
        "technician",
        "provider",
        "external_user_id",
        "external_chat_id",
        "username",
        "is_active",
    )
    list_filter = (
        "provider",
        "is_active",
    )
    search_fields = (
        "technician__full_name",
        "external_user_id",
        "external_chat_id",
        "username",
    )
    readonly_fields = (
        "created_at",
        "updated_at",
    )
    list_select_related = ("technician",)
    ordering = (
        "technician__full_name",
        "provider",
    )
    list_per_page = 25


@admin.register(Incident)
class IncidentAdmin(admin.ModelAdmin):
    def has_view_permission(self, request, obj=None):
        return can(request.user, "view_incident")

    def has_change_permission(self, request, obj=None):
        return can(request.user, "change_incident_status") or can(request.user, "change_incident_priority")

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if not can(request.user, "manage_dashboard_users"):
            allowed = set()
            if can(request.user, "change_incident_status"):
                allowed.add("status")
            if can(request.user, "change_incident_priority"):
                allowed.add("priority")
            fields.extend(field.name for field in self.model._meta.fields if field.name not in allowed)
        return list(dict.fromkeys(fields))

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        if request.method == "POST" and not can(request.user, "manage_dashboard_users"):
            if "priority" in request.POST and not can(request.user, "change_incident_priority"):
                raise PermissionDenied
            if "status" in request.POST and not can(request.user, "change_incident_status"):
                raise PermissionDenied
            if set(request.POST) & {"technician", "unit", "equipment", "failure_type", "description", "original_message"}:
                raise PermissionDenied
        return super().changeform_view(request, object_id, form_url, extra_context)

    list_display = (
        "incident_code",
        "technician",
        "unit",
        "equipment",
        "failure_type",
        "priority",
        "status",
        "created_at",
    )
    list_filter = (
        "priority",
        "status",
        "created_at",
    )
    search_fields = (
        "unit",
        "equipment",
        "failure_type",
        "description",
        "original_message",
        "technician__full_name",
        "technician__whatsapp_number",
        "technician__channels__external_user_id",
    )
    readonly_fields = (
        "incident_code",
        "created_at",
        "updated_at",
        "resolved_at",
    )
    list_select_related = ("technician",)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_per_page = 25

    fieldsets = (
        (
            "Identificación",
            {
                "fields": (
                    "incident_code",
                    "technician",
                    "status",
                    "priority",
                )
            },
        ),
        (
            "Información del incidente",
            {
                "fields": (
                    "unit",
                    "equipment",
                    "failure_type",
                    "description",
                    "original_message",
                )
            },
        ),
        (
            "Fechas",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                    "resolved_at",
                )
            },
        ),
    )

    @admin.display(
        description="Código",
        ordering="id",
    )
    def incident_code(self, obj):
        return obj.code


@admin.register(ChannelMessage)
class ChannelMessageAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "provider",
        "external_sender_id",
        "direction",
        "message_type",
        "status",
        "technician",
        "incident",
    )
    list_filter = (
        "provider",
        "direction",
        "message_type",
        "status",
        "created_at",
    )
    search_fields = (
        "external_message_id",
        "external_sender_id",
        "external_chat_id",
        "content",
        "technician__full_name",
    )
    readonly_fields = (
        "created_at",
        "processed_at",
    )
    list_select_related = (
        "technician",
        "incident",
        "channel_account",
    )
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_per_page = 50


@admin.register(IncidentDraft)
class IncidentDraftAdmin(admin.ModelAdmin):
    list_display = (
        "technician",
        "status",
        "incident",
        "updated_at",
        "expires_at",
    )
    list_filter = (
        "status",
        "updated_at",
    )
    search_fields = (
        "technician__full_name",
        "technician__whatsapp_number",
        "technician__channels__external_user_id",
    )
    readonly_fields = (
        "created_at",
        "updated_at",
        "completed_at",
    )
    list_select_related = (
        "technician",
        "incident",
    )
    ordering = ("-updated_at",)
    date_hierarchy = "updated_at"


# Las cuentas se escriben exclusivamente por el módulo que protege al último
# administrador y valida actor/objetivo dentro de una transacción serializada.
admin.site.unregister(get_user_model())


@admin.register(get_user_model())
class ManagedUserAdmin(UserAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        return {}


admin.site.unregister(Group)


@admin.register(Group)
class ManagedGroupAdmin(GroupAdmin):
    def has_add_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return self.has_add_permission(request) and (obj is None or obj.name not in ROLE_GROUPS.values())

    def has_delete_permission(self, request, obj=None):
        return self.has_change_permission(request, obj)

    def delete_queryset(self, request, queryset):
        if queryset.filter(name__in=ROLE_GROUPS.values()).exists():
            raise PermissionDenied
        super().delete_queryset(request, queryset)
    list_per_page = 25
