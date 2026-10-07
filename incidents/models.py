import uuid

from django.db import models


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
        verbose_name = "técnico"
        verbose_name_plural = "técnicos"
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
        verbose_name="técnico",
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
        verbose_name = "canal de técnico"
        verbose_name_plural = "canales de técnicos"
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
    class Priority(models.TextChoices):
        LOW = "low", "Baja"
        MEDIUM = "medium", "Media"
        HIGH = "high", "Alta"
        CRITICAL = "critical", "Crítica"

    class Status(models.TextChoices):
        REPORTED = "reported", "Reportado"
        UNDER_REVIEW = "under_review", "En revisión"
        IN_PROGRESS = "in_progress", "En progreso"
        RESOLVED = "resolved", "Resuelto"
        CLOSED = "closed", "Cerrado"
        CANCELLED = "cancelled", "Cancelado"

    technician = models.ForeignKey(
        Technician,
        on_delete=models.PROTECT,
        related_name="incidents",
        verbose_name="técnico",
    )
    unit = models.CharField(
        max_length=150,
        verbose_name="unidad",
    )
    equipment = models.CharField(
        max_length=150,
        verbose_name="equipo",
    )
    failure_type = models.CharField(
        max_length=100,
        verbose_name="tipo de falla",
    )
    description = models.TextField(
        verbose_name="descripción",
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

    @property
    def code(self):
        if self.pk is None:
            return "TVT-PENDIENTE"

        return f"TVT-{self.pk:06d}"

    def __str__(self):
        return f"{self.code} - {self.equipment}"


class ChannelMessage(models.Model):
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
        verbose_name="canal del técnico",
    )
    technician = models.ForeignKey(
        Technician,
        on_delete=models.SET_NULL,
        related_name="messages",
        null=True,
        blank=True,
        verbose_name="técnico",
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
        verbose_name="técnico",
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