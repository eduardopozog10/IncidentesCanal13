from django.conf import settings
from django.db import models
from django.utils import timezone
import uuid


class Participant(models.Model):
    class Role(models.TextChoices):
        JOURNALIST = 'journalist', 'Periodista'
        COORDINATOR = 'coordinator', 'Coordinador'
        DESIGNER = 'designer', 'Diseñador'
        REVIEWER = 'reviewer', 'Revisor autorizado'
        EDITOR = 'editor', 'Editor'
        SWITCH = 'switch', 'Switch'
        ADMIN = 'admin', 'Administrador'
    user = models.OneToOneField(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    name = models.CharField(max_length=150)
    role = models.CharField(max_length=20, choices=Role.choices)
    telegram_id = models.CharField(max_length=30, unique=True, null=True, blank=True)
    telegram_authorized = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    def __str__(self):
        return f'{self.name} · {self.get_role_display()}'


class ContentRequest(models.Model):
    class State(models.TextChoices):
        PENDING = 'pending', 'Pendiente de asignación'
        ASSIGNED = 'assigned', 'Asignada'
        PRODUCTION = 'production', 'En producción'
        REVIEW = 'review', 'En revisión'
        APPROVED = 'approved', 'Aprobada'
        EDITING = 'editing', 'En edición'
        RECEIVED = 'received', 'Recibida por Switch'
        EMITTED = 'emitted', 'Emisión confirmada'
        CANCELLED = 'cancelled', 'Cancelada'
    title = models.CharField(max_length=180)
    description = models.TextField()
    requester = models.ForeignKey(Participant, on_delete=models.PROTECT, related_name='requests')
    requesting_area = models.CharField(max_length=100)
    responsible_area = models.CharField(max_length=100)
    due_at = models.DateTimeField(null=True, blank=True)
    priority = models.CharField(max_length=15, default='normal', choices=[('normal','Normal'),('high','Alta')])
    state = models.CharField(max_length=20, choices=State.choices, default=State.PENDING)
    revision = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    @property
    def code(self):
        return f'CT-{self.pk:06d}'
    def __str__(self):
        return f'{self.code} · {self.title}'


class Task(models.Model):
    request = models.ForeignKey(ContentRequest, on_delete=models.PROTECT, related_name='tasks')
    kind = models.CharField(max_length=20, choices=[('design','Diseño'),('review','Revisión'),('editing','Edición'),('switch','Recepción Switch')])
    assignee = models.ForeignKey(Participant, null=True, blank=True, on_delete=models.PROTECT, related_name='tasks')
    state = models.CharField(max_length=20, default='pending', choices=[('pending','Pendiente'),('working','En producción'),('review','Pendiente de revisión'),('done','Completada'),('cancelled','Cancelada')])
    assigned_at = models.DateTimeField(null=True)
    started_at = models.DateTimeField(null=True)
    due_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True)
    last_progress = models.TextField(blank=True)
    progress_at = models.DateTimeField(null=True)
    eta_minutes = models.PositiveIntegerField(null=True, blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['request','kind'], name='content_task_kind_unique')]


class ContentVersion(models.Model):
    request = models.ForeignKey(ContentRequest, on_delete=models.PROTECT, related_name='versions')
    number = models.CharField(max_length=20)
    author = models.ForeignKey(Participant, on_delete=models.PROTECT, related_name='+')
    state = models.CharField(max_length=20, choices=[('working','En trabajo'),('review','En revisión'),('approved','Aprobada'),('changes','Correcciones')])
    created_at = models.DateTimeField(auto_now_add=True)
    approved_by = models.ForeignKey(Participant, null=True, on_delete=models.PROTECT, related_name='+')
    approved_at = models.DateTimeField(null=True)
    edited_by = models.ForeignKey(Participant, null=True, on_delete=models.PROTECT, related_name='+')
    edited_at = models.DateTimeField(null=True)
    received_by = models.ForeignKey(Participant, null=True, on_delete=models.PROTECT, related_name='+')
    received_at = models.DateTimeField(null=True)
    emitted_by = models.ForeignKey(Participant, null=True, on_delete=models.PROTECT, related_name='+')
    emitted_at = models.DateTimeField(null=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['request','number'], name='content_version_unique')]


