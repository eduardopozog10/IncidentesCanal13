"""Estado de transporte; no concede permisos ni contiene borradores de negocio."""
from django.core.exceptions import ValidationError
from django.db import models


class RoutingSession(models.Model):
    bot_id = models.CharField(max_length=30)
    sender_id = models.CharField(max_length=30)
    chat_id = models.CharField(max_length=30)
    module = models.CharField(max_length=15, blank=True, choices=[('incident', 'Incidencias'), ('content', 'Contenidos')])
    last_update_id = models.BigIntegerField(null=True)
    # Barreras de confirmación, sin borrar ni reescribir contextos del dominio.
    resume_after = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['bot_id', 'sender_id', 'chat_id'], name='routing_session_identity')]


class TelegramReceipt(models.Model):
    session = models.ForeignKey(RoutingSession, on_delete=models.PROTECT, related_name='receipts')
    bot_id = models.CharField(max_length=30)
    update_id = models.BigIntegerField()
    payload = models.JSONField()
    destination = models.CharField(max_length=15, choices=[('control', 'Control'), ('incident', 'Incidencias'), ('content', 'Contenidos'), ('blocked', 'Por revisar')])
    authorization = models.JSONField(default=dict)
    status = models.CharField(max_length=15, default='queued')
    reason = models.CharField(max_length=100, blank=True)
    incident_message = models.OneToOneField('incidents.ChannelMessage', null=True, on_delete=models.PROTECT, related_name='routing_receipt')
    content_message = models.OneToOneField('content_coordination.Message', null=True, on_delete=models.PROTECT, related_name='routing_receipt')
    # Los controles reutilizan la entrega durable de ChannelMessage; no pasan al agente.
    control_message = models.OneToOneField('incidents.ChannelMessage', null=True, on_delete=models.PROTECT, related_name='routing_control_receipt')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['bot_id', 'update_id'], name='routing_unique_bot_update')]
        indexes = [models.Index(fields=['bot_id', 'status'], name='routing_pending_idx')]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            immutable = ('session_id', 'bot_id', 'update_id', 'destination', 'payload', 'authorization')
            previous = type(self).objects.filter(pk=self.pk).values(*immutable).get()
            if any(previous[field] != getattr(self, field) for field in immutable):
                raise ValidationError('El destino y la identidad de una recepción son inmutables.')
        return super().save(*args, **kwargs)
