from django.apps import AppConfig


class IncidentsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'incidents'

    def ready(self):
        import logging
        from .sensitive_logging import PasswordLinkFilter
        logger = logging.getLogger("django.server")
        if not any(isinstance(item, PasswordLinkFilter) for item in logger.filters):
            logger.addFilter(PasswordLinkFilter())
