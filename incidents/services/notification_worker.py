"""Un worker local del receptor; la intención durable vive en PostgreSQL."""
import threading
from datetime import timedelta
import logging

from django.db import close_old_connections
from django.db.models import Q
from django.utils import timezone

from incidents.models import IncidentNotification
from incidents.services.incident_notification_service import send_incident_created_email

logger = logging.getLogger(__name__)


def drain_notifications(limit=10):
    rows = IncidentNotification.objects.filter(status__in=["pending", "failed"], attempts__lt=3).filter(
        Q(attempted_at__isnull=True) | Q(attempted_at__lte=timezone.now() - timedelta(seconds=30))
    ).order_by("pk")
    for row in rows[:limit]:
        if row.attempted_at and row.attempted_at > timezone.now() - timedelta(seconds=30):
            continue
        send_incident_created_email(row.incident_id)


def start_notification_worker(stop):
    def run():
        while not stop.is_set():
            close_old_connections()
            try:
                drain_notifications()
            except Exception as error:
                logger.warning("Worker local de correo: %s", type(error).__name__)
            finally:
                close_old_connections()
            stop.wait(5)

    thread = threading.Thread(target=run, name="secuoya-mail", daemon=True)
    thread.start()
    return thread
