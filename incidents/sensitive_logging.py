import logging
import re


class PasswordLinkFilter(logging.Filter):
    """No dejar el token de establecimiento en el access log de runserver."""

    def filter(self, record):
        message = record.getMessage()
        redacted = re.sub(r"(/dashboard/password/)[^/\s]+/[^/\s]+/", r"\1[enlace-privado]/", message)
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True
