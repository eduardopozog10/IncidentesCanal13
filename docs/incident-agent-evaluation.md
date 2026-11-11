# Evaluación inicial del agente de Incidencias

Esta fase añade infraestructura de evaluación offline. No modifica prompts,
proveedores, modelos, reglas de negocio, Contenidos ni datos reales. Las skills
`agent-regression-evals`, `prompt-engineering-patterns` y `tool-design` se usaron
como referencias para separar contrato, interpretación, efectos y regresiones.
No se ejecutaron sus scripts ni se añadieron dependencias.

**Estado actual: corpus/evaluador v2.** La [auditoría de las 16 sondas](incident-gemini-probe-audit.md)
corrige contextos, distingue conservación del parser y añade adjudicación humana
para semántica libre. Los informes v1 siguen conservados, pero no son comparables
con v2; no se reetiquetan ni se reescriben.

## Cobertura reutilizada y vacíos

El corpus vincula **26 casos con 48 métodos de pruebas existentes**, que se
ejecutan sin copiarlos. Los fixtures, mensajes exactos, fallos inyectados y
aserciones ejecutables siguen en esos métodos. El JSON enlaza cada uno por su
identificador completo. No vuelve a implementar el procesador ni su memoria.

| Cobertura existente | Componentes y pruebas reutilizables |
|---|---|
| Extracción tipada, evidencia literal y límites del proveedor | `incidents/ai/gemini_provider.py`, `schemas.py`; `test_qa_extreme`, `test_recovery_limits`, `test_third_conversation28` |
| Borrador, prioridad explícita, corrección y validación | `services/incident_processor.py`, `conversation_plan.py`, `conversation_content.py`; `tests`, `test_guided_clarification`, `test_operational_regressions`, `test_conversation_data_quality` |
| Confirmación vinculada a resumen entregado y datos vigentes | `test_natural_confirmation`, `test_second_conversation27`, `test_focused_conversation02` |
| Contexto, consultas sin mutación, respaldo, incertidumbre y duración | `test_conversation_real27`, `test_second_conversation27`, `test_third_conversation28`, `test_duration_impact` |
| Identidad, autorización, agrupación y deduplicación | `services/incoming_message_service.py`, `telegram_message_groups.py`, `messaging/telegram_update_handler.py`; `test_telegram_entry_security`, `test_telegram_groups` |
| Recuperación, transacciones y honestidad de entrega | `test_pilot_recovery`, `test_recovery_limits`, `test_natural_confirmation`; `services/incident_notification_service.py` |

Las pruebas actuales ya preservan borradores/mensajes en PostgreSQL, vinculan
autorizaciones a un resumen entregado, aíslan reportantes y controlan efectos.
La nueva suite reutiliza esos mecanismos. El vacío principal era la ausencia
de un índice estable de casos y de informes comparables que separaran errores
de interpretación de los efectos evitados por Django.

Un proveedor simulado que devuelve los campos esperados comprueba el backend;
no demuestra que Gemini entienda el texto. Incluso un backend correcto puede
ocultar una mala interpretación: el caso 08 inyecta prioridad `critical` aunque
el usuario dice «alta, no crítica», y Django debe guardar `high`. La sonda
independiente del mismo caso califica como incorrecto ese `critical` original.

Persisten límites de cobertura: lenguaje abierto, variabilidad real del modelo,
naturalidad de respuestas, carga con IA real y entrega remota no se miden aquí.
Las consultas del corpus consultan el borrador; no se inventa una nueva intención
para consultar incidentes operativos por código. La conversación larga reutiliza
50 saludos/agradecimientos: no prueba 50 correcciones semánticas distintas.

## Corpus y procedencia

Archivo versionable: `incidents/evaluation/corpus.json`. Cada caso incluye ID,
objetivo, contexto inicial, mensajes, resultado esperado, operaciones permitidas
y prohibidas, criticidad, decisión esperada, evidencia y métodos ejecutables.
Los textos de las **16 sondas del modelo son reconstrucciones sintéticas**;
no son transcripciones completas ni nuevas pruebas reales. Solo los casos con
evidencia `documented_historical_defect` se atribuyen a un defecto histórico
documentado; los demás provienen de regresiones/reglas, sin afirmar observación
real. Los informes históricos `docs/qa/` están ignorados por Git: las pruebas y
este documento conservan la trazabilidad mínima sin exigir esos informes al clonar.

