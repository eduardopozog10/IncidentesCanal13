# Fotografía inicial — 19 de octubre de 2026

Primera ronda completa ejecutada antes de cambiar lógica de aplicación. Evidencia original inmutable: `initial.json`, 15 casos, PostgreSQL QA, Gemini real, sin transporte Telegram (Nivel B). Se conservaron 12 incidentes QA y tres borradores sin fabricar registros. Los 71 tests históricos pasan.

## Hallazgos antes de corregir

| ID | Reproducción y observación | Esperado | Causa / módulo | Severidad |
|---|---|---|---|---|
| H01 / 11 | Enviar reporte completo y «Me equivoqué: es Cámara 4, ocurre en Estudio 8 y la prioridad es alta, no crítica». Solo pide prioridad; conserva cámara, ubicación y prioridad antiguas. | Aplicar los tres cambios y conservar área, síntoma e impacto. | Atajos de un solo campo y extracción de prioridad que considera también la alternativa negada; `incident_processor.py`. | Alta |
| H02 / 12 | «Puede ser Cámara 3 o Cámara 4; no sé cuál». Presenta resumen listo con ambas cámaras como un equipo. La aclaración posterior termina pidiendo otra vez prioridad ya conocida. | Aclarar equipo, conservar resto y aceptar corrección del síntoma. | Falta de validación de alternativas y mezcla entre atajo directo y corrección contextual. | Alta |
| H03 / 15 | Responder impacto con «Además impide enviar material de producción; usamos un respaldo. Prioridad media». Guarda toda la frase como impacto y vuelve a pedir prioridad. | Extraer ambos datos. | Detección insuficiente de respuestas compuestas en atajo directo. | Media |
| H04 / 14 | Confirmar dos veces tras crear. No duplica, pero segunda confirmación pide iniciar otro reporte y consulta IA. | Devolver código existente sin crear ni consultar IA. | Falta de respuesta determinista a confirmación posterior. | Baja |
| H05 / revisión | La consulta de duplicados precede a INSERT sin protección concurrente; procesador transaccional lee borrador sin bloquear reportante. | Serializar conversación y admitir duplicado de manera idempotente. | `incoming_message_service.py`, `incident_processor.py`. | Alta |
| H06 / revisión | Respuesta Gemini JSON con lista o campos excesivos no tiene validación de estructura/tamaños antes de persistir. Solo se captura TimeoutError en procesador. | Mantener borrador ante error de proveedor y validar en Django. | `gemini_provider.py`, `incident_processor.py`. | Alta |
| H07 / visual | Logo tiene mucho margen transparente, métricas en 3+2 columnas a 1265 px; varias capas CSS de acabado se contradicen. No hay favicon. | Composición compacta y coherente, identidad navegador, foco y tablas accesibles. | Template y CSS compartidos. | Media |

## Errores de instrumentación (no son fallos del producto)

- Todos los registros recibieron un falso aviso de persistencia porque el evaluador consultaba `draft.incident_id` en una instancia ORM anterior a la confirmación. API respondió 200 y los mensajes estaban vinculados. Auditar contra PostgreSQL, conservar `initial.json` y publicar evaluación corregida aparte.
- Caso 05: «intercom» es genérico según regla existente. La pregunta por identificador es válida. El evaluador repitió el mismo nombre genérico nueve veces en lugar de aportar identificador. Reejecutar solo este caso con «Intercom Riedel 1», sin relajar la regla existente.
- Caso 06: esperaba Video/Cámaras para switcher; el esquema vigente permite Estudios/Producción para este equipo. Revisar expectativa, sin cambiar clasificación de negocio para satisfacer el evaluador.
- Caso 15: el timeout controlado no llegó a activarse porque el atajo directo interceptó el mensaje. H03 es real; la recuperación externa aún requiere una prueba que fuerce la llamada al proveedor.

Los casos 11, 12, 14, 15 y el caso 05 de instrumentación se repetirán individualmente. El resto se audita sin volver a consumir Gemini. La suite automatizada completa sí puede repetirse.

Nivel A pendiente de acceso a sesión personal de Telegram y coordinación del receptor. No afirmar integración real Telegram a partir de estos resultados.

## Ronda A realizada posteriormente, todavía sin cambiar lógica

Autorización recibida para cuenta personal; identificada contra historial del proyecto. `getMe` verificó `secuoyachile_incidentes_bot`, sin webhook. El receptor anterior dejó de aparecer sin que el agente lo detuviera. Se comprobó que solo existía el receptor QA iniciado por este trabajo. No se consultaron ni modificaron otros proyectos.

Evidencia: `telegram-initial.json`, **78 mensajes, 15 borradores, 13 incidentes**, creados por Telegram Web → Telegram real → Gemini real → PostgreSQL QA. Casos 11 y 12 cancelados tras registrar sus fallos; no se confirmó un resumen incorrecto. Caso 15 incluyó reinicio del receptor QA e inyección de un único TimeoutError, luego Gemini real al reanudar. Conservó borrador, datos y prioridad y creó SC-000025.

Resultado inicial A: 01–10, 13 y 15 aprobados; 11 y 12 fallan funcionalmente; 14 falla en respuesta conversacional después de confirmar, aunque solo creó SC-000024. Caso 05 usa identificador Intercom Riedel 1. Caso 06 confirma clasificación existente Estudios/Producción.

Se repetirán por Telegram únicamente **11, 12 y 14**. El fallo de mensaje compuesto del caso 15 se reprodujo en B y en regresión C; su variante A con timeout aprobó y no se repetirá.

Regresiones previas a corrección: 11 pruebas nuevas, 5 fallos y 4 errores, 2 aprobadas (aislamiento y tolerancia a correo). Una prueba de fallo de proveedor fue interceptada por el atajo directo y debe ajustar su mensaje para forzar la llamada; no se atribuye ese error de instrumentación a Gemini.

## Observación de la reprueba A12

La ambigüedad se aclaró y se conservó prioridad media, pero el campo síntoma siguió diciendo «Corte de imagen» mientras la descripción ya decía «imagen intermitente en lugar de un corte total». No se confirmó este resumen. Se amplía H02: reconocer correcciones naturales también durante recopilación y extraer equipo y síntoma conjuntamente. Se conserva evidencia `telegram-retest-1.json` y se vuelve a verificar únicamente A12, continuando su borrador.

## Hallazgo de revisión final previo a su corrección

H08, severidad alta: el procesador marca el borrador READY antes de que Telegram envíe el resumen. Si el envío falla (por ejemplo, resumen de más de 4096 caracteres) o llega una confirmación concurrente antes de registrar la respuesta saliente, un nuevo «confirmo» podría registrar sin haber entregado el resumen. Reproducción controlada: preparar borrador READY sin respuesta OUTBOUND/SENT y confirmar desde un procesador asociado al transporte real. Esperado: volver a presentar el resumen y exigir otra confirmación después de entrega correcta. Se agregará comprobación de entrega en el handler real, manteniendo el contrato de los servicios internos que devuelven el resumen por respuesta HTTP.

La corrección incluye entrega de resúmenes extensos en partes: todos los datos se conservan, la pregunta de confirmación llega al final y solo se registra SENT cuando se enviaron todas las partes. El registro lógico conserva los IDs de las partes para trazabilidad. Un fallo parcial conserva FAILED y no autoriza creación.
