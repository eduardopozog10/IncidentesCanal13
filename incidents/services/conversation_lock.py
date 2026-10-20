"""Exclusión por conversación, sin transacción abierta durante llamadas externas.

PostgreSQL libera el advisory lock de sesión al perderse la conexión/proceso.
No sustituye los bloqueos cortos de filas usados para confirmar incidencias.
"""
from contextlib import contextmanager
from hashlib import blake2b

from django.db import connection


@contextmanager
def conversation_lock(identity, *, wait=True):
    key = int.from_bytes(blake2b(str(identity).encode(), digest_size=8).digest(), "big", signed=True)
    with connection.cursor() as cursor:
        function = "pg_advisory_lock" if wait else "pg_try_advisory_lock"
        cursor.execute(f"SELECT {function}(%s)", [key])
        acquired = True if wait else cursor.fetchone()[0]
    try:
        yield acquired
    finally:
        # Una conexión perdida ya liberó sus locks. No abrir otra para liberar.
        if acquired and connection.connection is not None:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [key])
