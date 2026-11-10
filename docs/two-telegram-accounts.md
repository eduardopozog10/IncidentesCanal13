# Preparación de dos cuentas Telegram — demo Secuoya

Para que Eduardo represente temporalmente a Fernando sin borrar al administrador,
utiliza la [transferencia temporal auditada](temporary-telegram-transfer.md).
No intentes hacerlo con `register` ni con cambios manuales de IDs.

Rama: `feature/coordinacion-contenidos-demo`. Base: `demo_secuoya_contenidos`.
Este procedimiento no requiere cambiar `.env`, credenciales ni la base operativa.
Estado comprobado: Eduardo/admin conserva 6690350935; Fernando/coordinator no
tiene vínculo; Daniela/designer tiene 6578528722 autorizado y verificado.
Los pasos de registro siguientes son una referencia para altas nuevas: no los
repitas sobre esas cuentas para realizar la transferencia temporal.

## 1. Obtener consentimiento y preparar el entorno

Acuerda con cada participante su ID personal real y su consentimiento para recibir
asignaciones, recordatorios y consultas autorizadas de esta demostración. No uses
el ID de un grupo ni el nombre de usuario de Telegram como sustituto del ID numérico.
Nunca ejecutes el alta con IDs de ejemplo. Los personajes Fernando y Daniela son
identidades de demostración; no prueban el consentimiento de una persona real.

Si el receptor ya está activo, detén **solo ese receptor** con Ctrl+C en su terminal.
No ejecutes dos procesos de polling del mismo bot.

```powershell
Set-Location 'C:\Users\eduar\OneDrive\Documentos\GitHub\IncidentesCanal13'
uv run python manage.py migrate --settings=config.content_demo
uv run python manage.py check --settings=config.content_demo
```

Las migraciones nuevas son aditivas: consentimiento/auditoría de acceso y acuses
de partes de mensajes. Ya se aplicaron en esta instalación, sin alterar solicitudes.

## 2. Registrar las dos cuentas, solo cuando sus titulares autoricen

Los usuarios de demostración `demo_fernando` (coordinador) y `demo_daniela`
(diseñadora) ya existen. El comando rechaza sobrescribir otro rol o vínculo.
Para otra identidad utiliza un participante nuevo; no recicla IDs silenciosamente.

Ejecuta lo siguiente **solo tras obtener ambos consentimientos**:

```powershell
$idFernando = Read-Host 'ID personal real autorizado de Fernando'
$idDisenador = Read-Host 'ID personal real autorizado del diseñador'
uv run python manage.py content_participant register --settings=config.content_demo --username demo_fernando --role coordinator --telegram-id $idFernando --consent-confirmed
uv run python manage.py content_participant register --settings=config.content_demo --username demo_daniela --role designer --telegram-id $idDisenador --consent-confirmed
```

Si necesitas un participante nuevo, proporciona además `--name` y un `--username`
disponible. Se crea sin contraseña web utilizable. El rol de este comando está
limitado a coordinador/diseñador; no concede administración ni permisos de incidencias.
El alta no envía mensajes, no activa avisos anteriores y no confirma conversaciones
pendientes. Conserva borradores e historial.

## 3. Verificar identidad mediante Telegram privado

Cada persona debe abrir `@secuoyachile_incidentes_bot`, pulsar Iniciar y enviar
**`/start` desde su propia cuenta**. Hasta que ese mensaje privado se persista,
la cuenta nueva no puede recibir avisos. No se verifica identidad con JSON externo.

Con el receptor aún detenido, inspecciona la cola:

```powershell
uv run python manage.py run_secuoya_demo --settings=config.content_demo --preflight
uv run python manage.py run_secuoya_demo --settings=config.content_demo --inspect-pending-id ID_ACTUALIZACION
```

Sustituye `ID_ACTUALIZACION` por cada ID mostrado. La inspección no confirma ni
procesa mensajes. Revisa que cada actualización sea el `/start` de la cuenta
consentida. Solo entonces autoriza **los IDs revisados**:

```powershell
uv run python manage.py run_secuoya_demo --settings=config.content_demo --live --accept-pending-ids ID_REVISADO_A,ID_REVISADO_B
```

No copies esos marcadores literalmente. Si no hay pendientes remotos, utiliza
simplemente `--live`. No utilices `--discard-pending` ni offsets manuales.
El receptor mantiene las protecciones de instancia única, HTTP 429 y recuperación.

Para consultar la configuración local desde otra terminal:

```powershell
uv run python manage.py content_participant show --settings=config.content_demo --username demo_fernando
uv run python manage.py content_participant show --settings=config.content_demo --username demo_daniela
```

`verified` indica que se persistió su `/start`; `delivery_eligible` indica permiso
actual para intentar envíos. `last_delivery` muestra un acuse real ya guardado.
Solo un acuse confirma una entrega; una cuenta verificada puede bloquear el bot
después, por lo que no se promete disponibilidad futura.

