import threading
import time

import httpx
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from incidents.messaging.telegram_update_handler import (
    TelegramUpdateHandler,
)


class Command(BaseCommand):
    help = (
        "Ejecuta el bot de Telegram mediante long polling "
        "para desarrollo local."
    )

    PROCESSING_NOTICE_SECONDS = 20.0

    def add_arguments(self, parser):
        parser.add_argument(
            "--discard-pending",
            action="store_true",
            help=(
                "Descarta las actualizaciones antiguas pendientes "
                "antes de comenzar a escuchar mensajes nuevos."
            ),
        )

    def handle(self, *args, **options):
        token = settings.TELEGRAM_BOT_TOKEN.strip()

        if not token:
            raise CommandError(
                "TELEGRAM_BOT_TOKEN no está configurado."
            )

        api_url = (
            f"https://api.telegram.org/bot{token}"
        )

        handler = TelegramUpdateHandler()
        offset = None

        timeout = httpx.Timeout(
            35.0,
            connect=10.0,
        )

        self.stdout.write(
            self.style.SUCCESS(
                "Bot de Telegram iniciado."
            )
        )

        self.stdout.write(
            "Presiona Ctrl+C para detenerlo."
        )

        try:
            with httpx.Client(timeout=timeout) as client:
                if options["discard_pending"]:
                    offset = self._discard_pending_updates(
                        client=client,
                        api_url=api_url,
                    )

                while True:
                    try:
                        updates = self._get_updates(
                            client=client,
                            api_url=api_url,
                            offset=offset,
                            timeout_seconds=25,
                        )
                    except httpx.RequestError:
                        self.stderr.write(
                            self.style.WARNING(
                                "No fue posible conectar con Telegram. "
                                "Reintentando en 3 segundos..."
                            )
                        )
                        time.sleep(3)
                        continue
                    except RuntimeError as error:
                        raise CommandError(str(error)) from error

                    for update in updates:
                        update_id = update.get("update_id")

                        if isinstance(update_id, int):
                            offset = update_id + 1

                        started_at = time.perf_counter()

                        processing_notice_timer = threading.Timer(
                            self.PROCESSING_NOTICE_SECONDS,
                            self._send_processing_notice,
                            kwargs={
                                "handler": handler,
                                "update": update,
                            },
                        )

                        processing_notice_timer.daemon = True
                        processing_notice_timer.start()

                        try:
                            result = handler.handle(update)
                        except Exception as error:
                            processing_notice_timer.cancel()

                            elapsed = (
                                time.perf_counter()
                                - started_at
                            )

                            self.stderr.write(
                                self.style.ERROR(
                                    "Error inesperado procesando "
                                    "una actualización "
                                    f"después de {elapsed:.2f} s: "
                                    f"{error}"
                                )
                            )
                            continue
                        finally:
                            processing_notice_timer.cancel()

                        elapsed = (
                            time.perf_counter()
                            - started_at
                        )

                        action = result.get(
                            "action",
                            "unknown",
                        )

                        self.stdout.write(
                            "Actualización procesada: "
                            f"{action} "
                            f"({elapsed:.2f} s)"
                        )

        except KeyboardInterrupt:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    "Bot de Telegram detenido."
                )
            )

    def _send_processing_notice(
        self,
        *,
        handler: TelegramUpdateHandler,
        update: dict,
    ):
        close_old_connections()

        try:
            result = handler.send_processing_notice(
                update
            )

            action = result.get(
                "action",
                "processing_notice_unknown",
            )

            if action == "processing_notice_sent":
                self.stdout.write(
                    self.style.WARNING(
                        "Aviso de procesamiento enviado "
                        f"tras {self.PROCESSING_NOTICE_SECONDS:.0f} s."
                    )
                )

        except Exception as error:
            self.stderr.write(
                self.style.WARNING(
                    "No fue posible enviar el aviso de "
                    f"procesamiento: {error}"
                )
            )
        finally:
            close_old_connections()

    def _get_updates(
        self,
        *,
        client: httpx.Client,
        api_url: str,
        offset: int | None,
        timeout_seconds: int,
    ) -> list[dict]:
        payload = {
            "timeout": timeout_seconds,
            "allowed_updates": ["message"],
        }

        if offset is not None:
            payload["offset"] = offset

        response = client.post(
            f"{api_url}/getUpdates",
            json=payload,
        )

        try:
            response_data = response.json()
        except ValueError:
            raise RuntimeError(
                "Telegram devolvió una respuesta inválida."
            ) from None

        if (
            response.status_code >= 400
            or not response_data.get("ok")
        ):
            description = response_data.get(
                "description",
                "Error desconocido de Telegram.",
            )

            raise RuntimeError(
                "Telegram rechazó getUpdates: "
                f"{description}"
            )

        return response_data.get("result", [])

    def _discard_pending_updates(
        self,
        *,
        client: httpx.Client,
        api_url: str,
    ) -> int | None:
        offset = None
        discarded_count = 0

        self.stdout.write(
            "Revisando actualizaciones antiguas pendientes..."
        )

        while True:
            updates = self._get_updates(
                client=client,
                api_url=api_url,
                offset=offset,
                timeout_seconds=0,
            )

            if not updates:
                break

            discarded_count += len(updates)

            last_update_id = updates[-1].get("update_id")

            if not isinstance(last_update_id, int):
                break

            offset = last_update_id + 1

        if discarded_count:
            self.stdout.write(
                self.style.WARNING(
                    f"Se descartaron {discarded_count} "
                    "actualizaciones antiguas."
                )
            )
        else:
            self.stdout.write(
                "No había actualizaciones antiguas pendientes."
            )

        return offset