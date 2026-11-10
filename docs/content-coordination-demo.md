# Coordinación de contenidos — demostración local

Estado al 9 de noviembre de 2026. Rama `feature/coordinacion-contenidos-demo`.
Primera versión funcional en PostgreSQL independiente. Sin commit ni push.

## Inicio rápido en Windows / PowerShell

Abre VS Code en el repositorio. PostgreSQL local debe estar iniciado.
No cambies `.env`: se reutilizan sus credenciales y el modelo Gemini configurado.
Todos los comandos siguientes seleccionan explícitamente la base de demostración.

```powershell
Set-Location 'C:\Users\eduar\OneDrive\Documentos\GitHub\IncidentesCanal13'
uv run python manage.py check --settings=config.content_demo
```

La base `demo_secuoya_contenidos` ya está creada y migrada. Para preparar otra
instalación local con la misma configuración:

```powershell
uv run python manage.py prepare_content_demo --settings=config.content_demo
```

Este comando crea únicamente esa base si falta, aplica migraciones y crea
participantes de demo. No copia ni modifica registros operativos. No borra
solicitudes existentes. Si un usuario ya tiene otra configuración, se detiene.

### Terminal 1: dashboard

La cuenta `demo_admin` se creó sin contraseña utilizable. Define tú su contraseña
local mediante el diálogo de PowerShell (no escribas contraseñas en archivos):

```powershell
uv run python manage.py changepassword demo_admin --settings=config.content_demo
uv run python manage.py runserver 127.0.0.1:8000 --settings=config.content_demo
```

Abre <http://127.0.0.1:8000/contents/>. Inicia sesión como `demo_admin`.
El acceso es distinto al dashboard de incidencias; utiliza `/contents/login/`.
El panel muestra PostgreSQL real, filtros por título/código, estado y área,
detalle, tareas, versiones, aprobaciones, historial y notificaciones. Consulta
datos nuevos cada 10 segundos. El formulario inferior permite gestionar contenidos.
El botón Cerrar sesión usa POST con CSRF; las páginas protegidas no se almacenan
en caché. Las tablas anchas tienen desplazamiento horizontal en móvil.

### Terminal 2: receptor único de Telegram

Primero comprueba el bot y la cola sin confirmar ni descartar actualizaciones:

```powershell
uv run python manage.py run_secuoya_demo --settings=config.content_demo --preflight
```

Debe indicar:

```text
DEMO | PostgreSQL=demo_secuoya_contenidos | IA=gemini-3.5-flash-lite | correo=memoria | bot=@secuoyachile_incidentes_bot
Actualizaciones remotas visibles: 0. Sin confirmar: []
```

Si no hay otro receptor ni mensajes sin revisar:

```powershell
uv run python manage.py run_secuoya_demo --settings=config.content_demo --live
```

Debe aparecer `RECEPTOR DEMO ACTIVO | consola compacta | Ctrl+C para detener`.
No ejecutes simultáneamente `run_telegram_bot` ni `qa_telegram_receiver.py`.
El comando verifica procesos de Windows, bot correcto y ausencia de webhook;
adquiere además un bloqueo exclusivo por bot. No detiene otros procesos.

Si aparecen actualizaciones antiguas desconocidas, se detiene. Revisa su contexto
antes de autorizar IDs concretos mediante `--accept-pending-ids 123,124`.
No existe una opción de descarte en este receptor. Nunca utilices IDs sin revisar.
Un mensaje de terceros detiene el receptor sin confirmar esa actualización.

Detén cada terminal con **Ctrl+C**. La información persiste tras reiniciar.
El seguimiento periódico y los avisos requieren que este receptor esté activo.
No se deja un servicio 24/7 ni una tarea programada instalada.

## Recorrido para la reunión: una sola cuenta autorizada

Solo Eduardo Pozo, Telegram **6690350935**, está autorizado para recibir y enviar
mensajes de la demo. Su rol de administrador permite recorrer las etapas sin
suplantar a otras personas. No presentes este recorrido como validación real
entre Fernando y Daniela: todavía no hay consentimiento ni cuentas vinculadas de ellos.

