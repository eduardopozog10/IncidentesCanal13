# Confirmaciones y seguimiento Fernando → Daniela

Entorno: `config.content_demo`, PostgreSQL `demo_secuoya_contenidos`.
La corrección no requiere migraciones ni cambios de `.env` o de vínculos.
No se modificó CT-000003 ni la transferencia temporal activa durante el desarrollo.

## Qué se corrigió

- El procesador eliminaba `pending` antes de interpretar cualquier respuesta fuera
  de una lista cerrada. Ahora conserva la operación ante entradas desconocidas,
  pausas, cortesías y consultas informativas. Una corrección fallida conserva su
  información, pero exige revisar un resumen nuevo antes de ejecutar.
- Se reutilizan los controles puros de confirmación del agente de incidencias
  (negaciones, citas, condiciones y terceros), con tolerancia acotada de un error
  tipográfico en una palabra completa como `confirmoS`. Se mantienen los controles
  de canal, entrega del resumen, rol, consentimiento, revisión e idempotencia.
- `query` consulta datos registrados; `ask` prepara un contacto. El contrato de IA,
  el enrutamiento Django y el servicio de seguimiento distinguen ambas acciones.
  La oferta de seguimiento también funciona con un CT explícito cuando no existe
  avance detallado o el último tiene al menos 20 minutos.
- El responsable y las tareas se resuelven en PostgreSQL. La confirmación guarda
  la pregunta literal en el outbox y una consulta persistente en `ProgressQuery`.
  No se envía un aviso por una consulta informativa ni por una mera propuesta de IA.
- Una respuesta parcial y su estimación actualizan la tarea propia, sin finalizarla.
  Se vincula solo con una consulta realmente entregada antes de recibir la respuesta.
  Si hay varias tareas posibles, pide el CT. Un cambio de asignación no permite
  aplicar la respuesta a otro trabajo ni entregar un aviso al responsable anterior.
- El bloqueo del destinatario y el outbox evitan seguimientos superpuestos y
  entregas duplicadas. Una entrega incierta sigue requiriendo revisión manual.

## Reinicio seguro

El receptor que ya estaba ejecutándose no recarga automáticamente los archivos.
En su terminal, detén **esa instancia** con Ctrl+C; no abras otra en paralelo.
Desde la raíz del proyecto:

```powershell
uv run python manage.py check --settings=config.content_demo
uv run python manage.py run_secuoya_demo --settings=config.content_demo --preflight
```

El preflight debe comprobarse antes del arranque. Si muestra actualizaciones
pendientes, no las descartes ni las autorices en conjunto. Inspecciona cada ID:

```powershell
uv run python manage.py run_secuoya_demo --settings=config.content_demo --inspect-pending-id ID_REAL
```

Con cola vacía, inicia una sola instancia:

```powershell
uv run python manage.py run_secuoya_demo --settings=config.content_demo --live
```

Si existen pendientes revisados, sigue el procedimiento habitual de aprobación
por sus IDs específicos. Este desarrollo no los leyó mediante Telegram, no avanzó
offsets, no los descartó ni inició otro receptor. No transfieras/restaures vínculos.

## Recorrido manual

Usa una solicitud NUEVA para conservar CT-000003 intacta. Sustituye `CT-NUEVO`
por el código real generado; no lo escribas literalmente.

1. Fernando: `/contenido`; crea una solicitud con sus datos y confirma el resumen.
2. Fernando: «Asigna el diseño de CT-NUEVO a Daniela». Espera recibir el resumen y
   responde `confirmoS`. Debe guardarse una sola asignación y llegar un solo aviso.
3. Daniela: `/contenido`; «Voy a comenzar el diseño de CT-NUEVO».
4. Fernando: «¿Cómo va Daniela con CT-NUEVO? Dime su estado y último avance».
   Debe leer PostgreSQL y ofrecer consultarle, sin enviar nada a Daniela aún.
5. Fernando: «Quiero que le preguntes a Daniela cómo va con CT-NUEVO y cuánto
   tiempo estima que le falta». Debe mostrar destinatario, CT y pregunta, sin envío.
6. Fernando: «Sí, pregúntale». Debe quedar un seguimiento y un solo aviso a Daniela.
   Repetir la confirmación o la petición mientras está abierta no duplica el aviso.
7. Daniela, después de recibirlo: «Me faltan 15 minutos». Si hay varias tareas
   consultadas, responde con su CT. Debe conservarse el diseño en producción,
   guardarse el avance/estimación y notificarse la respuesta a Fernando.
8. Fernando: «¿Cuál fue el último avance de CT-NUEVO?». Comprueba texto y estimación
   en Telegram y en el detalle del dashboard. No debe figurar como finalizado,
   aprobado, recibido ni emitido por haber indicado un tiempo restante.

También puedes responder «No, gracias» a una oferta: no se contacta a nadie.
Una entrada desconocida debe conservar la operación y pedir aclaración. Si cambia
el CT, el responsable o una precondición, revisa el resumen nuevo antes de confirmar.

## Alcance de la validación

Resultado: 179 pruebas de Contenidos aprobadas, incluidas 32 nuevas regresiones;
627 pruebas de Incidencias, con 625 aprobadas y 2 omitidas, sin fallos.
`manage.py check` no detectó problemas y `makemigrations --check --dry-run` no
detectó cambios. No se añadieron migraciones ni dependencias.

La lectura final confirmó que el snapshot de CT-000003 coincide con el anterior:
revisión 3, diseño en producción asignado a Daniela, sin avance/estimación ni
versiones nuevas. La transferencia `bdf62dea-4d35-474e-8fad-5cebf91215b8` sigue activa,
sin restauración ni cambios de vínculos.

Pruebas automáticas con PostgreSQL temporal, intérprete de IA simulado y proveedor
Telegram simulado. Cubren contexto, preguntas, consentimiento, privacidad, estados,
entrega, temporalidad, idempotencia, concurrencia y dashboard. No sustituyen la
repetición manual con Gemini y Telegram reales; esa repetición queda al usuario.
