import logging

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from incidents.models import Incident


logger = logging.getLogger(__name__)


def send_incident_created_email(
    incident_id: int,
) -> bool:
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

    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[recipient],
            fail_silently=False,
        )
    except Exception:
        logger.exception(
            "No fue posible enviar por correo "
            "la notificación del incidente %s.",
            incident.code,
        )
        return False

    logger.info(
        "Notificación por correo enviada para %s.",
        incident.code,
    )

    return True