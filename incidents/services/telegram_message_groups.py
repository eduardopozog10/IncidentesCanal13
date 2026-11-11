"""Ventana durable por reportante; los textos originales nunca se reemplazan."""
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Timer

from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone

from incidents.models import ChannelMessage
from incidents.services.conversation_content import is_query_only, natural_cancel
from incidents.services.conversation_lock import conversation_lock
from incidents.services.incoming_message_service import IncomingMessageService
from incidents.pilot_logging import completed

logger = logging.getLogger(__name__)


class TelegramMessageGroups:
    MAX_MESSAGES = 20
    MAX_WAIT_SECONDS = 8

    def __init__(self, handler):
        self.handler = handler

    def _pending(self, reporter=None):
        rows = IncomingMessageService.recoverable_messages().filter(provider="telegram").order_by("pk")
        return rows.filter(technician_id=reporter) if reporter is not None else rows

    def message_text(self, row):
        """El adaptador de transporte puede normalizar comandos, sin alterar el original."""
        return row.content

    def _members(self, first):
        ids = first.processing_result.get("_group_member_ids")
        if ids:
            return list(ChannelMessage.objects.filter(pk__in=ids, technician_id=first.technician_id,
                channel_account_id=first.channel_account_id, direction="inbound").order_by("pk"))
        if first.processing_attempts or first.processing_result.get("reply"):
            return [first]
        candidates = list(self._pending(first.technician_id).filter(channel_account_id=first.channel_account_id,
            status="received", processing_attempts=0).order_by("pk")[:self.MAX_MESSAGES])
        members = []
        window = max(0, float(getattr(settings, "TELEGRAM_GROUP_WINDOW_SECONDS", 2.5)))
        for row in candidates:
            if members and (row.created_at - members[-1].created_at).total_seconds() > window:
                break
            text = self.message_text(row)
            if text == "/start":
                if not members: members.append(row)
                break
            members.append(row)
            if text == "/cancelar" or natural_cancel(text): break
        return members

    def _due(self, first, members, now):
        if first.processing_attempts or first.processing_result.get("reply"):
            return IncomingMessageService.retry_is_due(first) or first.processing_attempts >= IncomingMessageService.MAX_PROCESSING_ATTEMPTS
        window = max(0, min(float(getattr(settings, "TELEGRAM_GROUP_WINDOW_SECONDS", 2.5)), self.MAX_WAIT_SECONDS))
        return (len(members) >= self.MAX_MESSAGES
                or (now - max(row.created_at for row in members)).total_seconds() >= window
                or (now - first.created_at).total_seconds() >= self.MAX_WAIT_SECONDS)

    def ready_reporters(self, *, now=None):
        now, seen, ready = now or timezone.now(), set(), []
        for first in self._pending().iterator(chunk_size=100):
            if first.technician_id in seen: continue
            seen.add(first.technician_id)
            if self._due(first, self._members(first), now): ready.append(first.technician_id)
        return ready

    def process_reporter(self, reporter):
        with conversation_lock(f"reporter:{reporter}", wait=False) as acquired:
            if not acquired: return {"action": "group_busy", "durable": True}
            first = self._pending(reporter).first()
            if first is None: return {"action": "group_empty", "durable": True}
            members = self._members(first)
            if not self._due(first, members, timezone.now()): return {"action": "group_waiting", "durable": True}
            if not all(self.handler._is_admitted_private_update(row.raw_payload)
                       and row.raw_payload["message"].get("text") == row.content
                       and row.external_message_id == f"{row.external_chat_id}:{row.raw_payload['message'].get('message_id')}"
                       for row in members):
                return {"action": "group_ignored", "durable": True}
            # Congelar IDs antes de Gemini. Un reinicio reusa exactamente el grupo.
            ids = [row.pk for row in members]
            with transaction.atomic():
                first = ChannelMessage.objects.select_for_update().get(pk=first.pk)
                first.processing_result = {**first.processing_result, "_group_member_ids": ids}
                first.save(update_fields=["processing_result"])
                for row in members:
                    if row.pk != first.pk:
                        ChannelMessage.objects.filter(pk=row.pk).update(processing_result={"_group_leader_id": first.pk})
            ordered = sorted(members, key=lambda row: row.raw_payload["message"]["message_id"])
            texts = [self.message_text(row) for row in ordered]
            # Cancelar dentro de la misma ráfaga invalida la confirmación aún no
            # procesada. /start delimita una secuencia; no cancela un borrador.
            if "/cancelar" in texts or any(natural_cancel(value) for value in texts):
                text = "/cancelar"
            elif len(texts) == 1:
                text = texts[0]
            elif all(is_query_only(value) for value in texts):
                text = "\n".join(texts)
            else:
                text = "[Secuencia ordenada de mensajes del mismo reportante]\n" + "\n\n".join(
                    f"Mensaje {index}:\n{value}" for index, value in enumerate(texts, 1))
            logger.info('telegram.group_ready group=%s reporter=%s count=%s span=%.3fs wait=%.3fs recovery=%s',
                first.pk, reporter, len(ids), (max(row.created_at for row in members) - min(row.created_at for row in members)).total_seconds(),
                (timezone.now() - max(row.created_at for row in members)).total_seconds(), bool(first.processing_attempts))
            return self.handler.handle(first.raw_payload, processing_text=text, group_members=ids)


class TelegramGroupRunner:
    """Trabajadores del mismo receptor; PostgreSQL conserva el trabajo pendiente."""
    def __init__(self, handler, workers=2):
        self.handler = handler
        self.groups = TelegramMessageGroups(handler)
        self.workers = max(1, min(int(workers), 4))
        self.pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="secuoya-telegram")
        self.active = {}

    def tick(self):
        for reporter, future in list(self.active.items()):
            if future.done():
                try:
                    result = future.result()
                    logger.info("telegram.group_completed reporter=%s action=%s", reporter, result.get("action"))
                except Exception as error:
                    logger.warning("telegram.group_interrupted reporter=%s error=%s", reporter, type(error).__name__)
                    logging.getLogger('secuoya.pilot').warning('Procesamiento interrumpido: %s; revisar pendientes.', type(error).__name__)
                del self.active[reporter]
        for reporter in self.groups.ready_reporters():
            if len(self.active) >= self.workers: break
            if reporter not in self.active:
                self.active[reporter] = self.pool.submit(self._run, reporter)

    def _run(self, reporter):
        started = time.perf_counter()
        close_old_connections()
        first = self.groups._pending(reporter).first()
        timer = Timer(20, self._notice, args=(first.raw_payload,)) if first else None
        if timer:
            timer.daemon = True
            timer.start()
        try:
            result = self.groups.process_reporter(reporter)
            if first:
                first.refresh_from_db(fields=['processing_result','processing_attempts'])
                completed(result.get('action'), time.perf_counter()-started,
                    len(first.processing_result.get('_group_member_ids', [first.pk])), recovery=first.processing_attempts>1)
            return result
        finally:
            logger.info('telegram.group_finished reporter=%s duration=%.2fs', reporter, time.perf_counter() - started)
            if timer: timer.cancel()
            close_old_connections()

    def _notice(self, payload):
        close_old_connections()
        try:
            self.handler.send_processing_notice(payload)
        except Exception as error:
            logger.warning("telegram.group_notice_failed error=%s", type(error).__name__)
        finally:
            close_old_connections()

    def close(self):
        self.pool.shutdown(wait=True)
