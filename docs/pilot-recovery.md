# Recuperación y estabilidad del piloto · Codex 2

## Cambios y garantías

- La deduplicación ahora distingue un resultado confirmado de un intento incompleto. El mensaje se conserva antes del procesamiento y el resultado se confirma en la misma transacción que los cambios del borrador o la incidencia. Un replay reutiliza el resultado; no vuelve a ejecutar la creación.
- Se conservan hasta tres intentos automáticos. Tras agotarlos, el mensaje queda fallido, con su contenido retenido y una respuesta de revisión; no permanece indefinidamente en procesamiento. Una caída antes de registrar el fallo también se normaliza al recuperar.
- Las respuestas y avisos tienen una identidad lógica única por mensaje. Se guardan antes de enviar; los resúmenes largos conservan el acuse de cada parte. Un rechazo explícito recuperable de Telegram permite reintentar solo las partes no confirmadas. Un rechazo permanente agota el presupuesto de entrega. `/start` y `/cancelar` también guardan su resultado: repetir una cancelación anterior no cancela un borrador nuevo.
- El polling avanza el offset solo cuando el handler termina normalmente con un resultado seguro o una recepción durable. Si falla antes de esa persistencia, no avanza por actualizaciones posteriores. La recuperación de mensajes guardados no depende de que Telegram reentregue el update; procesa uno por ciclo para no vaciar un gran backlog antes de consultar novedades.
- La marca `received_via_polling` se asigna desde Python, nunca desde JSON HTTP. Solo esos mensajes, con metadatos coherentes y canal/reportante activos, se recuperan automáticamente. No se reproducen los registros históricos sin prueba de origen ni los mensajes de la herramienta HTTP.
- Un fallo de correo no revierte la incidencia. La intención de notificar se guarda con ella; un worker local del receptor la envía en otra conexión/hilo. SMTP tiene timeout por operación de 10 segundos, configurable mediante `EMAIL_TIMEOUT`. Se conservan resultado e intentos; una notificación enviada no se vuelve a enviar automáticamente.

## Transacciones y orden

El procesador conserva su lógica conversacional. Primero determina dentro de una transacción corta si necesita extracción; revierte ese intento de planificación, consulta Gemini fuera de la transacción y aplica el resultado en otro tramo atómico. Si cambió el contexto, no aplica la extracción antigua sin reconsiderarlo.

Un advisory lock de sesión PostgreSQL por reportante conserva la exclusión durante ese intervalo. **Sigue existiendo ese bloqueo lógico**, pero no una transacción abierta ni el bloqueo de su fila durante Gemini. Reportantes diferentes no comparten ese lock. Al perderse la sesión del proceso se libera, sin depender de un temporizador de lease. Cancelaciones usan la misma exclusión. Los mensajes se ordenan por recepción persistida; las entradas posteriores esperan si hay una anterior recuperable.

El polling continúa siendo secuencial para el procesamiento conversacional; no se promete paralelismo de recepción ni se implementó otra infraestructura. Las llamadas externas todavía pueden demorar dentro de sus límites. Si otro llamador envuelve todo en una transacción propia, no puede esperarse que el procesador cierre esa transacción externa.

## Incertidumbre de entrega

Telegram y SMTP pueden aceptar un envío y perder el acuse, o el proceso puede terminar antes de guardar el resultado local. No es posible garantizar «exactamente una vez» en ese intervalo con estos protocolos.

- Telegram: estado `uncertain`, o `processing` interrumpido, que se convierte en incierto al recuperar. No se reenvía automáticamente. Si todos los acuses de las partes ya estaban guardados, puede completarse el registro sin enviar otra vez.
- SMTP: `uncertain`; una caída durante el envío deja `sending`, que también exige revisión. Los fallos inequívocos de conexión/autenticación/destinatario quedan `failed` y tienen reintentos acotados, separados al menos 30 segundos. Un timeout SMTP se trata conservadoramente como incierto.
- No se puede retirar una respuesta ya entregada. La recuperación evita volver a enviar las partes confirmadas, pero un reenvío manual de una parte incierta puede duplicarla. Hay dos pruebas explícitamente pendientes para la garantía imposible de «exactamente una vez sin acuse»; las pruebas de estado incierto y ausencia de reenvío automático sí se ejecutan.

## Migración y activación

