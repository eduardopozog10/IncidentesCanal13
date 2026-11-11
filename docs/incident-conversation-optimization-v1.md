# Optimización conversacional v1 — Incidencias Secuoya Chile

Primera candidata local del 10/11/2026, rama `feature/coordinacion-contenidos-demo`.
Se corrigieron dos problemas reproducidos en el enrutamiento determinista de
Django. No se modificó el prompt ni se hizo ninguna llamada real a Gemini.
Los resultados de esta fase demuestran cambios del backend con extracción y
transporte simulados; no una mejora medida de comprensión del modelo.

## Diagnóstico comprobado

| Área solicitada | Resultado de la inspección y pruebas |
|---|---|
| Preguntas frente a órdenes | Defecto reproducido: «¿Podrías corregir la prioridad a crítica?» era una consulta local y respondía Alta. Además, el extractor de prioridad eliminaba íntegramente el texto entre interrogantes. |
| Correcciones | El merge, la evidencia literal y la persistencia ya estaban implementados. La petición cortés no alcanzaba esos controles. Se corrigió el acceso al camino existente; no se añadieron escrituras directas. |
| Conservación de hechos | PostgreSQL, evidencia por campo, facetas e historial ya resuelven la conservación. Se verificó que la corrección conserva hipótesis y respaldo, incluso al reconstruir el handler simulado. No se añadió memoria. |
| Aclaraciones innecesarias | Defecto reproducido: «¿Qué significa prioridad?» devolvía el valor guardado; «¿Qué es el impacto operacional?» respondía No informado. Sin borrador, la definición de prioridad llegaba innecesariamente a IA. |
| Naturalidad | La explicación ahora responde el concepto solicitado sin volver a pedir un dato ya elegido. Una categoría concreta recibe solo su definición. No se rediseñó el resto de las respuestas. |
| Confirmación ambigua | Las guardas existentes rechazan preguntas, hipótesis, terceros y cambios pendientes como autorización de creación. Se mantuvieron intactas y se verificó su interacción con la nueva ayuda. |
| Varias intenciones | La planificación update/query y agrupación ya existían. Se añadió cobertura de petición cortés más consulta, incluida una secuencia agrupada, sin perder la rectificación. |
| Cambios de tema | Ya existen conservación de borrador, consultas guardadas, separación de reportantes y detección de otra incidencia. No se encontró una razón para reemplazarlos ni añadir infraestructura. |

La reproducción inicial tuvo **6 tests: 2 aprobados y 4 fallidos**. Los cuatro
fallos pertenecían a los dos defectos anteriores, no a cuatro fallos reales de
Gemini. Se guardó el log antes de corregir. Los mismos seis pasan con la candidata;
la cobertura final se amplió a **17 tests nuevos**.

## Correcciones pequeñas y reversibles

1. Un helper exclusivo de Incidencias distingue la lectura de datos guardados de
   una petición cortés directa de modificar el borrador. Las formas reconocidas
   pasan por la interpretación, merge, evidencia y validaciones existentes.
2. El validador de prioridad reconoce el pedido explícito «puedes/podrías
   cambiar/corregir ... prioridad a/por ...» antes de excluir preguntas
   informativas. Sigue exigiendo una prioridad única y explícita, conserva el
   tratamiento de negaciones y no acepta como decisión suficiente el valor de IA.
3. Las peticiones completas de definición de prioridad o impacto se responden
   localmente como ayuda. No modifican extracted_data, status, last_question ni
   updated_at del borrador, ni crean uno cuando no existe. Solo se persiste el
   mensaje y su respuesta mediante el mecanismo habitual.
4. Se reutilizaron las definiciones operacionales existentes, separándolas de la
   pregunta para completar el reporte. `PRIORITY_GUIDANCE` y `GUIDED_QUESTIONS`
   anteriores conservan exactamente su contenido; no cambian las reglas de
   prioridad ni la selección explícita por el reportante.

No se modifica una incidencia ya creada. La corrección sigue dirigida al borrador
activo del reportante autorizado. La nueva clasificación no autoriza registro,
cancelación, envío ni acceso a otros reportantes.

