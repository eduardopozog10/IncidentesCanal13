# Optimización conversacional v2 — Incidencias Secuoya Chile

Intervención offline del 10/11/2026, sobre `feature/coordinacion-contenidos-demo`,
HEAD `4a692ce`. El árbol estaba limpio al comenzar. Se conservaron la v1 y la
infraestructura de evaluación v2. No hubo llamadas reales a Gemini, Telegram,
SMTP ni otras APIs; no se modificaron bases reales.

## Escenarios investigados

Los mensajes son casos sintéticos reproducibles con contexto inicial guardado en
PostgreSQL temporal y contratos de extracción simulados. No se presentan como
conversaciones reales nuevas ni como mediciones de comprensión de Gemini.

| Caso | Esperado y contexto | Antes | Candidata final |
|---|---|---|---|
| V2-01: cámara sigue fallando, respaldo conectado y consulta de pendientes | Prioridad pendiente; guardar conexión sin inferir reanudación, conservar falla original y preguntar solo lo faltante. | Ya funcionaba con el plan update/query simulado. | Sigue funcionando: conexión guardada, operación detenida y prioridad pendiente; sin creación. |
| V2-02: cambiar prioridad a Alta aunque no se conoce la causa | Con hipótesis del cable ya guardada: Alta, sin diagnóstico confirmado ni pérdida de respaldo/antigüedad. | Ya conservaba la hipótesis previa por el camino local. | Conserva hechos e incertidumbre, procesando completo el mensaje mixto. La variante de hipótesis NUEVA se describe debajo. |
| V2-03: «¿Me explicas impacto operacional? Después seguimos con el reporte» | Explicar sin repreguntar un impacto guardado ni modificar el borrador. | Una extracción simulada terminaba en la pregunta «¿Qué impacto operacional tiene?». | Explicación local, cero extracciones, contexto intacto. No crea recordatorios ni agenda un seguimiento. |
| V2-04: ubicación Estudio B, no cambiar el resto | Actualizar ubicación/unit y sus referencias en descripción, conservando otros hechos. | Ya funcionaba con la extracción simulada correspondiente. | Conservado; respaldo, tiempo, prioridad e hipótesis no se alteran. |
| V2-05: falla de 40 minutos y conexión del respaldo «hace 10» | 40 minutos de falla, evento auxiliar separado, sin inferir la unidad omitida del «10»; respuesta solo de duración. | Ya funcionaba. | Conservado: duración vigente 40, sin 20 anterior; evento auxiliar y modalidad intactos. |
| V2-06: revisar cómo quedó el reporte antes de confirmar | Mostrar resumen completo sin escribir; una confirmación posterior válida crea una vez. | Ya funcionaba con query/summary del proveedor simulado. | Conservado; no se añadió un atajo ni se declaró un fallo inexistente. |
| V2-07: explicación conceptual seguida de corrección real de prioridad | Guardar Alta, reconocer el cambio y explicar el concepto sin repreguntar. | La prioridad se guardaba, pero el formateador omitía reconocer el cambio y volvía a pedir impacto. | Interpreta todo el mensaje, confirma solo los cambios realmente guardados y explica. No consume la orden como una continuación neutral. |
| Ayuda más otra incidencia o dato ambiguo | Conservar borrador y mostrar la aclaración decidida por Django. | La lógica de datos generaba la aclaración, pero el formateador la sustituía por ayuda genérica. | La aclaración de contexto tiene prioridad sobre la redacción conversacional. |
| Prioridad más incertidumbre causal NUEVA | Borrador sin hipótesis previa; guardar Alta y «no sabemos si la causa es el cable», sin afirmarlo como diagnóstico. | El atajo de un campo guardaba Alta y descartaba la nueva incertidumbre. | Delega el mensaje completo a la interpretación existente; incertidumbre y hechos previos quedan conservados. |

