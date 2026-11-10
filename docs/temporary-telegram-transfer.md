# Eduardo representa temporalmente a Fernando — solo demo

Preparado e implementado, **sin transferencias ni restauraciones reales ejecutadas**.
Base: `demo_secuoya_contenidos`; settings: `config.content_demo`.

## Riesgos y condiciones antes de operar

- El usuario Django `demo_admin` conserva su rol, acceso web, solicitudes, tareas,
  conversaciones e historial. Solo se mueve el vínculo Telegram, no esos datos.
- Telegram conserva el historial visual del chat. Los mensajes y operaciones
  anteriores continúan atribuidos a sus participantes originales en PostgreSQL.
- Fernando será coordinador, **no administrador**. No heredará los permisos de
  aprobación, Edición o Switch de Eduardo. El vínculo temporal solo permite
  Coordinación de Contenidos; no delega la cuenta a Gestión de Incidencias.
- Cada cambio exige consentimiento explícito y un `/start` privado **nuevo**.
  No se copia `verified_at` ni se usa la excepción histórica para saltar la
  verificación al restaurar al administrador.
- El receptor debe estar detenido. El comando adquiere su mismo bloqueo local y
  rechaza receptores normales/QA detectados. No detecta receptores de otro computador:
  coordina que no haya otro usando el mismo bot.
- Hay que revisar la cola remota antes de cada cambio. La herramienta no llama
  a Telegram para comprobarla: `--remote-queue-empty-confirmed` es tu declaración
  explícita después del preflight. No es una opción para ignorar mensajes.
- Se rechazan colas locales pendientes y entregas inciertas de origen/destino.
  No se borran ni se marcan como procesadas para permitir la transferencia.
  Si existen, completa o revisa esas operaciones con su identidad original.
- Los mensajes anteriores al nuevo vínculo no se ejecutan bajo el rol nuevo.
  Si aparece alguno después del cambio, detén la prueba y solicita revisión;
  no fuerces IDs, no avances offsets manualmente ni descartes actualizaciones.

El comando es una herramienta **local de administración privilegiada**, no una
API pública. `--operator demo_admin` exige un participante administrador y usuario
Django activos y registra su identidad declarada en auditoría; no es un mecanismo
de autenticación por contraseña. Usa una terminal local de confianza.

## 1. Comprobar el estado anterior, sin cambiar nada

```powershell
Set-Location 'C:\Users\eduar\OneDrive\Documentos\GitHub\IncidentesCanal13'
uv run python manage.py check --settings=config.content_demo
uv run python manage.py content_participant show --settings=config.content_demo --username demo_admin
uv run python manage.py content_participant show --settings=config.content_demo --username demo_fernando
uv run python manage.py content_participant show --settings=config.content_demo --username demo_daniela
uv run python manage.py content_telegram_transfer plan --settings=config.content_demo --source demo_admin --target demo_fernando --telegram-id 6690350935 --operator demo_admin
```

Estado confirmado por lectura al implementar: Eduardo (`admin`) vinculado a
6690350935; Fernando (`coordinator`) sin vínculo; Daniela (`designer`) vinculada a
6578528722, autorizada y verificada. No hay mensajes locales pendientes de estas
cuentas en la inspección realizada, pero debes volver a comprobar al operar.

La migración `0006` ya está aplicada en esta instalación. Añade auditoría y límites
de vigencia; no transfirió ningún vínculo. En otra instalación aplica primero:

```powershell
uv run python manage.py migrate --settings=config.content_demo
```

Se mantiene la unicidad de `Participant.telegram_id` para todos los participantes.
Los consentimientos históricos revocados pueden conservar el mismo ID, pero
PostgreSQL permite **un solo consentimiento activo** por ID y una sola transferencia
activa por cuenta/origen/destino.

## 2. Detener el receptor y revisar la cola como Eduardo

En la terminal donde se ejecuta el receptor actual, pulsa **Ctrl+C**. No abras otro
receptor mientras haces el cambio. En otra terminal:

```powershell
uv run python manage.py run_secuoya_demo --settings=config.content_demo --preflight
```

Debe informar cola remota vacía. Si aparecen pendientes, inspecciónalos con
`--inspect-pending-id ID_REAL` y decide su procesamiento bajo la identidad original
antes de transferir. Los envíos inciertos requieren revisión; no los reenvíes a
ciegas. Nunca uses `--discard-pending`.

## 3. Transferir manualmente, solo cuando estés listo

Genera el identificador **una sola vez** y guárdalo. Reutilizarlo hace idempotente
la operación: no duplica la auditoría ni reinicia una verificación ya obtenida.

```powershell
$transferId = [guid]::NewGuid().ToString()
Write-Host "Guarda este transfer-id para restaurar: $transferId"
uv run python manage.py content_telegram_transfer transfer --settings=config.content_demo --source demo_admin --target demo_fernando --telegram-id 6690350935 --operator demo_admin --transfer-id $transferId --consent-confirmed --remote-queue-empty-confirmed
uv run python manage.py content_telegram_transfer show --settings=config.content_demo --operator demo_admin --transfer-id $transferId
uv run python manage.py content_participant show --settings=config.content_demo --username demo_admin
uv run python manage.py content_participant show --settings=config.content_demo --username demo_fernando
```