## Técnicas pertinentes de las skills

- **prompt-engineering-patterns:** pares contrastivos y casos adversarios para
  separar «¿qué prioridad quedó?» de «¿podrías corregirla?», definiciones de datos
  guardados y modificaciones de confirmaciones. Se eligió corregir el enrutador
  demostrado antes de alterar un prompt sin una línea base real de Gemini.
- **memory-systems:** fuente de verdad relacional, contexto por reportante y
  conservación de hechos vigentes/modalidad. La consulta tras reconstruir el
  handler usa PostgreSQL, no memoria del proceso ni un almacén nuevo. No se
  eliminan filtros de identidad ni se adopta búsqueda vectorial.
- **tool-design:** funciones con responsabilidades acotadas: distinguir lectura,
  explicación y petición; devolver el control al servicio autorizado. Se separa
  la intención del texto de su ejecución; el modelo no decide persistencia ni
  permisos. No se exponen herramientas de shell/SQL al chatbot.

Se leyeron los tres SKILL.md y no se ejecutaron sus scripts, ni se instalaron
dependencias. No se añadió agente unificado ni otro proveedor.

## Archivos de esta intervención

| Archivo | Cambio |
|---|---|
| `incidents/services/incident_processor.py` | Integra lectura/ayuda/petición; reutiliza definiciones y conserva la validación de prioridad explícita. |
| `incidents/services/incident_dialogue.py` | Nuevo helper local sin SQL, red ni ejecución de operaciones. |
| `incidents/conversational_tests/__init__.py` | Paquete de regresiones adicionales. |
| `incidents/conversational_tests/test_optimization_v1.py` | 17 pruebas nuevas con PostgreSQL temporal, proveedores simulados y bloqueo de conexiones externas. |
| `docs/incident-conversation-optimization-v1.md` | Este informe. |

Los archivos de evaluación ya existentes y otros cambios locales se conservaron.
No se editaron corpus, calificador, comparador, contrato ni las 48 pruebas
congeladas. Las regresiones nuevas son un conjunto adicional, separado de la
medición v2 antes/después; no se incluyeron a posteriori como si hubieran formado
parte de la línea base.

## Resultados antes/después e integridad

| Medición | Antes | Candidata |
|---|---|---|
| Corpus v2 congelado | 26/26 casos; 48/48 tests | 26/26 casos; 48/48 tests |
| Seis reproducciones iniciales | 2 aprobadas, 4 fallidas | 6 aprobadas |
| Regresiones adicionales de esta fase | No se atribuye un resultado anterior no ejecutado | 17/17 aprobadas |
| Comparación con el evaluador existente | Baseline conservada | **PASS**, sin regresiones en el conjunto congelado |
| Regresiones relacionadas y evaluación | No se afirma una nueva ejecución histórica | **308 aprobadas**, 0 fallos/errores/omisiones, 0 intentos externos |
| Django check aislado | Correcto en la línea base | Sin incidencias |

Se compararon los JSON originales antes/después con el comparador existente.
Permanecen iguales `corpus_sha256`, `contract_sha256`, `evaluation_sha256` y
`backend_tests_sha256`. Cambia `agent_sha256`, como corresponde a una candidata.
No se relajaron huellas ni se editó un resultado para conseguir PASS.

Las 308 pruebas incluyen las 17 nuevas, las 41 del evaluador y regresiones de
confirmación natural, correcciones, segunda/tercera intervención, orientación,
duración, impacto, agrupación, identidad y procesamiento original. No se repitió
toda la batería del repositorio. Las dos omisiones históricas sobre entrega
exactamente una vez de Telegram/SMTP no se seleccionaron ni se cuentan como
aprobadas. Los errores SMTP del log son inyecciones de pruebas, no envíos reales.

Evidencias locales conservadas bajo `.qa/` (ignoradas por Git):

- `incident-conversation-v1-before/`: fuentes anteriores y manifiesto con longitudes.
- `incident-conversation-v1-baseline-20261110.json`.
- `incident-conversation-v1-reproduction-before.log`.
- `incident-conversation-v1-candidate-20261110.json`.
- `incident-conversation-v1-related-20261110.json`.
- `incident-conversation-v1-summary-20261110.json`.