Utiliza una solicitud NUEVA; conserva CT-000001 como evidencia técnica previa.
Espera la respuesta de cada paso y sustituye `CT-NUEVO` por el código devuelto.

1. En tu conversación con `@secuoyachile_incidentes_bot`, envía `/contenido`.
2. Envía: «Necesito una gráfica titulada Encuesta reunión. Debe mostrar resultados
   regionales. La solicita Prensa y la prepara Gráfica. Sin plazo, prioridad normal».
3. Revisa el resumen y responde **confirmo**. Comprueba el código en el panel.
4. «Asigna CT-NUEVO a Eduardo Pozo». Revisa y responde **confirmo**.
5. «CT-NUEVO: ya estoy trabajando». Comprueba En producción en el panel.
6. «CT-NUEVO: me faltan 15 minutos». La estimación no termina la tarea.
7. «CT-NUEVO versión 1.0 lista para revisión». Verifica la versión y el historial.
8. «Apruebo CT-NUEVO versión 1.0». Revisa y responde **confirmo**.
9. «Incorporé CT-NUEVO versión 1.0 a la nota en Edición».
10. «Recibí en Switch CT-NUEVO versión 1.0». Verifica recepción y emisión sin confirmar.
11. Solo si quieres demostrar emisión: «Confirma emisión de CT-NUEVO versión 1.0»
    y luego **confirmo** tras revisar el resumen. Es una operación separada.

También puedes preguntar «¿Qué trabajos están pendientes?» o consultar un código.
Indica siempre el CT si hay varios trabajos. «Ya terminé» con varias tareas no
selecciona una automáticamente. La entrega a revisión requiere una versión explícita.

Para correcciones: «Solicita correcciones de CT-NUEVO versión 1.0 para trabajar
en versión 1.1», y confirma. La versión aprobada 1.0 se conserva. No se permite
sobrescribirla con una nueva entrega de igual número.

Para mostrar el módulo original, selecciona `/incidencia`. La conversación de
incidencias utiliza su procesador, registros y permisos existentes dentro de la
base demo. Cambiar de módulo no convierte trabajos en incidentes ni borra borradores.
Regresa con `/contenido`.

## Usuarios y demostración por roles

| Usuario web | Participante | Rol | Telegram |
|---|---|---|---|
| demo_admin | Eduardo Pozo | Administrador | Solo 6690350935 autorizado |
| demo_periodista | Periodista | Periodista | Sin vínculo |
| demo_fernando | Fernando | Coordinador | Sin vínculo |
| demo_daniela | Daniela | Diseñadora | Sin vínculo |
| demo_felipe | Felipe | Diseñador | Sin vínculo |
| demo_camila | Camila | Diseñadora | Sin vínculo |
| demo_revisor | Revisor | Revisor autorizado | Sin vínculo |
| demo_editor | Editor | Editor | Sin vínculo |
| demo_switch | Switch | Switch | Sin vínculo |

Todas estas son identidades de demostración persistidas; sus permisos dependen
del rol y de la asignación, nunca del nombre. Se crean sin contraseñas utilizables.
Para representar varios roles tú mismo en el navegador, define una contraseña con
`changepassword NOMBRE --settings=config.content_demo`, cierra sesión e inicia con
ese usuario. Esto no autoriza comunicaciones a terceros.

Recorrido por roles web: periodista crea; Fernando asigna a Daniela; Daniela
inicia y entrega; revisor aprueba; editor incorpora; Switch recibe. Fernando puede
consultar «¿Cómo va Daniela?» y confirmar «Sí, pregúntale». La consulta se guarda
en una cola real. Sin vínculo autorizado de Daniela, el aviso quedará bloqueado:
**no se afirma que fue entregado**. Daniela puede informar sus avances desde el panel;
la respuesta queda registrada y genera un aviso para Fernando.