La primera reproducción de seis casos incluía una aserción de fixture con el
nombre «Equipo» cuando la plantilla aprobada usa «Equipo/servicio». Se corrigió
esa expectativa ANTES de tocar producción y no se contó como defecto del sistema.
La reproducción inicial válida ampliada a siete casos tuvo cinco aprobados y dos
fallidos. Los fallos de contexto y de hipótesis nueva se reprodujeron después,
antes de aplicar sus correcciones específicas; no se inventa una ejecución
anterior de los 18 tests finales completos.

## Defectos, causas y riesgo de corrección

| Defecto comprobado | Causa en código | Corrección | Riesgo y control |
|---|---|---|---|
| Definición con continuación neutral terminaba repreguntando | `explanation_requested` exigía que todo el mensaje fuera la definición; la ayuda semántica usaba siempre la pregunta para completar datos. | Reconocer una continuación conversacional acotada y reutilizar la explicación conceptual. | Consumir por error hechos u órdenes posteriores. El reconocimiento local exige una continuación completa sin datos, órdenes, confirmaciones ni horarios; hay contrastes negativos. |
| Cambio guardado quedaba sin reconocimiento cuando había ayuda | La rama help de `_compose_semantic_reply` devolvía antes del reconocimiento de cambios que ya usa la rama query. | Calcular el reconocimiento desde los cambios PostgreSQL y reutilizarlo en ambas ramas. | Afirmar cambios inexistentes. Solo se comparan datos previos con datos persistidos; no se retransmite prosa del modelo. |
| Ayuda/consulta ocultaba aclaraciones de contexto | `ask_clarification` agrupaba varios resultados; el formateador no conocía la decisión `requires_update_clarification` del servicio. | Propagar ese booleano calculado por Django y conservar la aclaración antes de aplicar estilo. | Saltarse aclaraciones o alterar reglas. El booleano no viene de Gemini, no autoriza escrituras y no modifica los criterios que deciden ambigüedad/nueva incidencia. |
| Se perdía incertidumbre nueva al corregir otro campo | `_try_process_direct_draft_reply` consumía el mensaje como una corrección de prioridad sin procesar el resto. | Desactivar el atajo ante incertidumbre diagnóstica o «no sabemos» y usar la interpretación completa y el merge existentes. | Más dependencia de IA para mensajes mixtos. Es necesaria para no perder información; se conserva presupuesto/reintentos y recuperación existentes, con regresiones de fallos transitorios. |

No se añadió una arquitectura, otro almacén de memoria ni un clasificador general
de lenguaje. Las nuevas distinciones son locales al módulo de Incidencias. El
procesador sigue decidiendo permisos, datos válidos, confirmación y persistencia.

## Archivos modificados

| Archivo | Cambio |
|---|---|
| `incidents/services/incident_dialogue.py` | Distingue continuación neutral de contenido que debe interpretarse; permite identificar una definición para redactar después de procesar el mensaje completo. |
| `incidents/services/incident_processor.py` | Reutiliza explicación/reconocimiento, preserva la aclaración Django y evita perder incertidumbre con un atajo de campo. |
| `incidents/conversational_tests/test_optimization_v2.py` | 18 tests nuevos de escenarios, contrastes, seguridad, contexto, confirmaciones y recuperación; bloqueo de conexiones externas. |
| `docs/incident-conversation-optimization-v2.md` | Este informe e instrucciones. |

No se modificaron corpus congelado, calificador/comparador, contrato Gemini,
proveedor, modelos, migraciones, permisos, `.env`, participantes ni transferencias.
`conversation_content.py` y `conversation_plan.py` compartidos permanecen byte a
byte iguales al snapshot previo. Contenidos no importa el nuevo enrutamiento de
Incidencias; sus contratos compartidos no cambiaron. No se reejecutó su suite
completa ni se afirma haber validado nuevas conversaciones de Contenidos.

## Skills aplicadas

