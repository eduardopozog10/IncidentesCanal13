# Historial de cambios · Secuoya Chile

## Arquitectura inspeccionada

Antes existían `Incident.created_at`, `updated_at`, `resolved_at`, reportante y relaciones con mensaje/borrador. Django Admin usa además su `LogEntry` nativo (actor, objeto y campos del formulario), pero este no contiene los valores anteriores ni registra el PATCH del dashboard. No había un historial de gestión común. Se conserva ese registro nativo y se incorpora `IncidentChange` para la trazabilidad solicitada.

Caminos de escritura identificados:

- `IncidentDetailView.patch`: solo estados; continúa rechazando prioridad y campos ajenos. Ahora admite también `observation` opcional.
- `IncidentAdmin.save_model`: estados/prioridad según el perfil y los campos operativos que ya estaban disponibles para Administrador. No amplía sus permisos ni añade una API de prioridad.
- `IncidentProcessor`: crea incidencias al confirmar un borrador; no cambia posteriormente su estado/prioridad. No se modifica el procesador ni se generan eventos en la creación.
- No se encontraron actualizaciones de incidencias mediante `QuerySet.update()`, `bulk_update()`, señales o SQL directo en los flujos actuales. Las actualizaciones de mensajes, borradores y notificaciones son modelos distintos y permanecen fuera del historial.

## Registro e integridad

`Incident.save()` compara la fila persistida, bloqueada con `select_for_update()`, y guarda la modificación y sus eventos dentro de una transacción. Se crea un evento por campo realmente modificado; se respetan `update_fields` y los guardados sin diferencias no crean eventos. La API adquiere el bloqueo antes de leer/aplicar el estado, manteniendo las reglas existentes de `resolved_at`. Admin vuelve a leer la fila bloqueada y aplica únicamente los campos cambiados del formulario: no sobreescribe otros campos que hayan cambiado mientras se procesaba la solicitud.

Cada evento conserva incidencia, fecha, acción, campo, valor anterior/nuevo, usuario, identificación textual histórica, observación y origen. Se registran `status`, `priority`, `technician`, `unit`, `area`, `location_context`, `asset_category`, `equipment`, `failure_type`, `description` y `operational_impact` cuando cambian mediante `save()`. El reportante se identifica mediante nombre y UUID para distinguir homónimos. Los valores de estados/prioridades se almacenan con sus códigos existentes y se muestran con sus etiquetas actuales.

Se excluyen mensajes originales, payloads de Telegram/Gemini, contraseñas, tokens y datos de cuentas. El mensaje original sigue teniendo su registro genérico nativo al editarlo desde Admin, pero no se copian sus contenidos al historial de gestión. Los textos operativos y observaciones son contenido introducido por usuarios: no incluir credenciales en ellos; no existe un detector automático de secretos.

El actor procede de `request.user`, no del JSON/formulario. El origen es `dashboard` o `admin`. Un guardado interno sin actor explícito se identifica como `Proceso interno` / `Sistema / proceso interno`; no se inventa una identidad. La cuenta puede desactivarse, renombrarse o eliminarse: la identificación textual permanece y la relación al usuario eliminado queda en NULL.

La relación con la incidencia usa `PROTECT`: una incidencia con historial no puede eliminarse accidentalmente a través del ORM/Admin. Esto protege la trazabilidad; no se añade ningún flujo de borrado. El historial no tiene permisos CRUD generados, no está registrado en Admin y no tiene rutas de escritura en el dashboard/API. No ofrece garantías frente a un administrador de PostgreSQL con acceso directo.

## Interfaz, observaciones y permisos

El detalle muestra **Historial de cambios**, del más reciente al más antiguo, con desempate por identificador. La API `/api/v1/incidents/<id>/history/` pagina 20 eventos por página y usa un índice por incidencia, fecha e identificador; no incluye el historial en las consultas de listado. La respuesta privada usa `no-store`. Las fechas del historial se presentan explícitamente en `America/Santiago`.

Sin eventos se muestra «No se han registrado cambios de gestión para esta incidencia». La observación es opcional, incluso al resolver, con máximo de 1000 caracteres validado en API y formulario Admin. Solo se conserva junto a un cambio real; escribir una observación sin modificar datos no crea un comentario independiente. Si Admin cambia varios campos, la observación acompaña a cada evento generado.

La línea de tiempo construye nodos DOM usando `textContent`, sin interpretar HTML. Reutiliza la paleta clara/oscura y el diseño responsive actuales. Todos los perfiles pueden consultar. Administrador/Supervisor mantienen sus permisos de estados y prioridad; Operador mantiene estados; Visualizador no escribe. No se modifica `access.py` ni la matriz de perfiles. CSRF y las sesiones existentes siguen vigentes; los reportantes de Telegram siguen separados de los usuarios web.