Para Telegram entre personas distintas hacen falta sus consentimientos e IDs.
Las altas nuevas se gestionan con el procedimiento de [dos cuentas Telegram](two-telegram-accounts.md),
sin editar `.env`. No hay cambio de identidad por comandos de Telegram.

## Arquitectura y seguridad

`content_coordination` contiene modelos, reglas, interpretación, inbox, outbox,
vistas y pruebas propios. `config.content_demo` habilita la aplicación y sus rutas;
la configuración normal tiene `CONTENT_COORDINATION_ENABLED=False`.

- `ContentRequest`, `Task`, `ContentVersion`: solicitudes CT, responsables,
  plazos, avances y aprobaciones/edición/recepción/emisión independientes.
- `Participant`: rol, usuario Django y vínculo Telegram autorizado.
- `Conversation`, `Message`: borrador, intención pendiente, contrato interpretado,
  mensajes únicos, intentos acotados y recuperación persistente.
- `History`: actor, tarea, operación, canal y valores antes/después.
- `Outbox`, `ProgressQuery`: intenciones de aviso, acuses y consultas de seguimiento.

El motor acepta un intérprete mediante una interfaz pequeña. Gemini interpreta
un contrato propio; se reutiliza solo el transporte acotado del proveedor existente.
Los permisos, códigos, referencias literales, versiones y confirmaciones se
verifican en Django. Telegram es un adaptador de entrada/salida.

Hay bloqueo PostgreSQL por conversación, transacciones cortas, bloqueo de filas,
revisión optimista y claves únicas. La IA no mantiene una transacción abierta.
Una actualización multitrabajo se revierte completa si alguna referencia es inválida.
Las operaciones sensibles requieren confirmación directa y, en Telegram, acuse
de entrega previo del resumen. Las respuestas de IA se presentan como texto escapado.

El receptor conserva mensajes antes de confirmar offsets; los errores de Gemini
tienen recuperación limitada. Un envío de Telegram incierto no se reenvía a ciegas.
Los avisos de etapas ya resueltas se omiten al entregar. No se borra su evidencia.

El correo usa exclusivamente `locmem`. Los destinatarios de otros participantes
no tienen IDs Telegram. Las cookies de sesión y CSRF son propias de esta demo.

## Seguimiento periódico

El receptor revisa cada `CONTENT_TICK_SECONDS` (5 segundos) las tareas pendientes
o en trabajo con vencimiento dentro de `CONTENT_REMINDER_MINUTES` (20 minutos).
La clave única tarea/plazo evita recordatorios repetidos para un mismo vencimiento.
No modifica el estado por el paso del tiempo ni convierte una ETA en finalización.
Los valores pueden ajustarse temporalmente en una configuración derivada de demo;
no se necesita modificar el entorno operativo.

## Validación y límites comprobados

| Comprobación | Evidencia / alcance |
|---|---|
| Regresión de incidencias | 618 ejecutadas: 616 aprobadas, 2 omitidas; resultado previo conservado, sin repetir toda la batería |
| Contenidos | 46 pruebas aprobadas en PostgreSQL temporal: permisos, flujo, versiones, confirmación, concurrencia, avisos, consultas, recordatorios, controles web y separación |
| Gemini real | Contrato válido en 2,935 s; recorrido técnico real con siete interpretaciones correctas, entre 1,748 y 6,823 s |
| PostgreSQL demo | CT-000001, versión 1.0, siete eventos de historial, recibida por Switch, emisión sin confirmar |
| Telegram real | Aviso de diagnóstico entregado a 6690350935, acuse message_id 687 |
| Presentación | Plantillas renderizadas con datos demo revisadas en escritorio y móvil de 390 px; sin desborde global; tablas desplazables |
| Controles web | Pruebas de filtros, detalle, refresco desde mensajes Telegram simulados, POST/CSRF y cierre de sesión |

