import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, router, transaction


MANAGEMENT_NOTE_MAX_LENGTH = 1000
# Datos operativos, nunca payloads del canal, contraseñas ni mensajes originales.
INCIDENT_HISTORY_FIELDS = (
    "status", "priority", "technician", "unit", "area", "location_context",
    "asset_category", "equipment", "failure_type", "description", "operational_impact",
)


class Technician(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    full_name = models.CharField(
        max_length=150,
        verbose_name="nombre completo",
    )
    whatsapp_number = models.CharField(
        max_length=20,
        unique=True,
        null=True,
        blank=True,
        verbose_name="número de WhatsApp",
        help_text=(
            "Campo temporal para conservar los datos existentes. "
            "Las nuevas integraciones usarán canales de mensajería."
        ),
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name="activo",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="fecha de creación",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="última actualización",
    )

    class Meta:
        db_table = "technicians"
        verbose_name = "reportante"
        verbose_name_plural = "reportantes"
        ordering = ["full_name"]

    def __str__(self):
        return self.full_name


class TechnicianChannel(models.Model):
    class Provider(models.TextChoices):
        TELEGRAM = "telegram", "Telegram"
        WHATSAPP = "whatsapp", "WhatsApp"

    technician = models.ForeignKey(
        Technician,
        on_delete=models.CASCADE,
        related_name="channels",
        verbose_name="reportante",
    )
    provider = models.CharField(
        max_length=20,
        choices=Provider.choices,
        verbose_name="proveedor",
    )
    external_user_id = models.CharField(
        max_length=255,
        verbose_name="identificador externo del usuario",
    )
    external_chat_id = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="identificador externo del chat",
    )
    username = models.CharField(
        max_length=150,
        blank=True,
        verbose_name="nombre de usuario externo",
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name="activo",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="fecha de creación",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="última actualización",
    )

    class Meta:
        db_table = "technician_channels"
        verbose_name = "canal de reportante"
        verbose_name_plural = "canales de reportantes"
        ordering = ["technician__full_name", "provider"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "external_user_id"],
                name="unique_user_per_messaging_provider",
            ),
        ]

    def __str__(self):
        return (
            f"{self.technician.full_name} "
            f"- {self.get_provider_display()}"
        )


class Incident(models.Model):
    class AssetCategory(models.TextChoices):
        VIDEO_CAMERAS = "video_cameras", "Video / Cámaras"
        AUDIO = "audio", "Audio"
        STUDIOS_PRODUCTION = "studios_production", "Estudios / Producción"
        SIGNAL_TRANSMISSION = "signal_transmission", "Señal / Transmisión"
        MOBILE_FIELD = "mobile_field", "Móviles / Terreno"
        NETWORKS = "networks", "Redes"
        SYSTEMS = "systems", "Sistemas"
        DIGITAL_STREAMING = "digital_streaming", "Digital / Streaming"
        INFRASTRUCTURE = "infrastructure", "Infraestructura"

    class Priority(models.TextChoices):
        LOW = "low", "Baja"
        MEDIUM = "medium", "Media"
        HIGH = "high", "Alta"
        CRITICAL = "critical", "Crítica"

    class Status(models.TextChoices):
        REPORTED = "reported", "Reportado"
        UNDER_REVIEW = "under_review", "En revisión"
        IN_PROGRESS = "in_progress", "En atención"
        RESOLVED = "resolved", "Resuelto"
        CLOSED = "closed", "Cerrado"
        CANCELLED = "cancelled", "Cancelado"

    technician = models.ForeignKey(
        Technician,
        on_delete=models.PROTECT,
        related_name="incidents",
        verbose_name="reportante",
    )
    unit = models.CharField(
        max_length=150,
        verbose_name="unidad",
    )

    area = models.CharField(
        max_length=150,
        blank=True,
        default="",
        verbose_name="área",
    )

    location_context = models.CharField(
        max_length=150,
        blank=True,
        default="",
        verbose_name="ubicación o contexto",
    )

    asset_category = models.CharField(
        max_length=30,
        choices=AssetCategory.choices,
        blank=True,
        default="",
        verbose_name="familia de equipo o servicio",
    )

    equipment = models.CharField(
        max_length=150,
        verbose_name="equipo o servicio afectado",
    )
    failure_type = models.CharField(
        max_length=100,
        verbose_name="tipo de falla",
    )
    description = models.TextField(
        verbose_name="descripción",
    )

    operational_impact = models.TextField(
        blank=True,
        default="",
        verbose_name="impacto operacional",
    )

    priority = models.CharField(
        max_length=20,
        choices=Priority.choices,
        default=Priority.MEDIUM,
        verbose_name="prioridad",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.REPORTED,
        verbose_name="estado",
    )
    original_message = models.TextField(
        verbose_name="mensaje original",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="fecha de creación",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="última actualización",
    )
    resolved_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="fecha de resolución",
    )

    class Meta:
        db_table = "incidents"
        verbose_name = "incidente"
        verbose_name_plural = "incidentes"
        ordering = ["-created_at"]
        permissions = [
            ("access_dashboard", "Acceder al centro de control"),
            ("view_incident_analytics", "Consultar analítica de incidencias"),
            ("change_incident_status", "Cambiar estados de incidencias"),
            ("change_incident_priority", "Cambiar prioridades de incidencias"),
            ("manage_dashboard_users", "Administrar usuarios y perfiles del dashboard"),
        ]

    @property
    def code(self):
        if self.pk is None:
            return "SC-PENDIENTE"

        return f"SC-{self.pk:06d}"

    def __str__(self):
        return f"{self.code} - {self.equipment}"

    def save(self, *args, history_actor=None, history_note="", history_source="internal", **kwargs):
        # La creación (incluido Telegram) no es un cambio de gestión.
        if self.pk is None:
            return super().save(*args, **kwargs)
        if len(history_note) > MANAGEMENT_NOTE_MAX_LENGTH:
            raise ValidationError("La observación no puede superar 1000 caracteres.")
        if history_source not in IncidentChange.Source.values:
            raise ValidationError("Origen del cambio no válido.")
        using = kwargs.get("using") or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            previous = type(self).objects.using(using).select_for_update().filter(pk=self.pk).first()
            if previous is None:
                return super().save(*args, **kwargs)
            update_fields = kwargs.get("update_fields")
            changes = []
            for name in INCIDENT_HISTORY_FIELDS:
                field = self._meta.get_field(name)
                if update_fields is not None and not {name, field.attname}.intersection(update_fields):
                    continue
                old, new = getattr(previous, field.attname), getattr(self, field.attname)
                if old != new:
                    # Conservar también el nombre histórico del reportante.
                    if name == "technician":
                        old, new = f"{previous.technician} [{old}]", f"{self.technician} [{new}]"
                    changes.append((name, str(old), str(new)))
            result = super().save(*args, **kwargs)
            actor = history_actor if history_actor and history_actor.is_authenticated else None
            label = "Sistema / proceso interno"
            if actor:
                full_name = actor.get_full_name().strip()
                label = f"{full_name} (@{actor.get_username()})" if full_name else actor.get_username()
            IncidentChange.objects.using(using).bulk_create([
                IncidentChange(
                    incident=self, actor=actor, actor_label=label, field=name,
                    action={"status": "status_changed", "priority": "priority_changed"}.get(name, "field_changed"),
                    old_value=old, new_value=new, observation=history_note.strip(), source=history_source,
                ) for name, old, new in changes
            ])
            return result


