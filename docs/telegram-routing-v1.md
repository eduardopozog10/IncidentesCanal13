# Enrutamiento Telegram v1 — validación y habilitación demo

Implementación sobre `feature/coordinacion-contenidos-demo`, referencia `f8390dc`.
No se llamó a Gemini, Telegram ni SMTP, no se consultaron bases reales y no se
aplicó la migración en `demo_secuoya_contenidos`. No hubo commit, push o merge.

## Arquitectura y comportamiento

`run_secuoya_demo` conserva un solo polling y verifica el bot y la ausencia de
webhook. `telegram_routing` decide el destino sin IA. La sesión pertenece al ID
del bot, remitente y chat privado. Cada `update_id` tiene una recepción única
por bot, con destino e identidad inmutables y referencia al inbox original.

La transacción guarda recepción + inbox antes de avanzar el offset. Los
selectores se aplican al recibirlos, por lo que el texto posterior del mismo
lote utiliza la selección nueva. Un replay no vuelve a aplicar un selector.

| Entrada | Resultado |
|---|---|
| `/incidencias`, `/incidencia` | Selecciona Incidencias solo con TechnicianChannel y reportante activos. |
| `/contenidos`, `/contenido` | Selecciona Contenidos solo con participante, consentimiento y verificación habilitados. |
| `/modulo` | Muestra selección y opciones autorizadas. |
| `/start` | Muestra opciones sin borrar contexto. Mantiene la verificación privada de un consentimiento nuevo. |
| `/cancelar` en Incidencias | Ejecuta la cancelación original; admite el sufijo del bot y conserva el texto recibido. |
| Otro comando o selector con argumentos | Respuesta de ayuda sin IA ni operación de negocio. |
| Texto sin selección | Solicita seleccionar; no infiere intención. |

No se modifica ninguna identidad al seleccionar. La transferencia
`bdf62dea-4d35-474e-8fad-5cebf91215b8` no se restaura ni se vuelve a crear.
Fernando conserva su rol en Contenidos; Incidencias resuelve independientemente
al reportante vinculado a 6690350935. Tener una identidad no autoriza el otro módulo.

Los borradores, operaciones y aclaraciones no se borran al alternar. Una
confirmación anterior al cambio requiere un resumen nuevo entregado después
de seleccionar o retomar el módulo, incluida la primera selección tras instalar
el router. Los permisos, asignaciones, revisiones y confirmaciones
siguen controlados por los servicios Django existentes.

Incidencias conserva su agrupación, originales, resultados cacheados y
recuperación. El adaptador limita el worker a sus recepciones autorizadas.
Contenidos conserva Processor, inbox, outbox, consentimiento y controles de
versiones. Los avisos salientes nunca seleccionan un módulo.

Los controles reutilizan la entrega durable de ChannelMessage, con propósito
`routing`; no son mensajes de negocio ni entradas recuperables del agente.
Una entrega incierta no se reenvía automáticamente. `status=done` del recibo
significa control terminado, no necesariamente mensaje entregado: consultar
`reason` y el mensaje saliente vinculado cuando hubo error.

El bloqueo local por token es compartido con `run_telegram_bot` y su script QA.
Se mantiene el chequeo de procesos Windows; no se detienen procesos. El bloqueo
local no sustituye la coordinación entre computadores; un 409 remoto detiene el
polling sin descartar actualizaciones.

## Estados que bloquean el arranque o el despacho

- Migración ausente: detener antes de consultar Telegram.
- Actualización desconocida en cola remota: inspeccionar y revisar antes de aceptar su ID.
- Inbox histórico recuperable sin recepción del router: detener, sin convertir,
  adivinar ni reprocesar el mensaje. No existe conversión automática de históricos.
- Selector mientras hay una entrada de negocio pendiente: detener y conservarla.
  Esperar la respuesta antes de alternar módulos durante la demostración.
- Actualización nueva fuera de orden, identidad cambiada o autorización revocada:
  recepción bloqueada con motivo registrado, sin despacho a otro módulo.

Una recepción bloqueada exige revisión técnica explícita. `--accept-pending-ids`
solo permite admitir IDs remotos previamente inspeccionados: no elimina bloqueos
locales, no cambia destinos y no autoriza reprocesamientos. No borrar filas ni
editar destinos para superar un bloqueo. No usar `--discard-pending`.