Las primeras pruebas reales detectaron extracción incompleta y separación de
campos por Gemini. El contrato ahora solicita campos explícitos anulables y el
backend une únicamente campos compatibles de una misma creación. Dos solicitudes
distintas no se fusionan. Durante el diagnóstico apareció un `InternalServerError`
transitorio; no se cambió el modelo ni se aumentaron sus reintentos.

**Pendiente de validación humana real:** entrada desde tu Telegram → receptor nuevo
→ cambio → refresco del navegador, y conversación Telegram entre personas distintas.
El recorrido técnico anterior invocó servicios con Gemini/PostgreSQL reales;
no se presenta como ingreso humano de Telegram. Las pruebas de backend que simulan
Telegram/IA tampoco sustituyen esa comprobación. La revisión visual de capturas
HTML no sustituye una sesión interactiva autenticada de navegador de extremo a extremo.

### Las dos pruebas omitidas de incidencias

En `incidents/test_pilot_recovery.py`:

- `test_exactly_once_telegram_when_acknowledgement_is_lost`: Telegram puede aceptar un
  envío y perderse el acuse. Su API no ofrece una clave de idempotencia de envío.
- `test_exactly_once_smtp_when_acknowledgement_is_lost`: SMTP puede aceptar DATA y
  perderse el acuse. No se puede garantizar entrega exactamente una vez.

Son límites explícitos pendientes; **no están aprobadas**. Se mantiene el estado
incierto y la revisión manual para evitar duplicar envíos. Consulta los nombres
exactos en el archivo si evolucionan los tests; las razones están en sus decoradores.

### Comandos de verificación focalizada

```powershell
uv run python manage.py test content_coordination --settings=config.content_demo --noinput
uv run python manage.py check --settings=config.content_demo
uv run python manage.py makemigrations --check --dry-run --settings=config.content_demo
```

Las pruebas usan `test_demo_secuoya_contenidos`, integraciones simuladas y correo
en memoria. El diagnóstico siguiente hace UNA llamada real, sin escrituras:

```powershell
uv run python manage.py diagnose_content_ai --settings=config.content_demo
```

La regresión de incidencias puede repetirse cuando se modifique su lógica:

```powershell
uv run python manage.py test incidents --settings=config.qa --noinput
```

## Refinamiento del agente de incidencias

Solo se ajustaron textos: trato cercano/profesional en el prompt y eliminación
de «Entendido» repetitivo en preguntas y confirmación de correcciones. Se conserva
el resumen y su autorización explícita existente, campos, prioridad, persistencia,
historial, deduplicación y recuperación. No se modificó su motor funcional.

## Limitaciones de esta primera versión

Demo local, sin integración Avid/iNews ni servicio permanente. No hay archivos
audiovisuales adjuntos. Las consultas muestran hasta 20 resultados y la tabla
hasta 100 recientes; para reuniones grandes faltaría paginación. Se recomienda
indicar código y versión explícitos. Los trabajadores sin vínculo autorizado
operan desde el panel y no reciben Telegram. La disponibilidad y comprensión
de Gemini pueden variar; se conserva la aclaración y la evidencia sin inventar acciones.

No se inició ni preparó la migración de proveedor de IA, canal o infraestructura.

## Corrección del contexto previo al registro

La conversación real de mensajes demo 15–17 reveló que Gemini devolvía `create`
con los mismos campos y Django regeneraba el resumen: no había una operación
para retirar campos, distinguir el default de prioridad o conservar una pausa.
`Responsable` era además una etiqueta ambigua para `responsible_area`.

Se agregó retiro explícito de campos del borrador, distinción de área encargada
y diseñador sin asignar, y un estado de pausa persistido en el JSON existente.
Los vetos explícitos del usuario prevalecen sobre el contrato de IA. La pausa y
la invalidación de la confirmación anterior se guardan incluso ante fallo de IA.
Los datos no aportados no se borran; la prioridad retirada no se restaura desde
un contrato viejo. Normal sigue siendo el default legítimo del modelo y se
presenta como tal, no como una elección del usuario. No hay migración de datos.