## Migración pendiente y validación manual

Migración nueva: **`incidents.0013_incidentchange`**, archivo `incidents/migrations/0013_incidentchange.py`. Crea únicamente la tabla del historial con sus relaciones e índice. No cambia incidencias, usuarios, numeración ni crea eventos históricos ficticios. **No se aplicó contra la base operativa.** Solo se ejecutó al crear bases temporales de pruebas.

Antes de probar en el piloto operativo, el responsable debe revisar la migración, disponer del respaldo habitual y aplicarla manualmente con Django detenido:

```powershell
uv run python manage.py migrate incidents 0013 --plan
uv run python manage.py migrate incidents 0013
uv run python manage.py check
```

No ejecutar estos comandos sobre la base operativa desde la automatización de esta tarea. Al retomar el servidor, la tabla debe existir antes de abrir detalles o guardar cambios.

1. Abrir una incidencia existente: debe mostrar el estado vacío si todavía no tiene cambios posteriores a esta incorporación.
2. Como Operador, cambiar estado y escribir una observación. Comprobar usuario, fecha chilena, origen, valor anterior/nuevo; repetir el estado no debe añadir evento.
3. Como Supervisor/Administrador, utilizar el enlace existente de prioridad a Django Admin, cambiar prioridad y volver al detalle. Debe aparecer el evento con origen Administración Django. Probar también cambio de estado con/sin observación, incluido Resuelto.
4. Como Visualizador, consultar el historial y comprobar que no aparecen controles de escritura. Los intentos directos de escritura siguen denegados.
5. Comprobar una observación con caracteres `<`/`>` como texto literal; no usar credenciales. Verificar escritorio, móvil y temas claro/oscuro; con más de 20 eventos, comprobar Anterior/Siguiente.

## Validación automatizada

Se añadieron **27 pruebas** en `incidents/test_incident_history.py`. Incluyen estados/prioridad/Admin, actor/fecha/valores, observaciones opcionales y límite, ausencia de duplicados y eventos ficticios, inyección de metadatos, permisos/CSRF, separación Telegram, lectura exclusiva, identidad tras desactivación/renombrado/borrado, paginación/aislamiento, reportantes homónimos, preservación de cambios concurrentes de Admin, rollback ante fallos y dos solicitudes concurrentes reales contra PostgreSQL.

Suite completa: **274 descubiertas, 272 aprobadas, 0 fallidas y las mismas 2 omitidas** (47,618 s), mediante:

```powershell
uv run --no-sync python manage.py test incidents --settings=config.qa --noinput
```

Las bases temporales `test_qa_secuoya_20261019` y `test_qa_secuoya_history_visual` se crearon/destruyeron únicamente para QA. Se usaron correo en memoria y proveedores simulados. Se revisó la interfaz con snapshots renderizados desde QA a 1440 y 390 px, paginación, texto HTML literal y temas claro/oscuro; no se realizaron escrituras operativas desde el navegador. Sintaxis del JavaScript renderizado verificada con `node --check`. `check --settings=config.qa`, `makemigrations --check --dry-run --settings=config.qa` y `git diff --check` correctos. Sin servicios externos, cambios de infraestructura, dependencias nuevas, commit ni push.

Tras el ajuste final del ancho de la observación se repitió `test incidents.test_incident_history --settings=config.qa --noinput`: **27/27 aprobadas** (5,653 s). También se ejecutó `uv run --no-sync python manage.py check` con la configuración normal: sin incidencias. La vista temporal y su servidor de revisión quedaron cerrados.

## Límites

- No reconstruye el pasado ni registra visualizaciones, filtros o correcciones de borradores.
- Una modificación futura con `QuerySet.update()`, `bulk_update()` o SQL no invoca `save()` y deberá incorporar auditoría transaccional explícita. No introducir esos caminos sin revisar esta garantía. Las inserciones masivas también son creación y no producen eventos de gestión.
- Los procesos internos que conozcan al responsable deben proporcionar `history_actor` y `history_source` al guardar. Sin ese contexto se conserva el origen interno sin atribuirlo a una persona.
- No es un registro criptográfico ni protege frente a cambios directos de un administrador de base de datos. Los eventos no se pueden modificar desde la aplicación.
- Las etiquetas de estados/prioridades se resuelven con las opciones actuales; los códigos originales permanecen almacenados.
