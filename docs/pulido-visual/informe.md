# Pulido visual final · Secuoya Chile

- Códigos completos con `nowrap` y columna mínima de 116 px en ambas tablas; botones de detalle conservados. Columnas de Incidentes con mínimos por contenido, fechas y etiquetas sin fragmentación, scroll interno. En móvil se conserva el listado de tarjetas existente.
- Indicador estático reemplazado por «Panel de gestión disponible», sin señal verde de salud. Críticos del período identifica todos los estados y usa un acento neutral; críticos abiertos conserva el énfasis de atención.
- Analítica: nombres largos completos, cifras alineadas, encabezados flexibles. La agregación existente usa el nombre de equipo; el texto ahora dice «Equipos o servicios más mencionados en los reportes».
- Gestión: espaciado de textos, controles y feedback; foco visible y controles táctiles. Sin cambios en transiciones ni guardado.
- Correo HTML con tablas, estilos inline, identidad textual independiente de imágenes, datos reales autoescapados, descripción e impacto sin reformulación y alternativa de texto plano intacta. El único cambio del servicio es renderizar y adjuntar `html_message` a la llamada existente de `send_mail`. Destinatario, asunto, momento de envío y manejo de errores conservados.

## Archivos de este encargo

- `incidents/static/incidents/operations.css`
- `incidents/templates/incidents/dashboard.html`
- `incidents/templates/incidents/email/incident_created.html` (nuevo)
- `incidents/services/incident_notification_service.py`
- `incidents/test_email_presentation.py` (nuevo)
- `scripts/preview_incident_email.py` (nuevo)
- Este informe y capturas en esta carpeta.

Se conservaron los cambios locales anteriores; no se hizo commit ni push.

## Validación

- `uv run --no-sync python manage.py check --settings=config.qa`: sin incidencias.
- `uv run --no-sync python manage.py test --settings=config.qa --noinput`: **94 pruebas correctas**, incluida la suite anterior y tres nuevas de presentación del correo.
- `git diff --check`: correcto.
- Navegador: cuatro vistas a 1440 y 390 px, claro/oscuro, sin desborde horizontal global. Tabla con scroll interno y códigos completos; detalle de Gestión hasta controles inferiores.
- Correo sintético SC-000038 revisado a 900 y 390 px, campos vacíos y botón condicional. Sin creación de incidentes para las vistas previas ni envío SMTP. Pruebas automatizadas solo en la base de tests QA y correo en memoria. Sin nuevas llamadas Telegram/Gemini ni escrituras en la base principal.

## Vista previa y límites

Ejecutar `uv run --no-sync python scripts/preview_incident_email.py` y abrir `.qa/email-preview/correo.html`. También genera `campos-opcionales.html` y `boton-condicional.html`; este último usa únicamente `example.org` para demostrar el layout y no debe usarse como enlace real.

No existe una URL pública configurable de detalle en el proyecto. El envío no proporciona `incident_url`, por lo que el botón permanece oculto. La plantilla tiene un bloque condicional preparado: una integración futura debe proporcionar una URL pública absoluta y validada del incidente; no usar direcciones locales.

Diseño basado en la descripción del encargo, sin imagen de referencia disponible. Compatibilidad prevista mediante tablas, estilos inline y ancho adaptable; no se ejecutaron pruebas en Gmail, Outlook o Apple Mail nativos. Outlook clásico puede mostrar esquinas rectas.

Commit sugerido: `Pulir tablas y vistas de Secuoya Chile y añadir correo HTML de incidencias`