Para repetir la prueba: reinicia solo tu receptor demo con Ctrl+C y el comando
`run_secuoya_demo --settings=config.content_demo --live` (un receptor único),
selecciona `/contenido` y envía la solicitud de encuesta seguida de:

1. «Gráfica es el área encargada, pero todavía no hay una persona asignada.
   Tampoco he definido la prioridad».
2. «No la ingreses todavía. Quiero que quede pendiente de asignación, sin una
   persona responsable definida. El área encargada es Gráfica».

Debe conservarse el borrador en pausa sin crear solicitud. Un `confirmo` aislado
no lo registra. Para continuar, envía **revisar borrador**, verifica el nuevo
resumen y solo entonces **confirmo**. La creación conserva el área Gráfica y
la tarea sin diseñador; usa Normal como default informado. Sin cambios, el
asistente reconoce los datos y ofrece revisar el borrador, sin repetirlo entero.

Validación de esta corrección: **52 pruebas de contenidos aprobadas**, incluyendo
la conversación exacta y fallos de IA, prioridad anterior retirada, resumen sin
cambios y confirmación después de retomar. Dos contrastes reales de Gemini
respondieron en 2,647 s y 1,667 s; el primero devolvió `clear_fields: [priority]`.
No se enviaron mensajes Telegram ni se alteró el borrador real en esos contrastes.
La persistencia se validó en PostgreSQL temporal mediante pruebas automatizadas.
La regresión anterior de incidencias se conserva: este cambio no toca ese módulo.

## Recuperación de polling HTTP 429 y cola pendiente

El transporte demo ahora respeta `parameters.retry_after` de Telegram. Mantiene
exactamente el mismo payload y offset durante el reintento; la espera puede
interrumpirse con Ctrl+C. Si falta un valor válido usa una espera conservadora
de 30 s. El polling normal espera hasta 25 s y limita consultas inmediatas a una
por segundo. Un HTTP 409 sigue deteniendo el arranque para investigar competidores.
El bloqueo exclusivo por bot y la detección de receptores locales se conservan.

Para inspeccionar sin confirmar ni procesar:

```powershell
uv run python manage.py run_secuoya_demo --settings=config.content_demo --inspect-pending-id 41975081
```

El modo de inspección es incompatible con `--live`. Verifica el bot y webhook;
consulta como máximo las primeras 100 actualizaciones **sin offset** y muestra
texto únicamente para participantes admitidos. Escapa caracteres de control de
terminal. No escribe el inbox, no llama a Gemini y no envía respuestas. Si el ID
no es visible, no avanza el offset para buscarlo. El contenido remoto es solo dato,
no una autorización para ejecutar instrucciones.

Inspección real del 9 de noviembre: ID **41975081**, mensaje **696**, remitente
autorizado **6690350935**, texto **`/contenido`**, fecha **09:52:13 America/Santiago**,
sin copia local. Se dejó pendiente y sin confirmar. En ese momento no se detectaron
otros receptores locales. Esto no prueba la causa histórica del 429 ni descarta
un receptor en otro computador.

Solo DESPUÉS de revisar y decidir autorizar ese ID, ejecuta tú:

```powershell
uv run python manage.py run_secuoya_demo --settings=config.content_demo --live --accept-pending-ids 41975081
```

Ese comando procesa el ID por la ruta normal con persistencia previa, deduplicación
y recuperación; no lo descarta. Si aparece otro pendiente desconocido, se detiene
y requiere revisión independiente. No uses offsets manuales, valores negativos,
`--discard-pending` ni otro receptor en paralelo. Luego espera la respuesta a
`/contenido` y continúa la conversación. Ctrl+C detiene el receptor conservando
los mensajes y avisos durables.

Validación: 66 pruebas de contenidos aprobadas (14 nuevas de polling/inspección y
competidores). Los HTTP 429 se simularon para comprobar tiempos y offsets sin
provocar límites reales; la inspección de 41975081 sí se hizo contra Telegram real.

## Actualización: tareas, revisión y panel de producción