| ID INC-EVAL | Caso | Control esperado |
|---|---|---|
| 01 | Creación completa, prioridad Alta | Resumen entregado y autorización; creación y aviso únicos |
| 02–04 | Prioridades Baja, Media y Crítica | Cambio explícito; nuevo resumen antes de confirmar |
| 05 | Urgencia sin prioridad | No deducir prioridad; pedir elección |
| 06 | Área desconocida | Orientar y conservar área null |
| 07 | Pregunta de aclaración | Explicar prioridad sin decidir |
| 08 | Correcciones múltiples | Equipo/lugar/prioridad correctos; conservar otros campos |
| 09 | Duración y evento auxiliar | Separar 35 minutos de falla y 5 de conexión |
| 10 | Respaldo | Conservar disponibilidad/modalidad tras cambiar prioridad |
| 11 | Impacto y consulta | Aplicar impacto; preservar descripción; no registrar |
| 12 | Reescritura | Conservar hechos e hipótesis; responder solo descripción |
| 13 | Confirmación con typo | Confirmación local inequívoca y creación única |
| 14–15 | Ambigüedad y tercero | No crear aunque IA sugiera confirmar |
| 16 | Consulta de estado | Datos/estado/pregunta/fecha idénticos; sin IA |
| 17 | Equipo frente a servicio | Consola pertenece a Audio aunque esté en streaming |
| 18 | Síntoma | Intermitencia frente a corte total, sin diagnóstico inventado |
| 19 | Varios mensajes | Orden, originales, prioridad rectificada y respuesta única |
| 20 | Cambio de contexto | No sustituir borrador ni cruzar reportantes |
| 21 | Conversación larga | Conservar contexto y limitar extracciones |
| 22 | Acceso no autorizado | Rechazar antes de persistencia/IA/contacto |
| 23 | Duplicación | Reentrega no repite extracción/respuesta |
| 24 | Fallo IA y reinicio | Texto durable, pausa y presupuesto, recuperación controlada |
| 25 | Persistencia | Rollback honesto y recuperación sin duplicación tras commit |
| 26 | Notificaciones | Incidencia conservada; entrega incierta no se reintenta sola |

Fuentes históricas utilizadas: tercera intervención del 28/10 (respaldo,
impacto, reescritura y 503); corrección de orientación/confirmación del 02/11
(QA-REAL-011 y separación consulta/ayuda); corrección de duración/impacto del
02/11. No se retransmiten conversaciones antiguas ni identificadores reales.

## Dos capas de evaluación

**Backend determinista.** `evaluate_incidents` selecciona los 48 tests existentes,
los ejecuta en PostgreSQL temporal y genera un resultado por caso. Un caso pasa
solo si pasan todos sus tests; omitido, no ejecutado, fallo o intento externo
impiden aprobarlo. Guarda IDs y diagnósticos de las aserciones fallidas. Las
expectativas de operaciones prohibidas se verifican mediante las aserciones de
esos tests, no mediante un agente juez ni textos de una supuesta trayectoria.

`diagnostic_area` distingue contexto, confirmación, persistencia, notificación,
validación, recuperación e interpretación. **Orienta la investigación, no
declara automáticamente la causa raíz**: un fallo de fixture también puede hacer
fallar un test. `expected_decision` distingue rechazos legítimos de permisos,
validación o confirmación. Que Django rechace legítimamente no es un fallo de IA.
`interpretation_correct` es null en esta capa.

