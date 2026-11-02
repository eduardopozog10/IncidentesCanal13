# Recuperación y estabilidad del piloto · Codex 2

**Actualización 27/10/2026:** la [agrupación durable](qa/conversaciones-2026-10-27.md) separa recepción y procesamiento con trabajadores locales acotados. Conserva los locks, intentos, cooldown y entregas inciertas documentados aquí; `recover_pilot` mantiene los miembros de un grupo. Las referencias siguientes a polling conversacional secuencial corresponden a la etapa anterior y a la modalidad con ventana cero. No se añadió una migración nueva. [Validación real pendiente](qa/conversaciones-2026-10-27-manual.md).

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

En la validación de Codex 2 solo se aplicó dentro de la base temporal de tests QA. Posteriormente el usuario confirmó la aplicación de `0011_pilot_recovery` a su PostgreSQL local. La corrección puntual descrita a continuación no añade ni ejecuta migraciones.

Con la migración autorizada/aplicada, el comando normal `uv run python manage.py run_telegram_bot` inicia recuperación y worker de correo. Si se cierra el receptor, no hay recuperación ni envíos en segundo plano: quedan pendientes hasta reiniciarlo. Una caída durante SMTP puede requerir revisión manual del estado `sending`. No se añadió un servicio permanente ni se promete disponibilidad 24/7.

## Corrección puntual de recuperación y Gemini

Causas comprobadas en código y pruebas aisladas:

- `processing` se confirma antes de consultar IA. Un cierre abrupto, `KeyboardInterrupt` o `SystemExit` puede impedir registrar el fallo; no significa por sí solo que se haya perdido el mensaje. `TimeoutError` puede corresponder a un intento anterior porque se conserva el error al iniciar otro.
- El SDK instalado normaliza `HttpRetryOptions(attempts=0)` a `1`, y su puente de Interactions traduce `1` a **un reintento adicional**, aunque el cliente padre lo documenta como un intento total. Por eso la configuración anterior no evitaba solicitudes internas ni imponía un límite total de tiempo. La prueba con el SDK real y `httpx.MockTransport` verifica ahora dos solicitudes en total ante 500 y 503, sin red. Esto explica una vía para exceder 45 segundos; no demuestra la cronología exacta de los aproximadamente 65 segundos observados en producción.
- La recuperación esperaba el lock del reportante y no tenía pausa entre fallos. Un receptor ocupado podía retrasar a los demás; un proveedor caído recibía intentos sucesivos sin enfriamiento.

Política actual, sin cambios de esquema:

- El límite sigue siendo **tres intentos de procesamiento por mensaje**. La pausa predeterminada es 30 segundos; los fallos recuperables de Gemini indican 60 segundos. `_retry_not_before` se guarda en el JSON `processing_result` existente, sin modificar el payload de Telegram. Al comenzar un intento también se guarda una pausa de 30 segundos para un posible reinicio temprano. Los tests anteriores fijan esa pausa en cero únicamente para conservar sus escenarios inmediatos; las nuevas regresiones prueban el tiempo real de la política con reloj simulado.
- El receptor intenta adquirir el advisory lock sin esperar. Si otro proceso lo posee, deja el mensaje durable y no lo declara abandonado. Si la sesión anterior terminó, PostgreSQL libera el lock y el mensaje se recupera cuando vence su pausa. Un registro histórico como el descrito para 675, sin fecha de pausa y con dos intentos, dispone de un tercer intento cuando el lock está libre. **No se ejecutó esa recuperación sobre el registro real.**
- El barrido omite reportantes activos o aplazados sin consumir su cupo de recuperaciones y continúa con otros. Conserva el orden del mismo reportante. El polling sigue siendo secuencial: una solicitud en curso puede retrasar otras hasta su presupuesto, pero no espera indefinidamente a Gemini ni al lock de otro receptor. El avance del offset no cambia.
- Un intento interrumpido al alcanzar el máximo, incluso un `failed` sin resultado, se convierte en fallo terminal con respuesta persistida. Se mantienen contenido, identidad, contador y error previo. La entrega de esa respuesta utiliza la misma deduplicación y presupuesto de Telegram. Tras resolverla o quedar incierta/agotada, los mensajes posteriores pueden continuar. Un rechazo permanente de configuración de IA genera respuesta terminal desde el primer intento, sin repetir una solicitud inválida.
- Interactions tiene desactivado explícitamente su reintento adicional en la configuración del recurso generado. El proyecto permite hasta dos solicitudes por intento de procesamiento: primera espera de hasta 30 segundos y segunda con el presupuesto restante, **45 segundos de espera total**. Agotar la consulta activa un enfriamiento de 60 segundos en la instancia del proveedor, y la pausa del mensaje también queda persistida. Los intentos durante indisponibilidad cuentan dentro del límite aunque el enfriamiento impida abrir otra solicitud.
- Un pequeño hilo daemon contiene exclusivamente la llamada al SDK. Si el transporte no termina dentro de su espera, el llamador continúa con un fallo recuperable; no aplica respuestas tardías. No se abre otra solicitud en esa instancia mientras la anterior siga viva. **No puede cancelarse forzosamente una llamada síncrona de Python ni garantizar que el servidor remoto la cancele**: puede quedar una consulta pendiente hasta que el transporte termine o cierre el proceso. El hilo no escribe en la base ni crea incidencias. La configuración del recurso generado depende del SDK instalado; la prueba de transporte detecta regresiones al actualizarlo.
- Se distinguen timeout/conexión, indisponibilidad/cuota y rechazo de configuración/solicitud. No se cambia modelo, credenciales, prompts ni reglas de incidentes. Un resultado ya confirmado tras crear una incidencia sigue recuperando únicamente la respuesta; se conservan las limitaciones de entrega incierta anteriores.

Validación de esta corrección: 13 regresiones nuevas sobre el estado de 675, interrupción durante IA, pausa persistida, agotamiento y liberación posterior, replay, aislamiento por reportante, 500/503 y transporte bloqueado. También se conservan las pruebas de caída después de crear una incidencia y de ausencia de respuestas duplicadas de Codex 2. Batería completa: **181 descubiertas, 179 aprobadas y las mismas 2 omitidas explícitamente**. `manage.py check --settings=config.qa` y `git diff --check` correctos. El receptor operativo permanece detenido; no se consultaron ni cambiaron los registros 675, 668 o 666.

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