## Validación offline

Desde PowerShell en la raíz del repositorio:

```powershell
uv run --no-sync python manage.py check --settings=config.telegram_routing_tests
uv run --no-sync python manage.py test telegram_routing content_coordination incidents.test_telegram_entry_security incidents.test_telegram_groups incidents.test_pilot_recovery incidents.test_recovery_limits incidents.test_operational_regressions incidents.conversational_tests --settings=config.telegram_routing_tests --noinput
uv run --no-sync python manage.py test incidents.test_agent_evaluation --settings=config.telegram_routing_tests --noinput
```

Esta configuración usa `test_secuoya_telegram_routing`, con nombre de conexión
normal deliberadamente inexistente. DiscoverRunner crea y elimina únicamente
la base temporal. Se utiliza un token ficticio, proveedor simulado y correo en
memoria. El runner bloquea HTTP real y SMTP; permite los MockTransport de pruebas.
No ejecutar estos comandos con settings operativos ni de demo.

Las pruebas cubren permisos independientes, verificación, transferencias,
borradores simultáneos, barreras de confirmación, comandos, lotes, duplicados,
concurrencia, orden, interrupciones atómicas, reinicio, recuperación, avisos,
worker con hilos y el receptor combinado completo con transporte simulado.

Evidencias locales ignoradas por Git:

- `.qa/telegram-routing-baseline.log`: 379 pruebas; 3 incidencias por token vacío
  de la configuración aislada (no defectos productivos), 2 omisiones conocidas.
- `.qa/telegram-routing-baseline-config.log`: 6 comprobaciones de esa configuración
  con token ficticio, aprobadas; incluye las tres que no podían ejecutarse antes.
- `.qa/telegram-routing-new.log`: primeras 37 pruebas nuevas aprobadas.
- `.qa/telegram-routing-candidate.log`: 429 pruebas, 427 aprobadas y 2 omitidas.
- `.qa/telegram-routing-final.log`: 150 pruebas posteriores, todas aprobadas.
- `.qa/telegram-routing-acceptance.log`: 97 aprobadas: 56 del router (incluida
  migración sobre datos sintéticos) y 41 del corpus/evaluador congelado.
- `.qa/telegram-routing-acceptance-final.log`: 57 pruebas finales del router
  aprobadas, incluida la barrera para resúmenes previos a su instalación.

Las ejecuciones se solapan; no se suman como si fueran casos distintos.
`manage.py check` pasó con settings normales y con los settings aislados.
La comprobación de migraciones no detectó cambios adicionales; Django advierte
que la conexión normal aislada no existe, como se ha configurado deliberadamente.

Las dos omisiones existentes corresponden a entrega exactamente una vez cuando
Telegram o SMTP aceptan un envío y se pierde el acuse. Siguen omitidas: se conserva
el tratamiento de estado incierto y revisión manual. Ningún resultado demuestra
disponibilidad, comprensión o latencia reales de Gemini. No se aplicó estadística
de ensayos variables a estas regresiones deterministas ni se ejecutaron scripts
de terceros de las skills.

## Archivos

Nuevos: `telegram_routing/__init__.py`, `apps.py`, `authorization.py`, `models.py`,
`receiver_lock.py`, `router.py`, `tests.py`, `migrations/__init__.py` y
`migrations/0001_initial.py`; `config/offline_test_runner.py`,
`config/telegram_routing_tests.py` y este documento.

Modificados: `config/content_demo.py`;
`content_coordination/management/commands/run_secuoya_demo.py`, `processor.py`,
`telegram_polling.py`, `test_link_transfer.py`, `test_telegram_polling.py`;
`incidents/management/commands/run_telegram_bot.py` y
`incidents/services/telegram_message_groups.py`.

No se modificaron prompts, contratos IA, proveedores, permisos de negocio,
modelos existentes, transferencia real, CT-000005 real ni `.env`.

## Migración demo: procedimiento manual, aún no ejecutado

La migración `telegram_routing/0001_initial.py` crea solo dos tablas e índices.
No tiene RunPython, RunSQL ni alteraciones de tablas existentes. Sus dependencias
son las migraciones actuales de Incidencias y Contenidos. La app se habilita solo
en `config.content_demo` y en la configuración aislada de pruebas.