class Conversation(models.Model):
    participant = models.OneToOneField(Participant, on_delete=models.PROTECT)
    module = models.CharField(max_length=15, default='')
    draft = models.JSONField(default=dict)
    pending = models.JSONField(default=dict)
    context = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)


class Message(models.Model):
    participant = models.ForeignKey(Participant, on_delete=models.PROTECT)
    external_id = models.CharField(max_length=100, unique=True)
    text = models.TextField()
    payload = models.JSONField(default=dict)
    channel = models.CharField(max_length=15, default='telegram')
    status = models.CharField(max_length=20, default='pending')
    attempts = models.PositiveIntegerField(default=0)
    retry_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    result = models.JSONField(default=dict)
    error_type = models.CharField(max_length=100, blank=True)


class History(models.Model):
    request = models.ForeignKey(ContentRequest, on_delete=models.PROTECT, related_name='history')
    task = models.ForeignKey(Task, null=True, on_delete=models.PROTECT)
    actor = models.ForeignKey(Participant, on_delete=models.PROTECT)
    operation = models.CharField(max_length=30)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    channel = models.CharField(max_length=20)
    message = models.ForeignKey(Message, null=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class Outbox(models.Model):
    recipient = models.ForeignKey(Participant, on_delete=models.PROTECT)
    request = models.ForeignKey(ContentRequest, null=True, on_delete=models.PROTECT)
    source = models.ForeignKey(Message, null=True, on_delete=models.PROTECT)
    key = models.CharField(max_length=180, unique=True)
    text = models.TextField()
    status = models.CharField(max_length=20, default='pending')
    attempts = models.PositiveIntegerField(default=0)
    retry_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True)
    telegram_message_id = models.CharField(max_length=80, blank=True)
    delivery_parts = models.JSONField(default=list)
    error_type = models.CharField(max_length=100, blank=True)

    @property
    def status_label(self):
        return {'pending': 'Pendiente de envío', 'retry': 'Reintento programado',
                'sending': 'Enviando', 'sent': 'Entrega confirmada', 'local': 'Respuesta del panel',
                'blocked': 'Sin destinatario autorizado', 'suppressed':'Confirmado en la conversación', 'superseded': 'Etapa ya resuelta', 'uncertain': 'Entrega por verificar',
                'failed': 'Envío fallido'}.get(self.status, self.status)


class ProgressQuery(models.Model):
    requester = models.ForeignKey(Participant, on_delete=models.PROTECT, related_name='+')
    worker = models.ForeignKey(Participant, on_delete=models.PROTECT, related_name='+')
    tasks = models.ManyToManyField(Task)
    notification = models.OneToOneField(Outbox, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    answered_at = models.DateTimeField(null=True)


class TelegramConsent(models.Model):
    participant = models.OneToOneField(Participant, on_delete=models.PROTECT)
    telegram_id = models.CharField(max_length=30)
    consent_at = models.DateTimeField()
    verified_at = models.DateTimeField(null=True)
    revoked_at = models.DateTimeField(null=True)
    bound_at = models.DateTimeField(null=True)
    verified_update_id = models.BigIntegerField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['telegram_id'], condition=models.Q(revoked_at__isnull=True),
                                               name='content_one_active_consent_per_id')]


class TelegramLinkTransfer(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(Participant, on_delete=models.PROTECT, related_name='telegram_transfers_out')
    target = models.ForeignKey(Participant, on_delete=models.PROTECT, related_name='telegram_transfers_in')
    telegram_id = models.CharField(max_length=30)
    state = models.CharField(max_length=15, default='active', choices=[('active','Activa'),('restored','Restaurada')])
    source_snapshot = models.JSONField()
    target_snapshot = models.JSONField()
    operator = models.ForeignKey(Participant, on_delete=models.PROTECT, related_name='+')
    restore_operator = models.ForeignKey(Participant, null=True, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    restored_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['telegram_id'],condition=models.Q(state='active'),name='content_one_active_transfer_per_id'),
            models.UniqueConstraint(fields=['source'],condition=models.Q(state='active'),name='content_one_transfer_source'),
            models.UniqueConstraint(fields=['target'],condition=models.Q(state='active'),name='content_one_transfer_target'),
        ]


class ParticipantAccessEvent(models.Model):
    participant = models.ForeignKey(Participant, on_delete=models.PROTECT)
    action = models.CharField(max_length=30)
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