Los logs intermedios de construcción de tests no sustituyen el resultado final.
Una expectativa del test de timeout se ajustó para admitir `_pending_messages`,
que es conservación legítima ya existente; se sigue exigiendo que todos los
hechos previos permanezcan iguales y que el mensaje pendiente no se pierda.

## Prompt, llamadas y componentes compartidos

- `gemini_provider.py` permanece idéntico al snapshot previo. Las 16 sondas
  producen exactamente las mismas longitudes: **22.236–22.728 caracteres**,
  **delta 0**. No son conteos de tokens ni latencias medidas de Gemini.
- Definiciones con borrador: 0 llamadas a IA antes y después, ahora con respuesta
  correcta al concepto. Definición sin borrador: se evita el camino de IA que
  antes podía tomarse; la prueba verifica cero llamadas después.
- Corrección cortés inequívoca: antes se ignoraba con 0 llamadas; ahora alcanza
  el camino normal, con 1 extracción simulada por mensaje/grupo. El presupuesto
  de reintentos/deadline del proveedor no cambia; no se hacen llamadas extra para
  mejorar la redacción ni para confirmar localmente.
- `conversation_content.py` compartido permanece byte a byte igual. Contenidos
  importa de él `direct_action_text` y `natural_confirmation`, no el nuevo helper
  ni IncidentProcessor. No se modificaron esos contratos ni Contenidos. No se
  reejecutó su suite completa; se verificó el aislamiento estático del cambio.

## Qué se verificó y qué queda pendiente

Comprobado con PostgreSQL temporal y proveedores simulados: petición de corrección
frente a consulta; definiciones sin repreguntar; contexto y otro reportante
intactos; orden de mensajes agrupados; recuperación sin repetir entrada; datos
persistidos tras reconstruir handler; creación única solo después de resumen
vigente entregado y confirmación; explicación conceptual no cuenta como resumen.

No se midió disponibilidad, exactitud lingüística, latencia, costo ni naturalidad
variable de Gemini. Los detectores son deliberadamente acotados; expresiones
mixtas más complejas o paráfrasis fuera de sus formas siguen usando los caminos
existentes y requieren futuras pruebas. No se promete comprensión universal.
Tampoco se probó presentación real en Telegram durante esta fase.

Siguiente mejora recomendada: autorizar una captura real acotada después de
revisar estas dos distinciones y el diseño de captura ya documentado, incluyendo
ejemplos de consulta/petición con el mismo contenido factual. Mantener el corpus
v2 congelado para su comparación y tratar estos nuevos escenarios como un
conjunto adicional declarado. Solo con evidencia real decidir si hace falta
optimizar el prompt. No añadir memoria ni infraestructura mientras no haya un
fallo de contexto no resuelto por los servicios actuales.

## Comandos PowerShell

Desde la raíz del repositorio, sin iniciar Django server ni receptor:

```powershell
uv run --no-sync python manage.py test incidents.conversational_tests incidents.test_agent_evaluation --settings=config.incident_evaluation --noinput
uv run --no-sync python manage.py check --settings=config.incident_evaluation

uv run --no-sync python -m incidents.evaluation compare --baseline .qa/incident-conversation-v1-baseline-20261110.json --candidate .qa/incident-conversation-v1-candidate-20261110.json
```

Para generar otra ejecución offline nueva, usando un destino que no exista:

```powershell
$conversationStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
uv run --no-sync python manage.py evaluate_incidents --settings=config.incident_evaluation --agent-version conversation-v1-candidate --output ".qa/incident-conversation-v1-$conversationStamp.json"
```

La base temporal es `test_secuoya_incident_evaluation`; se crea y destruye durante
los tests. No se accedió a bases reales, no se enviaron mensajes/correos reales,
no se consumieron servicios externos ni se tocaron procesos del receptor.
No se hicieron commit/push ni cambios en `.env`, credenciales o vínculos Telegram.