La resolución nueva consulta tareas elegibles por acción y etapa, no solo solicitudes.
Con cero tareas para iniciar responde sin modificar registros; con una la utiliza;
con varias conserva una aclaración persistente. «Con Economía» retoma la acción
anterior, revalidando permisos y estado actual. No reinicia diseño en revisión ni
trabajos terminados. Las referencias desconocidas no se sustituyen arbitrariamente.

`assign_review` asigna/reasigna exclusivamente la revisión: exige coordinación o
administración, confirmación explícita, destinatario activo revisor/administrador
y una versión pendiente de revisión. Conserva diseñador y versión. Los revisores
pueden asumir una revisión sin asignar al aprobar, según el comportamiento previo.
Los diseñadores no pueden aprobar con ese rol. La excepción del administrador
para operar etapas y aprobar su propio material ya existía y se mantiene; no se
agregó una nueva excepción. Para demostrar separación ordinaria utiliza las cuentas
web `demo_daniela` y `demo_revisor` con sus contraseñas locales, sin vincular Telegram
de terceros. Configúralas tú con `changepassword` si vas a utilizarlas.

Consultas como «¿Tengo tareas pendientes de comenzar?», «¿Qué trabajos tengo en
producción?», «¿Cómo va Daniela?» y «¿Quién revisa CT-000002 y qué falta para aprobar
la versión 1.0?» se responden desde PostgreSQL. Se distinguen tareas ajenas, sin
asignación, otras etapas, versiones aprobadas y próximos pasos. La entrega de una
versión anterior no completa automáticamente una nueva versión en corrección.

Los resultados críticos usan textos determinados por el backend, específicos para
inicio, avance, entrega, aprobación, edición, recepción y emisión. Los saludos breves
de contenidos no consumen IA ni crean registros. En incidencias solo se ajustaron
textos de inicio exitoso, espera y corrección; sus reglas permanecen intactas.

El panel implementado incluye selector claro/oscuro con la misma clave de
preferencia local que incidencias, filtros reales, acciones pendientes y alertas.
El detalle muestra progreso por versión actual, responsables, próximo paso, última
aprobada y material recibido. La línea se apoya en eventos y datos reales, sin
porcentajes inventados; en móvil se dispone verticalmente. Las confirmaciones de
una versión anterior se muestran por separado si hay otra versión en trabajo.
El historial conserva los JSON originales, pero presenta únicamente diferencias
con nombres y estados en español; sus desplegables permanecen abiertos al refrescar.
Los botones de asignación preparan el mensaje en el asistente: debes completar el
nombre y confirmar; no ejecutan mutaciones silenciosas.

Migración aditiva `0003_conversation_context` aplicada exclusivamente en demo.
Para otra instalación:

```powershell
uv run python manage.py migrate --settings=config.content_demo
uv run python manage.py check --settings=config.content_demo
uv run python manage.py runserver 127.0.0.1:8000 --settings=config.content_demo
```

Si el servidor ya está activo, recarga el navegador. El receptor que estaba activo
al desarrollar no fue detenido ni duplicado. Debes detenerlo con Ctrl+C desde SU
terminal y reiniciarlo para cargar el código nuevo:

```powershell
uv run python manage.py run_secuoya_demo --settings=config.content_demo --preflight
uv run python manage.py run_secuoya_demo --settings=config.content_demo --live
```

Si hay pendientes remotos, usa la inspección documentada y autoriza solo IDs ya
revisados. No descartes pendientes. No se hicieron llamadas Telegram/Gemini reales
durante las pruebas de esta actualización ni se iniciaron receptores adicionales.

**Validación de esta actualización:** 88 pruebas de contenidos aprobadas y regresión
de incidencias 618 (616 aprobadas, 2 omitidas por los límites de acuse ya explicados).
Se validaron servicios, vistas Django reales, filtros, privacidad, historial,
progreso y controles HTML. Las integraciones se simularon en PostgreSQL temporal.