1. Detener el receptor desde su terminal con Ctrl+C. Identificar cualquier otro
   receptor antes de detenerlo; no matar procesos desconocidos. No usar el receptor
   normal para esta demostración.
2. Crear un respaldo completo con `pg_dump`, comprobar que `pg_restore --list`
   puede leerlo y conservarlo fuera del repositorio si se comparte el proyecto.
   Los comandos siguientes guardan el archivo local bajo `.qa/`, ignorado por Git.

```powershell
$demoBackup = Join-Path (Resolve-Path .qa).Path ("demo_secuoya_contenidos_" + (Get-Date -Format 'yyyyMMdd_HHmmss') + '.dump')
@'
import os, shutil, subprocess, sys
from pathlib import Path
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.content_demo'
import django
django.setup()
from django.conf import settings
db = settings.DATABASES['default']
assert db['NAME'] == 'demo_secuoya_contenidos'
assert settings.EMAIL_BACKEND == 'django.core.mail.backends.locmem.EmailBackend'
program = shutil.which('pg_dump')
if not program:
    candidates = list((Path(os.environ['ProgramFiles']) / 'PostgreSQL').glob('*/bin/pg_dump.exe'))
    if len(candidates) != 1:
        raise SystemExit('Configura pg_dump de la versión de PostgreSQL local en PATH; no se hizo respaldo.')
    program = str(candidates[0])
restore = str(Path(program).with_name('pg_restore.exe' if os.name == 'nt' else 'pg_restore'))
environment = dict(os.environ)
environment['PGPASSWORD'] = str(db.get('PASSWORD') or '')
if Path(sys.argv[1]).exists():
    raise SystemExit('El respaldo ya existe; elige otro nombre. No se sobrescribió.')
subprocess.run([program, '--format=custom', '--no-owner', '--no-acl', '--file', sys.argv[1],
    '--host', str(db['HOST']), '--port', str(db['PORT']), '--username', str(db['USER']),
    '--dbname', 'demo_secuoya_contenidos'], env=environment, check=True)
subprocess.run([restore, '--list', sys.argv[1]], stdout=subprocess.DEVNULL, check=True)
print('Respaldo legible; credenciales no impresas.')
'@ | uv run --no-sync python - $demoBackup
if ($LASTEXITCODE -ne 0) { throw 'No continuar sin un respaldo válido.' }
Get-FileHash -LiteralPath $demoBackup -Algorithm SHA256
```

3. Obtener una huella de los datos protegidos antes de migrar. Esta función es
   exclusivamente de lectura; ejecutarla con el receptor detenido. La salida solo
   contiene cantidades y hashes, sin textos ni credenciales.

```powershell
function Get-DemoFingerprint {
@'
import os, json, hashlib
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.content_demo'
import django
django.setup()
from django.apps import apps
from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection, transaction
assert settings.DATABASES['default']['NAME'] == 'demo_secuoya_contenidos'
labels = ['auth.User', 'auth.Group', 'incidents.Technician', 'incidents.TechnicianChannel',
 'incidents.Incident', 'incidents.IncidentDraft', 'incidents.ChannelMessage',
 'incidents.IncidentChange', 'incidents.IncidentNotification',
 'content_coordination.Participant', 'content_coordination.TelegramConsent',
 'content_coordination.TelegramLinkTransfer', 'content_coordination.ContentRequest',
 'content_coordination.Task', 'content_coordination.ContentVersion', 'content_coordination.History',
 'content_coordination.Conversation', 'content_coordination.Message', 'content_coordination.Outbox',
 'content_coordination.ProgressQuery', 'content_coordination.ParticipantAccessEvent']
result = {}
with transaction.atomic():
    with connection.cursor() as cursor:
        cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
    for label in labels:
        rows = list(apps.get_model(label).objects.order_by('pk').values())
        encoded = json.dumps(rows, cls=DjangoJSONEncoder, sort_keys=True).encode()
        result[label] = {'count': len(rows), 'sha256': hashlib.sha256(encoded).hexdigest()}
print(json.dumps(result, sort_keys=True))
'@ | uv run --no-sync python -
if ($LASTEXITCODE -ne 0) { throw 'No se pudo obtener la huella de datos.' }
}
$beforeFingerprint = Get-DemoFingerprint
$beforeFingerprint | Set-Content -LiteralPath .qa/telegram-routing-demo-before.json -Encoding utf8
```