Resultado: Eduardo sigue activo y administrador, pero sin Telegram vinculado.
Fernando tiene 6690350935 autorizado, **todavía sin verificar**. Daniela no cambia.
Todos los cambios de enlace, consentimiento previo y operador se guardan en una
transacción y en la auditoría. No hay envíos desde este comando.

Si pierdes la variable al cerrar PowerShell, recupera el UUID de solo lectura:

```powershell
uv run python manage.py content_telegram_transfer show --settings=config.content_demo --operator demo_admin --source demo_admin --target demo_fernando
```

Asigna a `$transferId` el UUID exacto de la operación activa; no generes otro para
intentar restaurar. Un UUID restaurado no vuelve a transferirse al repetir el comando.

## 4. Verificar el vínculo nuevo y probar Fernando → Daniela

Espera al menos dos segundos tras el cambio para separar las fechas de Telegram.
Inicia el receptor único antes de enviar mensajes nuevos:

```powershell
uv run python manage.py run_secuoya_demo --settings=config.content_demo --preflight
uv run python manage.py run_secuoya_demo --settings=config.content_demo --live
```

Desde la cuenta personal 6690350935 envía un `/start` **nuevo** al bot, y luego
`/contenido`. El receptor valida remitente/chat privado, fecha posterior al cambio
y actualización recibida; solo entonces registra la verificación de Fernando.
Si enviaste `/start` antes de iniciar el receptor, inspecciona ese pendiente y
autoriza únicamente su ID revisado según el procedimiento habitual.

```powershell
uv run python manage.py content_participant show --settings=config.content_demo --username demo_fernando
uv run python manage.py content_participant show --settings=config.content_demo --username demo_daniela
```

Comprueba `verified`, `delivery_eligible` y el acuse `last_delivery` de Fernando.
No copies verificaciones ni edites la base manualmente si todavía no aparece listo.

Recorrido:

1. Desde Fernando crea una solicitud NUEVA y confirma su resumen. Conserva
   CT-000002 y los otros trabajos existentes.
2. «Asigna CT-NUEVO a Daniela», revisa y confirma.
3. Daniela recibe el aviso en 6578528722 y selecciona `/contenido` en su chat.
4. Daniela: «Ya puedo comenzar». Fernando: «¿Cómo va Daniela?».
5. Si Fernando autoriza «Sí, pregúntale», Daniela responde con su avance o ETA.
   Las conversaciones son independientes y una ETA no marca entrega.

Las tareas históricas de Eduardo **no se reasignan** a Fernando. Usa un nuevo
trabajo; Fernando puede coordinar, pero no actuar como diseñador/revisor/editor
simplemente por usar la cuenta que antes representaba al administrador.

## 5. Restaurar manualmente al terminar

Termina las operaciones en curso, detén el receptor con Ctrl+C y revisa otra vez
`--preflight` con la identidad de Fernando. Solo con las colas revisadas/vacías:

```powershell
uv run python manage.py content_telegram_transfer restore --settings=config.content_demo --operator demo_admin --transfer-id $transferId --consent-confirmed --remote-queue-empty-confirmed
uv run python manage.py content_telegram_transfer show --settings=config.content_demo --operator demo_admin --transfer-id $transferId
uv run python manage.py content_participant show --settings=config.content_demo --username demo_admin
uv run python manage.py content_participant show --settings=config.content_demo --username demo_fernando
```

Resultado: 6690350935 vuelve a Eduardo/admin; Fernando vuelve a quedar sin vínculo.
La restauración **no restaura automáticamente la verificación anterior**. Espera
dos segundos, reinicia el receptor y envía `/start` nuevo desde Eduardo, luego
`/contenido`. Comprueba que el administrador vuelve a estar habilitado.

La herramienta conserva las conversaciones de cada participante; no copia el
contexto de Fernando al administrador. `/start` inicia la sesión conversacional
segura según las reglas existentes, sin borrar mensajes, borradores ni auditoría.
Los avisos bloqueados no se reactivan y los mensajes previos no se ejecutan con el
rol restaurado. Los cambios inesperados de IDs/roles bloquean la operación, sin
sobrescribirlos; revisa su auditoría antes de continuar.

## Pruebas y alcance

Validación final: 147 pruebas de contenidos aprobadas (incluye 20 específicas
de este procedimiento), 69 pruebas seleccionadas de incidencias aprobadas,
`manage.py check` sin problemas y `makemigrations --check --dry-run` sin cambios.

Pruebas con datos sintéticos en PostgreSQL temporal: transferencia/restauración,
operadores, roles, usuarios inactivos, consentimiento, verificación real del flujo
de `/start` simulado, mensajes antiguos, unicidad, colas, idempotencia, bloqueo del
receptor y concurrencia. Los comandos de escritura no se ejecutaron en la demo
real; no se enviaron mensajes y no se inició ningún receptor.

Se modificó únicamente contenido/demo y su esquema. No se alteraron los permisos
ni los registros del módulo de incidencias, producción, los usuarios originales
ni los vínculos reales. No hubo commit ni push.