La autorización histórica de Eduardo Pozo (6690350935) se conserva. Las altas nuevas
usan consentimiento persistido, no una ampliación manual de la lista en `.env`.

## 4. Servidor y recorrido de demostración

Si Django no está activo:

```powershell
uv run python manage.py runserver 127.0.0.1:8000 --settings=config.content_demo
```

Accede a `http://127.0.0.1:8000/contents/` con tu usuario web. Si ya está activo,
recarga el navegador. Ambos participantes deben seleccionar `/contenido` en sus chats.

1. Crea una solicitud NUEVA desde un periodista/administrador autorizado o tu cuenta
   administradora existente. Conserva CT-000002 como evidencia de emisión.
2. Fernando consulta el nuevo código y escribe «Asigna la gráfica de Economía a
   Daniela» (usa el título real; si hay ambigüedad, indica el CT).
3. Fernando revisa el resumen y confirma. Recibe una confirmación; Daniela recibe
   un aviso individual con trabajo, etapa, plazo e indicación para comenzar.
4. Daniela escribe «Ya puedo comenzar». Con una tarea elegible se resuelve sola;
   con varias se pide aclaración. «Con Economía» conserva la acción pendiente.
5. Fernando pregunta «¿Cómo va Daniela?». Esto **no envía una consulta a Daniela**.
6. Para consultar una tarea específica: «¿Cómo va Daniela con Deportes?». Si el
   sistema ofrece preguntarle, Fernando puede autorizar con «Sí, pregúntale».
7. Daniela responde «Me faltan 15 minutos». Solo una consulta entregada e inequívoca
   permite asociarla a una tarea; si hay varias posibilidades se pide aclaración.
   La ETA no marca la tarea terminada. Fernando ve el avance al consultar de nuevo.
8. Comprueba el último evento global en el dashboard y en Telegram. El avance de
   diseño permanece en su tarea; aprobación, Edición, Switch y emisión son eventos
   distintos. La confirmación de emisión nunca se deriva de la recepción.

Cada cuenta tiene sus borradores, confirmaciones y aclaraciones. El diseñador no
puede consultar/modificar tareas ajenas ni aprobar con ese rol. El coordinador no
se convierte en diseñador o revisor por conversar. No se suplantan identidades.

## 5. Revocar al terminar

```powershell
uv run python manage.py content_participant revoke --settings=config.content_demo --username demo_fernando
uv run python manage.py content_participant revoke --settings=config.content_demo --username demo_daniela
```

Bloquea nuevas entradas, envíos pendientes y ejecución de mensajes ya en cola.
Invalida confirmaciones y aclaraciones antiguas; conserva borradores, tareas,
historial y acuses. No cambia los permisos web ni elimina registros. No se puede
retirar un envío que Telegram ya hubiera aceptado. Para reactivar la misma identidad
se exige consentimiento nuevo y otro `/start`; los avisos bloqueados no se reactivan.

## Presentación y comprobaciones

El renderizador compartido escapa datos y genera únicamente negritas permitidas.
No modifica los hechos ni el texto canónico de confirmación guardado. Limita emojis
decorativos al inicio de líneas; conserva símbolos literales de nombres y datos.
Solo un rechazo explícito de formato permite fallback plano. Ante timeout o entrega
incierta no reenvía a ciegas. Los mensajes largos guardan acuse por parte y retoman
solo partes no confirmadas ante un rechazo reintentable.

La autoasignación a la misma identidad autorizada conserva un aviso auditado como
suprimido y la confirmación de la operación; no duplica el envío. Las asignaciones
entre cuentas distintas, consultas y alertas importantes mantienen sus avisos.

La vista previa sintética está en `.qa/message-preview/index.html`. Se revisó en
navegador a tamaño de escritorio y móvil de 390 px: negritas, espacios, símbolos,
títulos largos y ausencia de desborde. No es una captura de Telegram ni prueba de
entrega real. Queda pendiente verificar desde las dos cuentas el formato nativo,
la emisión persistida de CT-000002 en el dashboard y el recorrido humano completo.

Pruebas: formato/fallback, acuses, consentimiento/revocación, contextos separados,
consultas autorizadas por tarea, concurrencia, último evento por versión, regresión
de contenidos e incidencias. Todas las identidades nuevas de pruebas son sintéticas
y viven en PostgreSQL temporal. No se enviaron mensajes reales en esta intervención.

Resultados: **136 pruebas aprobadas** en la selección de contenidos y presentación
compartida; regresión de incidencias **618: 616 aprobadas y 2 omitidas**. Las omitidas
siguen siendo los límites de acuse de Telegram/SMTP; no se cuentan como aprobadas.
Django check y comprobación de migraciones correctos. Dos aserciones anteriores de
presentación ahora verifican la aprobación sin exigir que se repita una segunda
vez como «Última aprobada»; las validaciones funcionales y de seguridad se conservan.