- **prompt-engineering-patterns:** ejemplos contrastivos y expectativas explícitas
  para distinguir definición, hecho nuevo, orden, hipótesis y autorización. El
  diagnóstico señaló atajos y composición de Django: no se alteró el prompt para
  compensarlos ni se atribuyó el resultado a mayor precisión del modelo.
- **memory-systems:** PostgreSQL como contexto vigente por reportante; conservación
  de modalidad causal, duración y respaldo sin mezclar hechos actuales con eventos
  históricos. No se añadió vector store ni se ampliaron filtros de identidad.
- **tool-design:** separar un resultado que necesita aclaración de uno listo para
  continuar; el formateador respeta la decisión del servicio y solo afirma efectos
  guardados. Ningún JSON de IA pasa a ser autoridad de escritura o permisos.
- **agent-regression-evals:** línea base previa, huellas congeladas, conjunto
  adicional declarado y comparación conservadora. Solo evaluación determinista de
  backend; no se ejecutaron sus scripts ni se presentaron estadísticas de ensayos
  variables de Gemini, que requieren una evaluación real separada y autorizada.

## Pruebas antes y después

| Comprobación | Antes de modificar producción | Candidata final |
|---|---|---|
| Evaluación v2 congelada | 26/26 casos, 48/48 tests | 26/26 casos, 48/48 tests |
| Regresiones relevantes previas | 308 aprobadas; 0 errores/omisiones | Las mismas etiquetas, ampliadas automáticamente con v2: **326 aprobadas**, 0 fallos/errores/omisiones |
| Regresiones conversacionales | v1: 17 aprobadas dentro de la línea base | **35 aprobadas: 17 v1 + 18 v2** |
| Comparación baseline/candidate | Baseline y fuentes conservadas | **PASS** del comparador existente |
| Intentos externos detectados | 0 | 0 |
| Django check aislado | Sin incidencias en las ejecuciones previas | Sin incidencias |

Se verificaron confirmación vinculada al resumen vigente, ninguna creación al
pedir explicación o corregir, entrega de resumen actualizado antes de registrar,
creación/notificación únicas, separación de reportantes, rechazo de actualizaciones
ambiguas, conservación de contexto, recuperación de mensaje y ausencia de reenvío
automático tras recuperarlo. Los mensajes de error Gemini/SMTP presentes en logs
son inyecciones de tests; no solicitudes ni envíos reales.

No se volvió a ejecutar toda la batería del repositorio. Las dos omisiones
históricas de entrega exactamente una vez en Telegram/SMTP no se seleccionaron ni
se cuentan como aprobadas. La comparación cubre exactamente las 48 regresiones
congeladas; las 18 nuevas son evidencia adicional y no se insertaron
retroactivamente en la línea base para mejorar una tasa.

Las huellas `corpus_sha256`, `contract_sha256`, `evaluation_sha256` y
`backend_tests_sha256` coinciden entre baseline y candidata. `agent_sha256`
cambia, como corresponde. No se relajaron controles para obtener PASS. Es una
comparación descriptiva de backend, no una prueba de significancia estadística
ni una certificación de producción.

## Prompts, llamadas y límites pendientes

El archivo del proveedor/prompt permanece idéntico. Para las mismas 16 sondas,
las longitudes siguen en **22.236–22.728 caracteres**, delta **0**. No son tokens
facturados ni mediciones de latencia. El contexto de una conversación puede
crecer legítimamente al conservar hechos nuevos que antes se perdían.

| Caso simulado | Extracciones antes → después |
|---|---|
| Respaldo y consulta de pendientes | 1 → 1 |
| Prioridad más incertidumbre causal | 0 → 1; interpretar la información completa evita descartarla |
| Definición con continuación neutral | 1 → 0 |
| Corrección de ubicación | 1 → 1 |
| Duración y evento del respaldo | 1 → 1 |
| Revisión del resumen | 1 → 1 |
| Definición más corrección efectiva | 1 → 1 |

