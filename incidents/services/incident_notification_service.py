import logging
import ipaddress
import smtplib
from socket import gaierror
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils import timezone
from django.urls import reverse
from django.db import transaction

from incidents.models import Incident, IncidentNotification
from incidents.services.conversation_lock import conversation_lock


logger = logging.getLogger(__name__)


def build_incident_url(incident_id):
    """Origen público HTTPS, con excepción exacta para el piloto local DEBUG."""
    base = getattr(settings, "INCIDENT_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if settings.DEBUG and base == "http://127.0.0.1:8000":
        return base + reverse("dashboard-incident-detail", kwargs={"pk": incident_id})
    try:
        URLValidator(schemes=["https"])(base)
        parsed = urlsplit(base)
        host = (parsed.hostname or "").rstrip(".")
        if (parsed.scheme != "https" or not host or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path or parsed.port not in (None, 443)
                or "." not in host or host.endswith((".localhost", ".local", ".internal"))):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
    except (ValueError, ValidationError):
        return None
    return base + reverse("dashboard-incident-detail", kwargs={"pk": incident_id})


def send_incident_created_email(
    incident_id: int,
) -> bool:
    with conversation_lock(f"notification:{incident_id}"):
        return _send_tracked_notification(incident_id)


def _send_tracked_notification(incident_id):
    if not Incident.objects.filter(pk=incident_id).exists():
        return False
    notification, _ = IncidentNotification.objects.get_or_create(incident_id=incident_id)
    with transaction.atomic():
        notification = IncidentNotification.objects.select_for_update().get(pk=notification.pk)
        if notification.status == "sent":
            return True
        if notification.status not in ("pending", "failed") or notification.attempts >= 3:
            return False
        notification.status = "sending"
        notification.attempts += 1
        notification.attempted_at = timezone.now()
        notification.save(update_fields=["status", "attempts", "attempted_at"])
    try:
        sent = _send_incident_created_email(incident_id)
        status = "sent" if sent else "failed"
        error_name = "" if sent else "NotSent"
    except (smtplib.SMTPConnectError, smtplib.SMTPAuthenticationError,
            smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused,
            ConnectionRefusedError, gaierror) as error:
        status, error_name, sent = "failed", type(error).__name__, False
    except Exception as error:
        # DATA pudo haber sido aceptado aunque no recibamos el acuse SMTP.
        status, error_name, sent = "uncertain", type(error).__name__, False
    IncidentNotification.objects.filter(pk=notification.pk).update(
        status=status, error_message=error_name, sent_at=timezone.now() if sent else None,
    )
    return sent


def _send_incident_created_email(incident_id):
    recipient = (
        settings.INCIDENT_NOTIFICATION_EMAIL.strip()
    )

    if not recipient:
        logger.warning(
            "No se envió la notificación del incidente %s "
            "porque INCIDENT_NOTIFICATION_EMAIL no está configurado.",
            incident_id,
        )
        return False

    try:
        incident = (
            Incident.objects.select_related(
                "technician"
            )
            .get(pk=incident_id)
        )
    except Incident.DoesNotExist:
        logger.error(
            "No se pudo enviar la notificación porque "
            "el incidente %s no existe.",
            incident_id,
        )
        return False

    priority = incident.get_priority_display()

    asset_category = (
        incident.get_asset_category_display()
        if incident.asset_category
        else "No informada"
    )

    reporter = (
        str(incident.technician)
        if incident.technician is not None
        else "No informado"
    )

    created_at = timezone.localtime(
        incident.created_at
    ).strftime(
        "%d/%m/%Y %H:%M"
    )

    subject = (
        f"[{priority.upper()}] "
        f"Nuevo incidente {incident.code} "
        f"- {incident.equipment}"
    )

    body = (
        "Se ha registrado un nuevo incidente "
        "en Secuoya Chile.\n\n"
        f"Código: {incident.code}\n"
        f"Prioridad: {priority}\n"
        f"Área: {incident.area or 'No informada'}\n"
        "Ubicación/contexto: "
        f"{incident.location_context or 'No informada'}\n"
        f"Familia: {asset_category}\n"
        "Equipo/servicio: "
        f"{incident.equipment}\n"
        f"Síntoma: {incident.failure_type}\n\n"
        "Descripción:\n"
        f"{incident.description}\n\n"
        "Impacto operacional:\n"
        f"{incident.operational_impact or 'No informado'}\n\n"
        f"Reportado por: {reporter}\n"
        f"Fecha y hora: {created_at}\n"
    )

    incident_url = build_incident_url(incident.pk)
    if incident_url:
        body += f"\nVer incidencia: {incident_url}\n"

    try:
        sent_count = send_mail(
            subject=subject,
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[recipient],
            fail_silently=False,
            html_message=render_to_string(
                "incidents/email/incident_created.html",
                {
                    "incident": incident,
                    "priority": priority,
                    "status": incident.get_status_display(),
                    "asset_category": asset_category,
                    "reporter": reporter,
                    "created_at": created_at,
                    "incident_url": incident_url,
                },
            ),
        )
    except Exception as error:
        logger.warning("Notificación %s: %s", incident.code, type(error).__name__)
        raise

    logger.info(
        "Notificación por correo enviada para %s.",
        incident.code,
    )

    return sent_count == 1