`0011_pilot_recovery` añade campos a `ChannelMessage` (origen, intentos, resultado, relación y propósito de respuesta), una restricción para la respuesta lógica y `IncidentNotification`, con una fila por incidencia. Amplía las opciones de estado de mensajes. No borra incidentes ni transforma textos históricos. Las relaciones nuevas son anulables y los mensajes antiguos quedan sin origen de polling acreditado. No se encolan correos históricos al migrar, para evitar reenviar notificaciones antiguas.

**No se aplicó a la base operativa.** Solo se ejecutó dentro de la base temporal de tests QA. Antes de activar el código en el piloto, solicitar autorización para aplicar esta migración y detener/reiniciar los procesos locales según corresponda. No ejecutar la nueva versión contra un esquema anterior.

Con la migración autorizada/aplicada, el comando normal `uv run python manage.py run_telegram_bot` inicia recuperación y worker de correo. Si se cierra el receptor, no hay recuperación ni envíos en segundo plano: quedan pendientes hasta reiniciarlo. Una caída durante SMTP puede requerir revisión manual del estado `sending`. No se añadió un servicio permanente ni se promete disponibilidad 24/7.

## Recuperación manual controlada

Primero detener el receptor y revisar qué llegó al destinatario. Los comandos siguientes usan el entorno seleccionado y **pueden hacer envíos reales**; no se ejecutaron contra servicios reales durante la validación.

```powershell
# Solo inspección; no envía ni reproduce conversaciones:
uv run python manage.py recover_pilot

# Hasta diez mensajes recuperables, sin forzar entregas inciertas:
uv run python manage.py recover_pilot --run

# Reanudar un mensaje específico; un fallo agotado solo se reabre si
# no creó incidencia y la conversación no recibió mensajes posteriores:
uv run python manage.py recover_pilot --message-id <ID_INTERNO>

# Reintentar respuesta o notificación con fallo conocido:
uv run python manage.py recover_pilot --reply-id <ID_INTERNO>
uv run python manage.py recover_pilot --notification-id <ID_INTERNO>
```

Para un envío incierto, el comando se niega sin `--acknowledge-duplicate-risk`. Añadirlo solo después de verificar la entrega y aceptar conscientemente el riesgo. Los ya enviados no se repiten, incluso con esa opción. No se reproduce un resumen antiguo si la conversación avanzó ni se reenvía información a canales/reportantes deshabilitados. Si la conversación avanzó tras un fallo de procesamiento agotado, el comando exige revisión humana y no mezcla el texto antiguo en el borrador nuevo.

## Validación

Pruebas de la etapa anterior conservadas. Se añaden simulaciones de recepción persistida, caída durante el intento, caída tras crear la incidencia, fallo al confirmar el resultado, acuses perdidos, multipartes, replay de comandos, timeouts y 503 de Gemini, concurrencia por reportante y entre reportantes, SMTP lento/fallido y recuperación manual. Se verifica que Gemini y SMTP se ejecuten fuera de transacciones propias y que el worker SMTP no detenga el procesamiento del receptor.

Todo se ejecuta con mocks, correo en memoria y PostgreSQL de tests mediante `--settings=config.qa`. No hubo llamadas reales a Telegram/Gemini, envío SMTP, migración operativa, despliegue, commit ni push. No se modificaron las reglas conversacionales, las plantillas del correo, el dashboard ni Gym Planet.

Resultado final: **168 pruebas descubiertas: 166 aprobadas y 2 omitidas explícitamente por las limitaciones de entrega sin acuse**. Las 127 pruebas anteriores se conservaron sin cambios. `manage.py check`, `makemigrations --check --dry-run --settings=config.qa` y `git diff --check` correctos.

## Archivos modificados

- `incidents/models.py`, `incidents/migrations/0011_pilot_recovery.py`.
- `incidents/services/incoming_message_service.py`, `incident_processor.py`, `incident_draft_service.py`, `incident_notification_service.py`, `conversation_lock.py` y `notification_worker.py` (todos bajo `incidents/services/`).
- `incidents/messaging/telegram_update_handler.py`, `incidents/messaging/telegram_provider.py`.
- `incidents/management/commands/run_telegram_bot.py`, `incidents/management/commands/recover_pilot.py`.
- `config/settings.py`, `.env.example` (solo timeout; sin cambios de credenciales).
- `incidents/test_pilot_recovery.py`, `README.md` y este documento.