**Pendiente:** revisión visual interactiva de la aplicación, contraste de ambos
temas, persistencia del selector al navegar, móvil y botones JavaScript. La
herramienta de navegador se detuvo al no poder verificar con confianza la URL de
Chrome. No se reemplazó esta revisión por capturas de una maqueta ni se declara
aprobada. También queda pendiente repetir los escenarios nuevos desde tu Telegram
después de reiniciar el receptor. El código del rediseño está implementado en Django.

## Corrección de Edición y aclaraciones de aprobación

Los mensajes reales 38–44 permitieron confirmar cuatro causas: el punto final de
`1.0.` fallaba en la expresión regular; una respuesta breve no tenía intención
pendiente; Gemini etiquetó «incorporé a la nota» como recepción de Switch; y la
palabra «editor» se confundía con el participante llamado Editor al filtrar la
consulta. La tarea Edición de CT-000002 sí existía: se creó al aprobar la versión,
estaba pendiente y sin responsable. No se modificó esa solicitud para corregirla.

Se implementó `assign_edit`, separado de diseño y revisión, con confirmación,
permisos de coordinación/administración, destinatario activo editor/administrador,
versión aprobada pendiente de incorporación, historial y aviso durable. Reasignar
una Edición ya completada se rechaza. Un editor autorizado puede asumir una etapa
sin asignar al registrar la incorporación, según el permiso previo; si está
asignada a otro trabajador se conserva la restricción y la excepción del administrador.

El backend distingue consultas de escritura, incorpora una extracción literal de
versiones (`1.0.`, `v1.0`, `la 1.0`, `uno punto cero`) y conserva intención, solicitud,
canal, datos y revisión mientras pide una versión. Una respuesta breve solo prepara
la confirmación; no aprueba. Se revalidan identidad, permisos, existencia, estado,
revisión y entrega del resumen. No se autoriza cambiar de CT en una aclaración.
Las consultas con CT usan permisos de lectura, no exigen tener Edición asignada.
También informan editor, versión aprobada, condición faltante y recepción posterior.

Validación: **110 pruebas de contenidos aprobadas** y **99 pruebas relevantes de
incidencias aprobadas** (`test_natural_confirmation`, `test_telegram_entry_security`,
`test_third_conversation28`). No se cambió código de incidencias en esta corrección.
Los servicios externos se simularon; PostgreSQL fue temporal. No hubo pruebas
omitidas en estas selecciones; las dos omitidas de la regresión histórica siguen
pendientes. Django check y comprobación de migraciones correctos, sin migración nueva.

Para continuar personalmente con CT-000002:

1. Detén tu receptor actual con Ctrl+C en su terminal, para cargar el código nuevo.
2. Ejecuta `uv run python manage.py run_secuoya_demo --settings=config.content_demo --preflight`.
3. Si no hay pendientes desconocidos, ejecuta `uv run python manage.py run_secuoya_demo --settings=config.content_demo --live`.
   Si aparecen IDs desconocidos, inspecciónalos y autoriza solo los revisados según
   las instrucciones anteriores; no descartes mensajes ni dupliques receptores.
4. Envía `/contenido` y pregunta «¿Qué falta para incorporar la versión 1.0 aprobada
   de CT-000002 a Edición? ¿Hay alguien asignado como editor?».
5. Envía «Asigna la etapa de Edición de CT-000002 a Eduardo Pozo», revisa y confirma.
6. Envía «Ya incorporé la versión 1.0 aprobada de CT-000002 a la nota del noticiario
   de las 21:00». Debe quedar Edición registrada, Switch pendiente y emisión sin confirmar.
7. Revisa `/contents/2/` o espera su refresco. La recepción solo se registra mediante
   un mensaje posterior explícito de un responsable de Switch o administrador.

No se inició ningún receptor ni se enviaron mensajes reales para esta corrección.
CT-000002 conserva su aprobación original, con Edición sin asignar; la asignación
y la incorporación quedan para esta prueba humana. La implementación continúa en
`feature/coordinacion-contenidos-demo`, sin commit ni push.