**Modelo variable.** El comando offline `grade` lee grabaciones JSONL aportadas
posteriormente; **no hace llamadas a Gemini**. Comprueba el esquema real y usa
el parser productivo sin construir cliente. Califica campos crudos ANTES de que
la normalización/backend oculten errores, evidencia literal y restricciones del
resultado normalizado. En v2 las comparaciones sobre `normalized` se informan
aparte: el fallback del parser no se atribuye a memoria del modelo. La semántica
libre requiere revisión humana identificada y vinculada al hash de respuesta;
mientras falte, el caso no aprueba ni se declara incorrecto solo por usar
sinónimos. Una revisión no puede ocultar un fallo automático. Es una rúbrica
parcial, no una garantía sobre todos los matices. Las 16 sondas cubren extracciones relevantes;
los caminos deterministas sin IA no reciben una exactitud Gemini inventada.

Clasifica separadamente interpretación correcta/incorrecta, JSON/esquema inválido,
HTTP 500/503/429, autenticación 401, autorización del proveedor 403 y otros fallos. Publica
conteos, exactitud de rúbrica sobre respuestas válidas y validez sobre respuestas
recibidas; no publica exactitud agregada mientras haya revisiones pendientes.
Errores externos no se convierten en fallos semánticos. Un informe
parcial indica casos faltantes y no aprueba la cobertura completa. Los tiempos
del modelo son los declarados por la grabación, no los del replay local.

## Ejecución segura en PowerShell

Desde la raíz del repositorio:

```powershell
New-Item -ItemType Directory -Force .qa | Out-Null
$evalStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
uv run --no-sync python manage.py test incidents.test_agent_evaluation --settings=config.incident_evaluation --noinput
uv run --no-sync python manage.py evaluate_incidents --settings=config.incident_evaluation --agent-version baseline --output ".qa/incidents-baseline-$evalStamp.json"
uv run --no-sync python manage.py check --settings=config.incident_evaluation
```

El settings exclusivo deja como origen `secuoya_evaluation_unused` y obliga a
usar **`test_secuoya_incident_evaluation`**, que Django crea, migra y destruye.
No reutiliza la base QA persistente, la demo ni la operativa. El comando rechaza
otros settings y no ofrece opciones para seleccionar arbitrariamente una base.
Si otro proceso usa esa misma base de tests, esperar a que termine; no ejecutar
dos suites con ese nombre simultáneamente.

Credenciales Gemini/Telegram quedan vacías solo en este proceso; IA mock y correo
locmem. Un guard bloquea sockets Python/SMTP reales; HTTPX MockTransport continúa
en memoria y PostgreSQL usa libpq. Un intento bloqueado se registra aunque lo
capture el servicio. No es una sandbox general para ejecutar código no confiable;
solo ejecuta regresiones locales conocidas. Desactiva temporalmente exportaciones
históricas controladas por `SECUOYA_QA_EVIDENCE`, sin cambiar `.env`.

Para repetir el backend o comparar una futura versión candidata:

```powershell
uv run --no-sync python manage.py evaluate_incidents --settings=config.incident_evaluation --agent-version candidate --output ".qa/incidents-candidate-$evalStamp.json"
uv run --no-sync python -m incidents.evaluation compare --baseline ".qa/incidents-baseline-$evalStamp.json" --candidate ".qa/incidents-candidate-$evalStamp.json"
```

Los informes incluyen hashes del corpus, esquema, fuentes del agente, evaluador
y pruebas. Se rechaza comparar corpus/contratos/evaluadores/pruebas distintos
(para evitar aprobar debilitando aserciones), casos faltantes/desconocidos, trials duplicados
o no pareados y cualquier fallo candidato. Fallos altos/críticos bloquean incluso
si también fallaban antes. No se añade cuarentena ni se borran casos para aprobar.
PASS significa que pasan estas reglas; no certifica producción. La comparación
es descriptiva, sin afirmaciones de significancia estadística para muestras pequeñas.

Para Gemini se exigirán **al menos tres trials únicos por sonda y por versión**,
con el mismo modelo, perfil de captura y procedencia; backend determinista admite un trial.
No se comparan automáticamente resultados sintéticos con reales. Mantener el
modelo/parámetros fijos al comparar cambios de prompt. Un cambio de contrato
requiere revisar el corpus y generar otra línea base.

## Grabaciones futuras: formato y uso offline

