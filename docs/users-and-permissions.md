# Usuarios y permisos · Secuoya Chile

## Alcance y compatibilidad

El módulo usa el `User` nativo de Django, sus grupos y sus permisos. No incorpora un modelo de usuario ni vincula `Technician` / `TechnicianChannel` a cuentas web. El autorregistro de Telegram, Gemini, las incidencias históricas, sus correcciones, la recuperación y los correos no se modifican.

| Perfil | Consultar panel/incidencias/detalle | Analítica | Estados | Prioridad existente | Usuarios |
|---|---|---|---|---|---|
| Administrador | Sí | Sí | Sí | Sí | Sí |
| Supervisor | Sí | Sí | Sí | Sí | No |
| Operador | Sí | No | Sí | No | No |
| Visualizador | Sí | Sí | No | No | No |

Los grupos son `Secuoya - Administrador`, `Secuoya - Supervisor`, `Secuoya - Operador` y `Secuoya - Visualizador`. El prefijo evita apropiarse de grupos genéricos preexistentes. Usan `view_incident` y permisos específicos de acceso al panel, analítica, estados, prioridades y usuarios. Administrador y Supervisor conservan también `change_incident` para la operación existente del administrador de Django. Los permisos individuales o de otros grupos no pueden ampliar la matriz del perfil oficial. Una cuenta con varios perfiles oficiales se rechaza hasta corregir su asignación.

La API de detalle existente sigue actualizando **solo estados**. Ahora rechaza campos extra en vez de ignorarlos. La edición de prioridad ya existía en `IncidentAdmin`; el detalle autorizado enlaza ese mismo formulario. Supervisor puede editar únicamente estado y prioridad en él; no puede modificar otros datos. No se crea una nueva operación REST de prioridad ni nuevas reglas de transición.

Operador conserva el resumen básico del centro de control y la gestión de estados. No consulta la API de analítica ni puede abrir su vista por URL. Visualizador consulta detalles, incluso desde correos, sin controles de escritura. Las comprobaciones se realizan en servidor, además de adaptar la navegación y los botones.

## Transición de cuentas existentes

La migración **no modifica usuarios existentes**, contraseñas, flags, grupos de sus cuentas ni incidencias:

- Un superusuario activo conserva Administrador y sus privilegios técnicos. El dashboard no asigna ni elimina `is_superuser`.
- Un `is_staff` histórico sin perfil oficial, que ya tenga `auth.change_user`, conserva Administrador virtual, sin añadir grupos.
- Los demás `is_staff` históricos sin perfil conservan acceso operativo como Supervisor virtual, sin recibir administración de usuarios automáticamente.
- Una cuenta sin perfil y sin privilegios anteriores no obtiene acceso.
- La primera asignación explícita desde Usuarios sustituye los grupos oficiales de esa cuenta y finaliza la excepción histórica. Administrador/Supervisor necesitan `is_staff` para acceder al formulario nativo de prioridad; Operador/Visualizador no lo necesitan. `is_staff` por sí solo no concede permisos a una cuenta con perfil oficial.

No se incluyen reportantes de Telegram en esta transición. Solo aparecen en Usuarios si existe una cuenta `User` creada explícitamente.

Se bloquean la desactivación o degradación del último administrador activo. Las escrituras del módulo usan una transacción y un advisory lock PostgreSQL, revalidando los permisos del actor después de obtener el lock. Un administrador común no puede editar ni restablecer la contraseña de un superusuario. La edición/eliminación de cuentas desde el administrador nativo de Django queda deshabilitada: debe utilizarse el módulo protegido. Los grupos oficiales tampoco se pueden editar o borrar por esa vía.

## Interfaz

`/dashboard/users/` está disponible solo para Administrador, con enlace desde la sidebar y la navegación móvil. Permite buscar por nombre/apellido/usuario/correo, filtrar por perfil y estado, paginar, crear, editar y activar/desactivar. No se implementa eliminación de cuentas.

Las páginas reutilizan la sidebar, los controles de sesión, la marca y las hojas de estilo del dashboard. Los formularios validan campos obligatorios, correo, perfil y duplicados de usuario/correo sin distinguir mayúsculas. Los duplicados históricos no se alteran automáticamente. Las escrituras mantienen CSRF y las páginas privadas no se almacenan en caché.

## Contraseñas sin SMTP

