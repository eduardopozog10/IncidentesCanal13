from django.contrib import admin
from .models import Participant, ContentRequest, Task, ContentVersion, History, Message, Outbox, TelegramConsent, ParticipantAccessEvent, TelegramLinkTransfer


@admin.register(Participant)
class ParticipantAdmin(admin.ModelAdmin):
    list_display = ['name', 'role', 'active', 'telegram_authorized']
    # La demo no habilita nuevas cuentas Telegram desde la interfaz.
    readonly_fields = ['telegram_id', 'telegram_authorized']


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


for model in (ContentRequest, Task, ContentVersion, History, Message, Outbox, TelegramConsent, ParticipantAccessEvent, TelegramLinkTransfer):
    admin.site.register(model, ReadOnlyAdmin)