class IncidentChange(models.Model):
    class Source(models.TextChoices):
        DASHBOARD = "dashboard", "Dashboard"
        ADMIN = "admin", "Administración Django"
        INTERNAL = "internal", "Proceso interno"

    class Action(models.TextChoices):
        STATUS = "status_changed", "Cambio de estado"
        PRIORITY = "priority_changed", "Cambio de prioridad"
        FIELD = "field_changed", "Cambio de información operativa"

    incident = models.ForeignKey(Incident, on_delete=models.PROTECT, related_name="changes", editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, editable=False)
    actor_label = models.CharField(max_length=512, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    action = models.CharField(max_length=24, choices=Action.choices, editable=False)
    field = models.CharField(max_length=30, editable=False)
    old_value = models.TextField(editable=False)
    new_value = models.TextField(editable=False)
    observation = models.CharField(max_length=MANAGEMENT_NOTE_MAX_LENGTH, blank=True, editable=False)
    source = models.CharField(max_length=16, choices=Source.choices, editable=False)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["incident", "-created_at", "-id"], name="incident_history_recent")]
        default_permissions = ()
        verbose_name = "cambio de incidencia"
        verbose_name_plural = "cambios de incidencias"

    @property
    def field_display(self):
        return str(Incident._meta.get_field(self.field).verbose_name).capitalize()

    def display_value(self, value):
        return str(dict(Incident._meta.get_field(self.field).flatchoices).get(value, value))


class IncidentNotification(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pendiente"
        SENDING = "sending", "Enviando / verificar antes de reintentar"
        SENT = "sent", "Enviada"
        FAILED = "failed", "Fallida, recuperable"
        UNCERTAIN = "uncertain", "Entrega incierta, revisión manual"

    incident = models.OneToOneField(Incident, on_delete=models.CASCADE, related_name="notification")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    error_message = models.TextField(blank=True)
    attempted_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)


