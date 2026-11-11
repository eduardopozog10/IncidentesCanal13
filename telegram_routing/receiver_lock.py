"""Exclusión local por bot compartida por demo, Incidencias y QA."""
import hashlib
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings
from django.core.management.base import CommandError


@contextmanager
def receiver_lock():
    token_hash = hashlib.sha256(settings.TELEGRAM_BOT_TOKEN.encode()).hexdigest()[:20]
    lock_path = Path(tempfile.gettempdir()) / f'secuoya-receiver-{token_hash}.lock'
    with lock_path.open('a+b') as file:
        file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise CommandError('Receptor activo: ya existe un receptor para este bot.') from None
        try:
            yield
        finally:
            if os.name == 'nt':
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)


def incident_receiver_exclusive(function):
    """También protege el script QA, que llama directamente a Command.handle."""
    from functools import wraps

    @wraps(function)
    def wrapped(*args, **kwargs):
        with receiver_lock():
            return function(*args, **kwargs)
    return wrapped