No hay llamadas adicionales para redactar respuestas, nuevos reintentos ni cambio
de modelo/parámetros. Los fallos del proveedor en un mensaje mixto pueden aplazar
su procesamiento: se reutiliza la recuperación durable existente. No se aumentó
el presupuesto de tiempo para forzar respuestas.

Lo verificado es el comportamiento del backend con extracciones simuladas. No se
midieron comprensión, disponibilidad, naturalidad variable, costos o latencia
reales de Gemini. No se probó Telegram real. Las formas de continuación son
acotadas; expresiones fuera de esa gramática siguen el camino existente. Las
definiciones para redactar se identifican al comienzo del mensaje, sin prometer
comprensión universal de frases mixtas.

Siguiente iteración recomendada, sin gastos: ampliar contrastes offline de
aclaraciones, cambios de tema y mensajes agrupados, manteniendo el corpus
congelado y su cobertura adicional separada. Cualquier captura de IA real seguirá
pendiente de autorización expresa. No iniciar la arquitectura unificada ni una
migración de proveedor.

## Evidencias locales

Todos los artefactos siguientes están bajo `.qa/`, ignorado por Git, sin
credenciales ni datos operativos:

- `incident-conversation-v2-before/`: fuentes y manifiesto previos.
- `incident-conversation-v2-baseline-20261110.json`.
- `incident-conversation-v2-related-before-20261110.json`.
- `incident-conversation-v2-reproduction-final-before-20261110.json`.
- `incident-conversation-v2-guards-before.log`.
- `incident-conversation-v2-new-hypothesis-before.log`.
- `incident-conversation-v2-candidate-final-20261110.json`.
- `incident-conversation-v2-related-final-20261110.json`, con observaciones por escenario.
- `incident-conversation-v2-summary-final-20261110.json`, comparación, huellas y llamadas.

Los artefactos intermedios se conservaron sin sobrescribir. Solo los nombres
`candidate-final`, `related-final` y `summary-final` corresponden a la candidata
definitiva. No se hizo commit ni push; el HEAD continúa en `4a692ce`.

## Validación desde PowerShell

Desde la raíz del repositorio, sin iniciar servidor ni receptor:

```powershell
uv run --no-sync python manage.py test incidents.conversational_tests --settings=config.incident_evaluation --noinput
uv run --no-sync python manage.py check --settings=config.incident_evaluation
uv run --no-sync python -m incidents.evaluation compare --baseline .qa/incident-conversation-v2-baseline-20261110.json --candidate .qa/incident-conversation-v2-candidate-final-20261110.json
```

Mismas etiquetas de las regresiones previas y finales:

```powershell
$incidentV2Tests = @(
  'incidents.conversational_tests'
  'incidents.test_agent_evaluation'
  'incidents.test_natural_confirmation'
  'incidents.test_second_conversation27'
  'incidents.test_third_conversation28'
  'incidents.test_focused_conversation02'
  'incidents.test_duration_impact'
  'incidents.test_telegram_groups'
  'incidents.test_operational_regressions'
  'incidents.test_priority_flow'
  'incidents.test_guided_clarification'
  'incidents.tests.IncomingMessageServiceTests'
  'incidents.test_telegram_entry_security'
)
uv run --no-sync python manage.py test @incidentV2Tests --settings=config.incident_evaluation --noinput
```

Para otra ejecución del corpus con un archivo nuevo:

```powershell
$conversationV2Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
uv run --no-sync python manage.py evaluate_incidents --settings=config.incident_evaluation --agent-version conversation-v2-final --output ".qa/incident-conversation-v2-$conversationV2Stamp.json"
```

La base temporal `test_secuoya_incident_evaluation` se crea y destruye durante los
tests. Las ejecuciones de esta intervención tuvieron bloqueo de conexiones
externas y correo en memoria; se desactivó solo en el proceso el exportador de
evidencias históricas para no sobrescribirlas. No se iniciaron/detuvieron
receptores ni se modificaron incidentes, participantes, transferencias o bases
reales. Sin dependencias nuevas, scripts de terceros ni consumo de APIs.