1. Al crear una cuenta activa, Django guarda una contraseña inutilizable. Nunca genera una contraseña visible ni envía invitaciones o correos.
2. Se presenta al administrador, una sola vez, un enlace privado para que el titular establezca su contraseña. El token nativo de Django expira en una hora, deja de funcionar al establecer la contraseña y se invalida al generar otro.
3. La entrega debe hacerse personalmente o por un canal privado acordado. El enlace es una credencial temporal: no publicarlo ni pegarlo en incidencias. Con `127.0.0.1` solo funciona en el mismo computador. En el piloto puede abrirlo el titular en una ventana privada del mismo equipo, sin entregar la sesión administrativa.
4. Para una cuenta inactiva no se genera enlace hasta activarla. En Editar usuario se puede generar otro enlace; esto invalida su contraseña y sesiones previas. No solicita servicios externos.
5. Para la propia cuenta se usa **Cambiar mi contraseña**, con la contraseña actual, conservando la sesión vigente. No se permite restablecerse a sí mismo por el flujo administrativo para evitar perder el único acceso.

No hay registro público ni formulario público de solicitud de restablecimiento. El receptor del enlace usa `PasswordResetConfirmView` y `SetPasswordForm` nativos, validadores de fortaleza y campos de contraseña enmascarados. Los formularios y la pantalla privada de entrega usan `Referrer-Policy: same-origin`: permiten los POST del mismo origen sin enviar `Referer` a otros orígenes. No hay una etiqueta meta que sustituya esa política ni recursos externos en estas pantallas. La URL con token responde con `no-referrer`, tanto al redirigir a la URL nativa `set-password` como al mostrar un enlace inválido; el formulario se sirve sin el token original en su URL. Los formularios y la entrega conservan `Cache-Control: no-store`; el token se oculta del access log de `runserver`. Un servidor/proxy futuro deberá aplicar su propia redacción de URLs sensibles y HTTPS. No se configura esa infraestructura en esta fase.

Cuando exista correo transaccional aprobado, podrá enviarse el enlace al destinatario verificado en lugar de entregarlo manualmente. No se deben enviar contraseñas en texto plano. Esa integración no forma parte de este cambio.

## Migración y validación manual

Se prepara `0012_management_roles`: añade opciones de permisos al modelo Incident y crea grupos/permisos de manera repetible. No cambia columnas ni transforma datos operativos. Si existe un grupo del mismo nombre, con miembros y permisos distintos, se detiene para revisión sin cambiar cuentas. Una reversión conserva los grupos para no borrar asignaciones.

**La implementación inicial no ejecutó esta migración contra la base local operativa.** El responsable confirmó después su aplicación manual con respaldo. La corrección posterior de CSRF no necesita migraciones. Para revisar sin tocar la base operativa:

```powershell
uv run python manage.py check --settings=config.qa
uv run python manage.py makemigrations --check --dry-run --settings=config.qa
uv run python manage.py test incidents --settings=config.qa --noinput
git diff --check
```

Después de autorizar la aplicación manual, con copia de seguridad y el servidor web detenido:

```powershell
uv run python manage.py migrate --plan
uv run python manage.py migrate
uv run python manage.py check
# Solo para una instalación sin administrador técnico existente:
uv run python manage.py createsuperuser
uv run python manage.py runserver
```

No repetir `createsuperuser` si ya existe una cuenta técnica. Iniciar sesión con ella o con un administrador histórico autorizado en `/dashboard/login/`, abrir Usuarios y crear las cuentas de los cuatro perfiles. `/admin/login/?next=...` sigue aceptándose, conservando los enlaces de correo y rechazando destinos externos.

Comprobar manualmente: edición y filtros; enlace de contraseña desde ventana privada; login de cada perfil; denegaciones por URL/API; edición de estados y prioridad según la tabla; desactivación mientras existe otra sesión; prohibición de quitar al último administrador; enlace de una incidencia recibido por correo, sin generar ni reenviar correo durante esta validación.

## Archivos del módulo

- Autorización y cuentas: `incidents/access.py`, `user_forms.py`, `user_views.py`, `services/management_user_service.py`.
- Integración: `config/settings.py`, `config/urls.py`, `incidents/dashboard_views.py`, `views.py`, `serializers.py`, `admin.py`.
- Permisos: `incidents/models.py`, `migrations/0012_management_roles.py`.
- Presentación: `incidents/templates/incidents/sidebar.html`, `users/*.html`, `dashboard.html`, `account_controls.html`; `incidents/static/incidents/users.css`, `dashboard.css`.
- Plantillas nuevas bajo `incidents/templates/incidents/users/`: `auth_base.html`, `base.html`, `list.html`, `form.html`, `handoff.html`, `login.html`, `password_confirm.html`, `password_complete.html` y `password_change.html`.
- Redacción de tokens de desarrollo: `incidents/sensitive_logging.py`, `incidents/apps.py`.
- Pruebas: `incidents/test_user_management.py`. Las 201 pruebas anteriores permanecen sin editar.