class ChannelMessage(models.Model):
    received_via_polling = models.BooleanField(default=False)
    processing_result = models.JSONField(default=dict, blank=True)
    processing_attempts = models.PositiveSmallIntegerField(default=0)
    in_reply_to = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="replies")
    reply_purpose = models.CharField(max_length=16, default="reply")
    class Provider(models.TextChoices):
        TELEGRAM = "telegram", "Telegram"
        WHATSAPP = "whatsapp", "WhatsApp"
        TEST = "test", "Prueba"

    class Direction(models.TextChoices):
        INBOUND = "inbound", "Recibido"
        OUTBOUND = "outbound", "Enviado"

    class MessageType(models.TextChoices):
        TEXT = "text", "Texto"
        AUDIO = "audio", "Audio"
        IMAGE = "image", "Imagen"
        DOCUMENT = "document", "Documento"
        UNKNOWN = "unknown", "Desconocido"

    class Status(models.TextChoices):
        RECEIVED = "received", "Recibido"
        PROCESSING = "processing", "Procesando"
        PROCESSED = "processed", "Procesado"
        SENT = "sent", "Enviado"
        DELIVERED = "delivered", "Entregado"
        READ = "read", "Leído"
        FAILED = "failed", "Fallido"
        UNCERTAIN = "uncertain", "Entrega incierta, revisión manual"

    provider = models.CharField(
        max_length=20,
        choices=Provider.choices,
        default=Provider.WHATSAPP,
        verbose_name="proveedor",
    )
    external_message_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        verbose_name="identificador externo del mensaje",
    )
    external_sender_id = models.CharField(
        max_length=255,
        verbose_name="identificador externo del remitente",
    )
    external_chat_id = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="identificador externo del chat",
    )
    channel_account = models.ForeignKey(
        TechnicianChannel,
        on_delete=models.SET_NULL,
        related_name="messages",
        null=True,
        blank=True,
        verbose_name="canal del reportante",
    )
    technician = models.ForeignKey(
        Technician,
        on_delete=models.SET_NULL,
        related_name="messages",
        null=True,
        blank=True,
        verbose_name="reportante",
    )
    incident = models.ForeignKey(
        Incident,
        on_delete=models.SET_NULL,
        related_name="messages",
        null=True,
        blank=True,
        verbose_name="incidente",
    )
    draft = models.ForeignKey(
        "IncidentDraft",
        on_delete=models.SET_NULL,
        related_name="messages",
        null=True,
        blank=True,
        verbose_name="borrador",
    )
    direction = models.CharField(
        max_length=10,
        choices=Direction.choices,
        verbose_name="dirección",
    )
    message_type = models.CharField(
        max_length=20,
        choices=MessageType.choices,
        default=MessageType.TEXT,
        verbose_name="tipo de mensaje",
    )
    content = models.TextField(
        blank=True,
        verbose_name="contenido",
    )
    raw_payload = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="datos originales",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.RECEIVED,
        verbose_name="estado",
    )
    error_message = models.TextField(
        blank=True,
        verbose_name="detalle del error",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="fecha de creación",
    )
    processed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="fecha de procesamiento",
    )

    class Meta:
        db_table = "channel_messages"
        verbose_name = "mensaje de canal"
        verbose_name_plural = "mensajes de canales"
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "external_message_id"],
                condition=models.Q(
                    external_message_id__isnull=False,
                ),
                name="unique_message_per_provider",
            ),
            models.UniqueConstraint(fields=["in_reply_to", "reply_purpose"], condition=models.Q(in_reply_to__isnull=False), name="unique_logical_reply"),
        ]

    def __str__(self):
        return (
            f"{self.get_provider_display()} "
            f"- {self.get_direction_display()} "
            f"- {self.external_sender_id}"
        )


class IncidentDraft(models.Model):
    class Status(models.TextChoices):
        COLLECTING = "collecting", "Recopilando información"
        READY = "ready", "Información completa"
        COMPLETED = "completed", "Incidente creado"
        EXPIRED = "expired", "Expirado"
        CANCELLED = "cancelled", "Cancelado"

    technician = models.ForeignKey(
        Technician,
        on_delete=models.PROTECT,
        related_name="incident_drafts",
        verbose_name="reportante",
    )
    initial_message = models.OneToOneField(
        ChannelMessage,
        on_delete=models.SET_NULL,
        related_name="started_draft",
        null=True,
        blank=True,
        verbose_name="mensaje inicial",
    )
    incident = models.OneToOneField(
        Incident,
        on_delete=models.SET_NULL,
        related_name="source_draft",
        null=True,
        blank=True,
        verbose_name="incidente creado",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.COLLECTING,
        verbose_name="estado",
    )
    extracted_data = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="datos extraídos",
    )
    missing_fields = models.JSONField(
        default=list,
        blank=True,
        verbose_name="campos faltantes",
    )
    last_question = models.TextField(
        blank=True,
        verbose_name="última pregunta realizada",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="fecha de creación",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="última actualización",
    )
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="fecha de expiración",
    )
    completed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="fecha de finalización",
    )

    class Meta:
        db_table = "incident_drafts"
        verbose_name = "borrador de incidente"
        verbose_name_plural = "borradores de incidentes"
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["technician"],
                condition=models.Q(
                    status__in=["collecting", "ready"],
                ),
                name="unique_active_draft_per_technician",
            ),
        ]

    def __str__(self):
        return (
            f"Borrador de {self.technician.full_name} "
            f"- {self.get_status_display()}"
        )