Una línea JSON por sonda/trial, con `case_id`, entero `trial >= 1`,
`source: "gemini_recording"` o `"synthetic"`, `model`, opcional `duration_ms` y
exactamente uno de `response` o `error`. `response` es el objeto JSON original
completo o su texto, nunca solo el resultado ya corregido por Django. Para fallos:

```json
{"case_id":"INC-EVAL-01","trial":1,"source":"synthetic","model":"test-only","duration_ms":12,"error":{"http_status":503}}
```

Este ejemplo es **sintético**, no un 503 observado. El índice `message_index`
(o `message_indices` para el grupo 19) y `previous_data` fijan la entrada mediante
`probe_message`. Las grabaciones reales requieren además el manifiesto `capture`
descrito en la auditoría v2; no basta con etiquetar una respuesta como real.

```powershell
uv run --no-sync python -m incidents.evaluation grade --input .qa/recordings.jsonl --output .qa/model-baseline.json --agent-version baseline
```

El comando no captura grabaciones, no verifica criptográficamente su procedencia
declarada y no incluye un ejecutor de Gemini real. Eso requiere una fase posterior
autorizada, presupuesto y sanitización. Los informes son evidencia local confiable
del equipo, no entradas remotas autenticadas. Salidas nuevas exclusivamente: no
sobrescribe archivos. Códigos CLI: 0 aprobado, 2 incompleto/fallido/bloqueado,
3 entrada inválida. El comando Django devuelve error ante suite fallida.

## Próximo paso recomendado

Revisar y aprobar las 16 rúbricas antes de capturar una línea base variable real
con Gemini actual, presupuesto acotado y sin Telegram ni escrituras operativas.
Son 48 extracciones lógicas para tres trials por sonda. El proveedor actual permite
dos intentos por extracción: el techo es 96 intentos HTTP generativos, más cualquier
conteo de tokens que se autorice por separado. Todavía no ejecutar esa captura. Solo después de medir
fallos de interpretación conviene elegir una mejora concreta del prompt, empezando
por distinguir ayuda/consulta/actualización y conservación de modalidad del respaldo.
Las decisiones, permisos, confirmación y persistencia continúan en Django.

## Archivos añadidos y validación de esta fase

| Archivo | Propósito |
|---|---|
| `incidents/evaluation/corpus.json` | 26 casos, 16 sondas y 48 vínculos a regresiones |
| `incidents/evaluation/core.py` | Carga, esquema, rúbricas, hashes y comparación |
| `incidents/evaluation/runner.py` | Ejecución de pruebas existentes y bloqueo de efectos externos |
| `incidents/evaluation/__main__.py` | CLI offline grade/compare |
| `incidents/evaluation/__init__.py` | Paquete aislado del procesamiento productivo |
| `incidents/management/commands/evaluate_incidents.py` | Comando Django protegido para evaluar backend |
| `config/incident_evaluation.py` | Configuración exclusiva de tests |
| `incidents/test_agent_evaluation.py` | 26 pruebas de la infraestructura y sus rechazos |
| `docs/incident-agent-evaluation.md` | Informe, límites e instrucciones |

Validación local del 10/11/2026: **26 pruebas nuevas aprobadas**. Dos ejecuciones
finales de la misma suite: **48/48 pruebas y 26/26 casos aprobados en cada una**,
cero omisiones y cero intentos externos detectados. Django check: sin incidencias.
Se comprobó también el CLI grade con una respuesta sintética: un caso calificado
y quince sondas faltantes; informó cobertura incompleta, sin presentarlo como
evaluación real del modelo.

Informes finales locales (ignorados por Git):
`.qa/incident-evaluation-v1-baseline-20261110.json` y
`.qa/incident-evaluation-v1-repeat-20261110.json`. Se conservan los artefactos
intermedios sin sobrescribirlos; solo el par v1 corresponde al evaluador final.
No se volvió a ejecutar la regresión completa del repositorio. Las dos omisiones
históricas de `test_pilot_recovery.PendingDeliveryGuarantees` siguen documentando
la falta de garantía de entrega exactamente una vez ante pérdida de acuse
Telegram/SMTP; no están seleccionadas ni se contabilizan como aprobadas aquí.