## Resultado de validación

**240 pruebas descubiertas: 238 aprobadas, 0 fallidas y las mismas 2 omitidas explícitamente** por las limitaciones de entrega incierta. Se agregaron 39 pruebas del módulo. Se cubren roles, login y enlaces profundos, accesos directos/API/admin, CSRF, estados/prioridad, perfiles conflictivos, privilegios técnicos, cuenta administrativa final y concurrencia, desactivación y sesiones, duplicados y formularios, colisiones de grupos al migrar, separación de reportantes y tokens locales de contraseña.

`check --settings=config.qa`, `makemigrations --check --dry-run --settings=config.qa` y `git diff --check` correctos. Se validó la sintaxis del JavaScript renderizado para Administrador y Operador con `node --check`. Revisión visual del listado en escritorio y del formulario a 390 px, usando únicamente cuentas ficticias y una segunda base temporal de tests QA, destruida al finalizar. No se inició el bot ni se usaron APIs externas o SMTP. No se modificó `.env`, no se aplicó la migración operativa y no se realizaron commits ni push.

## Corrección posterior de CSRF y contraseñas

El responsable reprodujo en Chrome y Edge POST con `Origin: null` y comprobó que retirar `no-referrer` de las páginas con formularios resolvía el bloqueo. `Origin` y `Referer` son cabeceras distintas: el [estándar Fetch, algoritmo de Origin](https://fetch.spec.whatwg.org/#append-a-request-origin-header) contempla serializar `Origin` como `null` para solicitudes no CORS distintas de GET/HEAD bajo `no-referrer`. El middleware CSRF de Django rechaza ese origen antes de validar el token. No se acepta `null`, no se añade ningún origen confiable y no se exime ningún formulario de CSRF.

Se conserva la retirada manual del meta de `auth_base.html`. Login, creación y cambio de contraseña conservan el `same-origin` predeterminado de `SecurityMiddleware`; confirmación de contraseña y entrega lo establecen explícitamente. Solo la respuesta en la URL con token aplica `no-referrer`. En la entrega el enlace privado está en un campo de solo lectura, no en la URL de la página que podría enviarse como `Referer`.

Se agregan 7 pruebas de regresión: el módulo pasa de 39 a 46. Comprueban POST reales del cliente de pruebas con `enforce_csrf_checks=True`, rechazo de CSRF ausente/incorrecto y orígenes externo/`null`, políticas de respuestas y plantillas, entrega y logout, token ligado a sesión y de un solo uso. Las pruebas previas conservan la revocación de enlaces anteriores, expiración, permisos y separación de reportantes de Telegram.

Validación de esta corrección: `uv run --no-sync python manage.py check` y su variante `--settings=config.qa` sin incidencias; `test incidents.test_user_management --settings=config.qa --noinput`: **46/46 aprobadas** (3,567 s); `test incidents --settings=config.qa --noinput`: **247 descubiertas, 245 aprobadas, 0 fallidas y 2 omitidas** (16,295 s). Ambas baterías se ejecutaron con `uv run --no-sync python manage.py` y crearon/destruyeron únicamente la base temporal PostgreSQL `test_qa_secuoya_20261019`, con correo en memoria y proveedores simulados. No se usaron APIs externas ni se modificó la base operativa. No se realizaron commits ni push.

Para la última validación manual: reiniciar el servidor Django si no recargó el código, cerrar las pestañas antiguas y abrir de nuevo el login. Crear una cuenta de prueba o generar un enlace nuevo desde Usuarios; abrirlo en una ventana privada del mismo computador. La navegación debe terminar en `/set-password/`; comprobar el guardado y el inicio de sesión, y que reutilizar el enlace no funcione. Comprobar también Cerrar sesión desde la entrega. No hay que ejecutar migraciones.

`no-store` y la redirección reducen la exposición, pero no borran el historial, el portapapeles ni capturas, y no revocan un enlace copiado antes de usarlo o regenerarlo. La credencial original puede quedar en el historial o en herramientas locales; compartirla solo por el canal privado acordado y cerrar la ventana privada al terminar. Las pruebas Django no ejecutan Chrome/Edge: la comprobación final en esos navegadores queda como validación manual, sin cuentas ni contraseñas operativas modificadas por estas pruebas.
