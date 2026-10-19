# Correcciones conversacionales y enlaces de correo · Secuoya Chile

## Diagnóstico y solución

La versión inicial guardada pasó las 94 pruebas existentes. El bloque experimental que evitaba todos los atajos ya estaba retirado; se conservaron los cambios manuales útiles. No se revirtió el procesador.

La última pregunta podía restringir una corrección a un solo campo. Además, el detector solo reconocía ciertas ubicaciones y la fusión sustituía el equipo sin revisar sus dependencias. Ahora las correcciones explícitas y de varios campos preceden al contexto de la pregunta anterior; los nombres de lugares no se limitan a dos estadios. Los mensajes con equipo y síntoma también se extraen completos: el detector controla cuándo evitar un atajo, no cuenta exclusivamente campos corregidos.

Los atajos de ubicación, prioridad y equipos ambiguos se conservan antes de derivar rectificaciones no resueltas a Gemini. Una prioridad declarada se conserva hasta que el usuario la modifica expresamente. No se crea el incidente al corregir: se solicitan los datos pendientes y después se presenta un nuevo resumen que requiere confirmación explícita.

Cuando el equipo cambia de familia, los síntomas, descripción e impacto sin evidencia explícita en el nuevo mensaje se vacían y vuelven a consultarse. La categoría se ajusta a la familia reconocida o queda sin definir si no se conoce. Los cambios de identificador dentro de una misma familia reconocida conservan los datos válidos. Este criterio es conservador: puede pedir una aclaración adicional ante equipos desconocidos o formulaciones que no coincidan claramente; no intenta inventar equivalencias semánticas. Gemini también recibe instrucciones de no arrastrar datos incompatibles.

## Enlace y acceso

El botón «Ver incidencia» y el enlace de texto plano usan el ID real y la ruta Django `dashboard-incident-detail`: `/dashboard/incidents/<id>/`. Se reutiliza el dashboard, que abre Gestión y carga el detalle independientemente de los filtros del listado. IDs inexistentes devuelven 404.

Se detectó acceso anónimo en el dashboard y las cuatro API de consulta/gestión de incidentes. Ahora exigen una sesión de usuario activo de personal (`is_staff`), coherente con el acceso administrativo. La vista HTML dirige al login existente y conserva `next`. Las API rechazan usuarios anónimos o sin autorización. Las cuatro clases de tests anteriores de API ahora autentican un usuario de personal; no se alteraron sus aserciones funcionales. El webhook y la recepción de mensajes conservan su mecanismo actual.

Configurar **`INCIDENT_PUBLIC_BASE_URL`** con el origen HTTPS público real, sin ruta, consulta ni credenciales; agregar ese host a la configuración existente `ALLOWED_HOSTS` del entorno. Ejemplo de formato únicamente: `https://incidencias.example.org`. No se configuró un dominio ficticio en `.env`. Sin origen válido, se mantiene el correo con sus datos y sin botón. Se rechazan direcciones locales/privadas y esquemas inseguros. Los destinatarios necesitarán una cuenta activa de personal. No se crearon cuentas reales ni se desplegó nada.

## Validación

Base de tests: `test_qa_secuoya_20261019`, mediante `--settings=config.qa`; proveedores simulados y correo en memoria. Base inicial: 94/94. Suite final: 106/106, incluidos los casos Monumental → Nacional, cambios mientras falta prioridad, evidencia nueva, prioridad explícita, acceso anónimo/no staff, redirección tras login, 404 y enlace distinto por incidente. `manage.py check` y `git diff --check` correctos. Las excepciones de correo registradas son simulaciones controladas.

Vista previa sintética regenerable con `scripts/preview_incident_email.py`; captura `correo-con-enlace.jpg`. La URL example.org solo ilustra el botón, no se abre ni se envía. No hubo llamadas reales a Telegram/Gemini, SMTP, escrituras en la base operativa, migraciones, commit ni push. No se tocó Gym Planet.

Pendiente manual: configurar el origen público real y verificar navegación completa desde un cliente de correo tras el despliegue autorizado; no se probó Gemini real ni clientes nativos de correo en esta tarea. No se atribuye una causa al retraso informado sin evidencia de ejecución.

## Archivos de este encargo

- `incidents/services/incident_processor.py`, `incidents/ai/gemini_provider.py`
- `incidents/services/incident_notification_service.py`, `incidents/templates/incidents/email/incident_created.html`
- `incidents/dashboard_views.py`, `incidents/views.py`, `incidents/templates/incidents/dashboard.html`
- `config/urls.py`, `config/settings.py`, `.env.example`
- `incidents/tests.py`, `incidents/test_email_presentation.py`, `incidents/test_corrections_and_links.py`
- `scripts/preview_incident_email.py`, este informe y captura.