4. Revisar el plan. Debe incluir únicamente `telegram_routing.0001_initial`.
   Si aparecen migraciones pendientes de otros módulos, detenerse y revisar.

```powershell
uv run --no-sync python manage.py migrate telegram_routing 0001 --plan --settings=config.content_demo
```

5. Solo después de revisar respaldo y plan, aplicar la migración:

```powershell
uv run --no-sync python manage.py migrate telegram_routing 0001 --settings=config.content_demo --noinput
if ($LASTEXITCODE -ne 0) { throw 'Migración no completada: no iniciar el receptor.' }
$afterFingerprint = Get-DemoFingerprint
$afterFingerprint | Set-Content -LiteralPath .qa/telegram-routing-demo-after.json -Encoding utf8
if (($beforeFingerprint -join "`n") -cne ($afterFingerprint -join "`n")) { throw 'Datos protegidos cambiaron: revisar antes de iniciar.' }
uv run --no-sync python manage.py check --settings=config.content_demo
```

Esto permite comprobar también la conservación real de la transferencia, CT-000005,
usuarios, tareas, versiones, permisos del participante, conversaciones y avisos.
Durante la implementación solo se verificaron fixtures equivalentes en tests:
no se afirma haber comprobado el contenido actual de esas filas reales.

No revertir la migración ni restaurar el dump encima de la demo automáticamente:
una reversión elimina el nuevo registro de enrutamiento. Ante un problema, detener
el receptor y conservar ambas fuentes para una recuperación revisada.

## Prueba real de comandos sin Gemini: posterior y manual

No iniciar esta prueba antes de completar la migración y revisar las colas.
Estos comandos realizan llamadas a Telegram cuando el operador los ejecuta;
no se ejecutaron durante la implementación.

```powershell
uv run --no-sync python manage.py run_secuoya_demo --settings=config.content_demo --preflight
```

Ante IDs desconocidos, inspeccionar cada uno, sin avanzar offsets:

```powershell
uv run --no-sync python manage.py run_secuoya_demo --settings=config.content_demo --inspect-pending-id ID_REVISADO
```

No sustituir `ID_REVISADO` sin revisar remitente, fecha, texto, vínculo y estado.
No aceptar históricos de negocio para una prueba limitada a comandos.

Con las comprobaciones correctas y sin pendientes ambiguos:

```powershell
uv run --no-sync python manage.py run_secuoya_demo --settings=config.content_demo --live --commands-only
```

La terminal debe mostrar `PostgreSQL=demo_secuoya_contenidos`, `correo=memoria`,
el bot correcto y `ROUTER | comandos solamente, sin IA ni avisos históricos`.
Desde la cuenta autorizada enviar, uno por uno y esperando cada respuesta:

```text
/start
/modulo
/incidencias
/modulo
/contenidos
/modulo
/incidencia
/contenido
/comando_desconocido
```

No enviar descripciones, confirmaciones o `/cancelar` en esta fase: son entradas de
negocio y el modo limitado las bloquea y conserva. No usar cuentas de terceros.
Ctrl+C detiene el receptor; no se inicia ningún proceso de fondo.

Para comprobar los estados locales, solo lectura:

```powershell
uv run --no-sync python manage.py shell --settings=config.content_demo -c "from telegram_routing.models import RoutingSession,TelegramReceipt; print(list(RoutingSession.objects.values('bot_id','sender_id','chat_id','module'))); print(list(TelegramReceipt.objects.order_by('-pk').values('update_id','destination','status','reason')[:20]))"
```

Recomendación: habilitar primero esta prueba limitada, condicionada a respaldo,
migración, huellas y revisión de pendientes. Mantener bloqueadas las pruebas de
negocio con IA hasta una autorización posterior y comprobación humana de cuotas
y facturación. El modo normal sin `--commands-only` sí puede invocar Gemini y
entregar avisos pendientes autorizados; no usarlo para comprobar ausencia de cargos.
