# Registro seguro del revisor por Telegram — demo Secuoya

Implementado sin registrar la cuenta real ni liberar avisos reales.
Settings: `config.content_demo`. Base: `demo_secuoya_contenidos`.

## Causa y alcance

`Participant.Role` ya incluye `reviewer`. El receptor admite participantes activos
con consentimiento y Telegram verificado, sin una lista de roles de dos entradas.
Las operaciones de revisión ya pasan por `stage_task`, roles, asignación, versión,
revisión de datos vigente y confirmación explícita del resumen entregado.

El rechazo estaba exclusivamente en `content_participant --role`, cuya lista del
piloto contenía solo `coordinator` y `designer`. Ahora admite también `reviewer`,
con la misma lista comprobada en el backend del comando. No se cambia el rol de
un participante existente ni se habilitan `journalist`, `editor`, `switch` o `admin`.

El alta no otorga la verificación: registra consentimiento y deja `verified_at`
vacío. Usa el límite temporal de vínculo existente para exigir un `/start`
privado **posterior** al consentimiento. Repetir un alta idéntica conserva la
verificación/auditoría y no reactiva avisos. Una revocación conserva el historial;
otro consentimiento exige una nueva prueba de identidad.

La unicidad de ID en participantes y de consentimientos activos sigue protegida
por PostgreSQL; los conflictos concurrentes se revierten completos y se informan.
Los IDs con ceros iniciales se rechazan, evitando dos representaciones del mismo
identificador. No se reactivan participantes o usuarios Django inactivos.

## Estado real inspeccionado, sin cambios

- `demo_revisor`: Revisor, `reviewer`, activo, sin ID Telegram, no autorizado,
  sin consentimiento ni verificación registrados.
- CT-000005: revisión 5, versión 1.0 pendiente de revisión, asignada al Revisor;
  todavía sin aprobación, edición, recepción ni emisión.
- Aviso 119, `review:3:7`: anuncio de versión disponible, bloqueado por
  `RecipientNotAuthorized`, cero intentos y sin acuse.
- Aviso 126, `assign_review:5:4`: asignación de revisión, mismo bloqueo, cero
  intentos y sin acuse. Es el aviso específico que puede revisarse para liberar.
- Fernando y Daniela conservan sus vínculos. La transferencia
  `bdf62dea-4d35-474e-8fad-5cebf91215b8` sigue activa y no se restaura.

Los avisos 119 y 126 tienen propósitos diferentes. No se borran ni se deduce que
son duplicados. El alta y `/start` no los liberan automáticamente. Para continuar
no hace falta reasignar el trabajo ni aprobarlo para forzar una notificación.

## Archivos

- `content_coordination/management/commands/content_participant.py`: lista cerrada
  de roles, consentimiento/alta segura, consulta de avisos de revisión y liberación
  explícita de un ID. `show` incluye el límite temporal y estado activo.
- `content_coordination/review_notices.py`: inspección y liberación transaccional
  del aviso específico de asignación nunca enviado; auditoría e idempotencia.
- `content_coordination/test_reviewer_registration.py`: registro, consentimiento,
  `/start`, permisos, revocación, entrega, bloqueo fuera de demo y concurrencia.
- Este documento: procedimiento manual y evaluación de otros roles.

No se cambiaron modelos, migraciones, reglas de revisión ni el receptor. No hay
nuevas dependencias ni cambios de `.env`, producción o Incidencias.

## Procedimiento manual

Estas operaciones NO se ejecutaron sobre las cuentas reales. Deben realizarse
después de revisar el resultado y obtener consentimiento del titular de la cuenta.

### 1. Detener solo el receptor actual y comprobar el participante

Pulsa Ctrl+C en su terminal; no abras un segundo receptor. En PowerShell:

```powershell
Set-Location 'C:\Users\eduar\OneDrive\Documentos\GitHub\IncidentesCanal13'
uv run python manage.py content_participant show --settings=config.content_demo --username demo_revisor
uv run python manage.py content_participant review-notices --settings=config.content_demo --username demo_revisor
```

Ambos comandos son de lectura. Antes del alta, `delivery_eligible` debe ser `false`.

### 2. Registrar el consentimiento

```powershell
uv run python manage.py content_participant register --settings=config.content_demo --username demo_revisor --role reviewer --telegram-id 9005591275 --consent-confirmed
uv run python manage.py content_participant show --settings=config.content_demo --username demo_revisor
```

Resultado esperado: rol `reviewer`, ID `9005591275`, `authorized: true`,
`verified: false`, `delivery_eligible: false`. El comando no llama a Telegram y
mantiene bloqueados los avisos anteriores. No cambia tareas ni versiones.

### 3. Arrancar una sola instancia y verificar con un mensaje nuevo

Espera al menos dos segundos tras el registro para separar la marca temporal del
vínculo de las fechas de Telegram. Antes de mandar `/start`:

```powershell
uv run python manage.py run_secuoya_demo --settings=config.content_demo --preflight
```

Si hay actualizaciones pendientes, inspecciona cada ID antes de decidir qué hacer:

```powershell
uv run python manage.py run_secuoya_demo --settings=config.content_demo --inspect-pending-id ID_REAL
```

No las descartes ni autorices a ciegas. Un `/start` anterior al alta no sirve para
verificar el vínculo nuevo; si bloquea el arranque, requiere revisión de su contexto.
Con la cola revisada y vacía:

```powershell
uv run python manage.py run_secuoya_demo --settings=config.content_demo --live
```

Desde la tercera cuenta, abre `@secuoyachile_incidentes_bot` en chat privado y envía
un `/start` NUEVO, después `/contenido`. El remitente y el chat deben corresponder
a 9005591275. No lo envíes desde Fernando ni desde un grupo.

```powershell
uv run python manage.py content_participant show --settings=config.content_demo --username demo_revisor
```

Comprueba `authorized: true`, `verified: true`, `revoked: false`, `active: true` y
`delivery_eligible: true`. La verificación no cambia el rol ni los permisos.

### 4. Liberar únicamente la asignación revisada, sin copias

Esta es una operación separada y deliberada. Revisa otra vez:

```powershell
uv run python manage.py content_participant review-notices --settings=config.content_demo --username demo_revisor
```

Solo si el aviso 126 sigue siendo la asignación vigente de CT-000005 y aparece
`release_eligible: true`, puedes encolarlo explícitamente:

```powershell
uv run python manage.py content_participant release-review --settings=config.content_demo --username demo_revisor --notice-id 126 --operator demo_admin --release-confirmed
```

Se reutiliza la MISMA fila 126 y su clave de deduplicación. No se crea otro aviso,
no se modifica CT-000005 y el anuncio 119 permanece bloqueado. Se registra el
operador y el estado anterior/posterior en `ParticipantAccessEvent`.

El comando es administración local privilegiada. `--operator demo_admin` exige un
administrador activo y registra la identidad declarada; no autentica una sesión
web por contraseña. No depende de que el administrador tenga Telegram vinculado.

La respuesta `pending` significa encolado, no entregado. El receptor único existente
efectuará la entrega y guardará su acuse. Después consulta:

```powershell
uv run python manage.py content_participant review-notices --settings=config.content_demo --username demo_revisor
```

Entrega confirmada: aviso 126 con `status: sent`, `sent_at` y
`telegram_message_id`. Si continúa pendiente no se afirma recepción. Si la entrega
es incierta, no la reenvíes: requiere revisión. Repetir la liberación ya registrada
no altera ni repite envíos, acuses o contadores.

Se rechazan mensajes enviados, inciertos, fallidos o parciales, intentos previos,
destinatarios diferentes, asignaciones posteriores, revisiones completadas y
versiones que ya no correspondan. Nunca se liberan todos los históricos en lote.

### 5. Continuar la revisión sin aprobar accidentalmente

En el Telegram del Revisor:

```text
/contenido
¿Qué revisión tengo asignada?
Muéstrame el estado y la versión pendiente de CT-000005.
```

Estas consultas no aprueban ni cambian el estado. Tras revisar el material real,
el titular podrá solicitar aprobación o correcciones según las reglas vigentes;
el bot pedirá la confirmación explícita del resumen entregado. No basta una
consulta de estado ni el alta para aprobar la versión.

## Otros roles

La infraestructura de participantes, consentimiento, recepción privada, outbox y
auditoría ya es común. Puede reutilizarse posteriormente para `journalist`, `editor`
y `switch`, pero la lista del comando permanece cerrada a los tres roles actuales.
Habilitar otro rol requiere autorización y pruebas de sus tareas, destinatarios y
confirmaciones. No se habilitaron ni se prepararon migraciones para esa ampliación.

## Pruebas

Suite completa de Contenidos: **227 aprobadas, 0 omitidas y 0 fallidas**, incluidas
23 pruebas nuevas de esta intervención. Django `check` sin problemas;
`makemigrations --check --dry-run` sin cambios y `migrate --check` sin pendientes.
No se aplicaron migraciones ni se repitieron suites ajenas al cambio.

La comprobación final `READ ONLY` confirmó que CT-000005 y su historial, los dos
avisos, los participantes, los consentimientos y la transferencia coinciden con
la referencia inicial. El ID 9005591275 sigue sin registrar; la versión 1.0 sigue
en `review`, con `approved_at` vacío. Evidencia:
`.qa/reviewer-registration-integrity-final.log`.

Datos sintéticos y PostgreSQL temporal. Se probó la aprobación exclusivamente en
esa base para comprobar el permiso del revisor y la entrega del resumen; nunca
se aprobó CT-000005 real. Telegram está simulado y no se consultaron sus updates.
Logs: `.qa/reviewer-registration-targeted.log` y `.qa/reviewer-registration-final.log`.
