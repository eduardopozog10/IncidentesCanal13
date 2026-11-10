"""Transporte de polling demo; no confirma offsets por su cuenta."""
import json
import math
import time
from datetime import datetime, timezone

import httpx
from django.core.management.base import CommandError
from django.core.exceptions import PermissionDenied

from .models import Message
from .routing import admit


class TelegramPollingClient:
    def __init__(self, client, api_url, write, *, sleep=time.sleep, now=time.monotonic):
        self.client = client
        self.api_url = api_url
        self.write = write
        self.sleep = sleep
        self.now = now
        self.next_poll_at = 0

    def wait(self, seconds):
        # Tramos cortos para permitir Ctrl+C incluso con retry_after largo.
        deadline = self.now() + seconds
        while self.now() < deadline:
            self.sleep(max(0, min(30, deadline - self.now())))

    def call(self, method, data=None):
        payload = dict(data or {})
        while True:
            if method == 'getUpdates':
                self.wait(max(0, self.next_poll_at - self.now()))
                self.next_poll_at = self.now() + 1
            try:
                response = self.client.post(self.api_url + '/' + method, json=payload)
                body = response.json()
            except (httpx.RequestError, ValueError):
                raise CommandError(f'No fue posible consultar {method}; offsets y mensajes conservados.') from None
            if not isinstance(body, dict):
                raise CommandError(f'Respuesta inválida de {method}; no se confirmó ningún offset nuevo.')
            if response.status_code == 429 or body.get('error_code') == 429:
                parameters = body.get('parameters') or {}
                retry = parameters.get('retry_after') if isinstance(parameters, dict) else None
                if retry is None:
                    retry = response.headers.get('Retry-After', 30)
                try:
                    delay = float(retry)
                    if not math.isfinite(delay) or delay <= 0:
                        raise ValueError()
                except (ValueError, TypeError):
                    delay = 30
                self.write(f'Telegram HTTP 429 en {method}: espera de {delay:g} s; se conserva el offset. Ctrl+C para detener.')
                self.wait(delay)
                continue  # Mismo payload, nunca incrementar ni descartar el offset.
            if response.status_code >= 400 or not body.get('ok'):
                raise CommandError(f'Telegram rechazó {method}: HTTP {response.status_code}.')
            return body['result']


def inspect_pending(updates, requested_id):
    """Solo lectura; texto visible únicamente para el participante autorizado."""
    update = next((u for u in updates if u.get('update_id') == requested_id), None)
    if update is None:
        return 'ID no visible entre las primeras 100 actualizaciones. No se avanzó el offset para buscarlo.'
    try:
        actor, text = admit(update)
    except PermissionDenied:
        return json.dumps({'update_id': requested_id, 'authorized': False,
                           'notice': 'Contenido oculto: no pertenece a una conversación admitida.'}, ensure_ascii=True)
    message = update['message']
    stored = Message.objects.filter(external_id=f'telegram:{requested_id}').first()
    date = message.get('date')
    try:
        date = datetime.fromtimestamp(date, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        date = 'Fecha no válida o ausente'
    # JSON escapa controles de terminal y evita interpretar el texto como instrucciones.
    return json.dumps({'update_id': requested_id, 'authorized': True,
                       'sender_id': actor.telegram_id, 'participant': actor.name,
                       'date_utc': date, 'message_id': message.get('message_id'), 'text': text,
                       'local_status': stored.status if stored else 'no persistida',
                       'notice': 'Solo inspección. No autorizada, procesada ni confirmada.'}, ensure_ascii=True)
