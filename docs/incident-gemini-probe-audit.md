# Auditoría de sondas y propuesta de línea base Gemini — 10/11/2026

La captura real **no está autorizada ni se ejecutó**. Se comprobó localmente
`AI_PROVIDER=gemini` y modelo efectivo `gemini-3.5-flash-lite`, sin construir
clientes, usar Telegram/SMTP o conectar bases. No se alteró el prompt productivo.
Las tres skills se usaron como referencias; no se ejecutó código de terceros.

## Resultado individual de las 16 sondas

Son sondas de extracción aislada, no 16 conversaciones end-to-end. Su persistencia,
permisos, confirmaciones y entregas siguen cubiertos por los 48 tests de backend.
No se concede autoridad al JSON del modelo. Todas las sondas incluyen ahora una
revisión humana de semántica libre; los enums/hechos explícitos siguen teniendo
comprobaciones automáticas. Los IDs se conservan; corpus y formato pasan a v2.

| Sonda | Diagnóstico de v1 | Ajuste v2 y criterio válido |
|---|---|---|
| INC-EVAL-01 | «sin imagen intermitente» era poco claro. Igualdad literal en nombres; faltaban ubicación y evidencia. El título sugería medir creación con IA. | Texto inequívoco de pérdida intermitente; área/equipo/lugar toleran mayúsculas, acentos y espacios, sin cambiar IDs. Familia/prioridad mantienen códigos. Evidencia literal obligatoria. Revisión de síntoma/impacto/no invención; crear sigue siendo prueba de Django. |
| INC-EVAL-02 | Baja correcta, pero un resultado podía cambiar otros hechos o interpretar «sí, pero» como permiso. | `low`, acción update y evidencia actual. Revisar conservación de hechos. `authorization=null/none` o confirm condicionado por cambios es admisible; autorización incondicional no. El fallback normalizado no cuenta como memoria del modelo. |
| INC-EVAL-03 | Mismo vacío para Media. | `medium`, update y evidencia; mismos límites de conservación y autorización. La definición de prioridad no se usa para decidir por el usuario. |
| INC-EVAL-04 | Mismo vacío para Crítica. | `critical`, update y evidencia; no crear con el «sí» de una corrección. No modificar parámetros del modelo. |
| INC-EVAL-05 | Un JSON vacío con priority=null podía superar la rúbrica pese a no extraer nada útil. | Prioridad null, pero también intención de reporte, Cámara 3, Móvil 2, familia y evidencia. Revisar síntoma, dato faltante y ausencia de área/impacto inventados. El rechazo por incompletitud pertenece a Django. |
| INC-EVAL-06 | Solo medía help/área null; podía perder los hechos de la consola. | update+help, help_field=area, evidencia de equipo/prioridad. Revisión de consola/Estudio B/cortes/continuidad parcial y no selección arbitraria de Audio o Producción. |
| INC-EVAL-07 | Priority=null por sí solo aprobaba incluso una respuesta sin orientación. | help sobre priority y null. Revisión del acto de explicación y fidelidad si emite texto. No exigir redacción exacta ni atribuir a Gemini el menú final de Django. |
| INC-EVAL-08 | Decía Cámara 3/Móvil 2/Crítica, pero previous_data era Cámara 2/Estudio A/Alta. | Contexto corregido y coherente. Cámara 4/Estudio 8/high, citas actuales, update; revisar conservación de síntoma/impacto/hipótesis. La revalidación local de prioridad negada sigue en Django. |
| INC-EVAL-09 | Buscar «35 minutos» y vetar «20 minutos» confundía palabras/unidades equivalentes o antecedentes con estado vigente. No distinguía conexión del respaldo. | update+query/time y revisión temporal: 35 minutos actuales, 20 solo antecedente, 5 desde conexión. Conservar hipótesis y no inferir reanudación. Sin clasificador semántico nuevo ni LLM juez. |
| INC-EVAL-10 | normalized.operational_impact podía aprobar gracias al fallback con JSON null; parecía memoria de Gemini. | Prioridad/evidencia/update automáticos; no emitir operational_updates ajenos. Revisión permite null o repetición fiel de respaldo sin usar. El fallback se identifica como parser/Django. |
| INC-EVAL-11 | El impacto anterior ya decía lo mismo: no probaba corrección. Exigía dos substrings aunque el contrato admite facetas operacionales. | Estado previo parcial/con respaldo en uso, realmente distinto del nuevo detenido/sin conectar. update+query/impacto; revisar texto o facetas equivalentes. Comparación de descripción normalizada es control separado del parser, no exactitud cruda. |
| INC-EVAL-12 | Una diferencia inocua de punto final fallaba en rewrite_clauses; síntoma previo no coincidía con rayas. | Contexto con rayas. Multiconjunto de cláusulas sin diferencias de punto final/caja/acentos/espacios, manteniendo negaciones, números y cláusulas únicas; se permite reordenar. Revisión de restricciones sobre otros campos. La prohibición de parafrasear cláusulas proviene del contrato actual. |
| INC-EVAL-17 | Consola en streaming heredaba una descripción/síntoma de cámara y unit=Estudio A. «conex» rechazaba «no logra enlazarse». | Contexto previo de Audio/Estudio de streaming, equipo/síntoma/impacto/prioridad pendientes. Audio como familia, consola como equipo; revisar síntoma equivalente sin inferir impacto ni prioridad. |
| INC-EVAL-18 | Equipo Cámara 3, descripción Cámara 2; exigir «intermit» rechazaba «se corta y vuelve». | Descripción previa coherente con Cámara 3 y sin imagen. Exigir síntoma no vacío y evidencia; revisar intermitencia con formulación libre, no diagnóstico ni ausencia permanente actual. |
| INC-EVAL-19 | Sin borrador, pero inyectaba uno completo y solo enviaba «Corrijo la prioridad». No medía agrupación. | previous_data vacío; `message_indices=[0,1]`; envoltorio ordenado idéntico al de `TelegramMessageGroups.process_reporter`. Extraer datos de ambos mensajes y última prioridad crítica. Recepción individual, orden durable y dedup siguen siendo pruebas de backend. |
| INC-EVAL-20 | Contexto declarado Cámara 3/Estudio B, real Cámara 2/Estudio A; solo comprobaba new_incident. | Contexto corregido, new_incident más Router 4/Redes/networks y evidencia. Revisar que no herede lugar/impacto/prioridad de la cámara. No calificar como fallo de Gemini que Django conserve el borrador original o rechace operaciones ajenas. |

Los textos son sintéticos y no se presentan como resultados reales nuevos. Para
nombres de equipo con otra paráfrasis válida fuera de los alias aceptados, revisar
la rúbrica antes de declarar un defecto: el corpus v2 sigue siendo una muestra
acotada, no un evaluador universal de español. Los enums `high`, `critical`, etc.
mantienen igualdad exacta porque el contrato productivo exige esos códigos.

## Debilidades del evaluador corregidas

1. **Falsas aprobaciones por rúbrica mínima:** priority=null o un solo substring
   no prueban una extracción correcta. Se ampliaron hechos/citas y revisión.
2. **Semántica libre tratada como coincidencia textual:** ahora produce
   `manual_review_required`, con interpretación null, hasta adjudicación humana.
   Enums/IDs seguros siguen siendo automáticos; una revisión no anula sus fallos.
3. **Memoria del modelo confundida con fallback:** `normalized` informa una
   dimensión distinta. Puede haber interpretación correcta y
   `normalization_mismatch`; no se atribuye automáticamente una causa raíz.
4. **Creación sugerida indebidamente:** ninguna sonda autoriza una creación
   incondicional. Se detecta ese plan, pero se admite confirm con cambios pendientes;
   no se replica ni reemplaza la autorización final de Django.
5. **Procedencia insuficiente:** antes `--agent-version` reetiquetaba grabaciones
   con hashes de las fuentes actuales sin demostrar qué entrada se envió.
   Las capturas reales requieren ahora versión, hashes de corpus/contrato/entrada,
   agente, perfil, run y SDK. No mezclar fuentes/modelos/runs/perfiles en un informe.
6. **Comparaciones engañosas:** se bloquea PASS contradictorio con error de
   proveedor, esquema inválido, checks fallidos o revisión pendiente; también
   cambios de perfil. Se conservan las guardas de corpus/evaluador/tests y trials
   pareados. Los hashes detectan deriva; no son firma ni autorización de ejecución.
   Una revisión pendiente o una caída del proveedor bloquean, pero se listan
   como `pending_reviews`/`provider_failures`, no como regresión semántica crítica
   confirmada. Cubrir 16 casos una vez tampoco completa el muestreo mínimo de tres.
7. **Entradas mal formadas:** se distingue presencia de error de su truthiness,
   se rechazan error+response, errores vacíos y JSON NaN. 401 y 403 se distinguen
   como autenticación y autorización del proveedor, sin confundir permisos Django.

La comparación sigue siendo conservadora y descriptiva: no estima significancia
estadística con 16 sondas, no verifica tarifas/latencia y no certifica producción.
La exactitud agregada queda null mientras haya revisión humana pendiente; los
errores de disponibilidad no se convierten en errores de comprensión.

## Etapas separadas

| Etapa | Estado y alcance |
|---|---|
| A. Offline | Implementada. 26 casos/48 regresiones y pruebas del evaluador; no proveedor real. |
| B. Captura | Solo propuesta en este informe. **No hay todavía subcomando capture implementado.** Requiere autorización y un ejecutor acotado, probado con transporte en memoria. |
| C. Calificación | Implementada offline: esquema/parser real, rúbricas crudas, normalización separada y revisiones humanas. Nunca llama a Gemini. |
| D. Comparación candidata | Implementada: mismo corpus/evaluador/tests/contrato y perfil, tres trials únicos por sonda, sin revisiones pendientes; candidato con fallos no aprueba. |

## Propuesta segura de captura B

- Conservar proveedor, modelo efectivo, `_build_prompt` y RESPONSE_SCHEMA actuales.
  No invocar IncidentProcessor, modelos ORM, Django.setup, receptores, workers ni
  notificaciones. Usar solo mensajes/contextos sintéticos de `probe_message`.
- Congelar antes de llamar: corpus, versión de código, SDK, esquema, prompt hash
  por sonda y perfil de solicitud. El perfil registra parámetros **no definidos**
  como tales: hoy no hay temperatura, thinking ni max_output_tokens explícitos.
  No sustituir los defaults por valores inventados.
- Usar el cliente/reintentos/deadline existentes, instrumentando cada intento
  antes del parser. No repetir la solicitud para extraer el JSON. Guardar
  `output_text` original íntegro incluso cuando el parser falle, usage, tiempos,
  modelo devuelto si existe y errores allowlist: clase, HTTP y retry_after
  sanitizado. No guardar API keys, headers, URLs privadas ni repr/str completos de
  excepciones/clientes. Mantener logs HTTP desactivados.
- 16 sondas × 3 repeticiones = **48 extracciones lógicas**. Hasta dos intentos
  existentes por extracción: **96 intentos HTTP generativos como máximo**, SDK
  retries deshabilitados como en producción. No añadir reintentos exteriores.
- Secuencial, concurrencia 1, espaciado propuesto 2 s. Comenzar por sondas 01/06
  trial 1 como piloto, incluidas en el total, no llamadas adicionales de salud.
  Ante 500/503, 429, 401/403, timeout o interrupción: conservar fila/intentado y
  detener nuevas extracciones después de que termine el presupuesto existente.
  En 429 registrar retry_after, sin forzar una nueva ejecución ni ignorarlo.
- Límite total propuesto 2.700 s (45 min). Una extracción tiene como máximo 45 s;
  no iniciar otra si queda menos de ese presupuesto. El primer intento conserva
  hasta 30 s. No aumentar reintentos/deadlines para completar el corpus a toda costa.
- Ctrl+C o archivo STOP: detener planificación y cerrar/guardar el intento actual
  con estado interrumpido/resultado remoto desconocido si corresponde. Un timeout
  local no cancela ni desfactura una generación remota. No relanzar automáticamente
  ese par case/trial. Persistir append-only con flush por intento y run nuevo para
  un reintento autorizado posterior; no reemplazar fallos por simulaciones.
- Manifiesto separado del JSONL con filas planeadas/terminadas/no ejecutadas,
  presupuesto reservado/consumido/desconocido y motivo de parada. Archivos nuevos
  bajo `.qa/`; no sobrescribir los informes anteriores. Un cierre incompleto no
  aprueba cobertura ni se rellena con respuestas ficticias.

El proveedor actual usa Interactions, que almacena peticiones por defecto según
Google. La propuesta transmite solo material sintético; no extrae conversaciones
QA/reales. Si se quiere `store=false`, debe acordarse y registrarse como diferencia
de transporte de captura, sin cambiar silenciosamente la configuración productiva.
[Documentación oficial de Interactions](https://aistudio.google.com/docs/interactions-overview).

## Tokens, volumen y costo

Medición **local, sin tokenizar ni llamar APIs**: 16 prompts de 22.236–22.728
caracteres, 360.730 caracteres en total por repetición; esquema de 5.461 bytes
UTF-8 adicionales. No equivalen a tokens facturados. El prompt productivo no se
recorta para abaratar esta línea base.

Google publica para Standard de `gemini-3.5-flash-lite` **USD 0,30 por millón de
tokens de entrada y USD 2,50 por millón de salida, incluido thinking**. Se
consultaron el 10/11/2026. No se comprobó el plan/cuota de la cuenta ni se asume
gratuidad. [Tarifas oficiales](https://aistudio.google.com/docs/pricing#gemini-3.5-flash-lite).

Fórmula: costo = entrada facturable/1e6 × 0,30 + salida facturable/1e6 × 2,50.
Usar usage oficial, sin sumar thinking dos veces si ya está incluido. No habilitar
herramientas, búsquedas, caching explícito, batch/flex/priority ni cambiar endpoint.

**Ejemplo presupuestario, no predicción observada:** 8.000 tokens de entrada y
1.500 de salida por intento costarían USD 0,00615: USD 0,2952 por 48 intentos o
USD 0,5904 por 96 de ese tamaño. Los segundos intentos, errores y pensamiento
pueden tener otros consumos; sin usage no se afirma un costo real.

**Propuesta de reserva conservadora, pendiente de aprobación:** entrada máxima
16.384 tokens por intento, salida máxima documentada 65.536, techo 96 intentos.
Con esas cotas y tarifa Standard: USD 16,2005; proponer reserva máxima **USD 18**,
no cargar ni consumir ese presupuesto ahora. El límite de salida se obtiene del
[modelo oficial](https://aistudio.google.com/docs/models/gemini-3.5-flash-lite),
no de un parámetro introducido en el agente.

No existe hoy un max_output_tokens productivo más pequeño. Por tanto, NO prometer
un techo de USD 1 conservando los parámetros. Antes de autorizar B se debe verificar
que las cotas incluyen todos los tokens facturables, perfil Standard y tarifas
vigentes, y acreditar el tamaño de entrada con prompt/esquema/envoltorio completo.
Si no se puede acreditar la cota de entrada o salida, **no ejecutar bajo la reserva
de USD 18** ni aumentar el presupuesto automáticamente.

Google ofrece count_tokens y usage de respuesta. Un preflight remoto podría
requerir hasta **16 solicitudes de conteo adicionales**, independientes de las 96
generativas; también necesitan autorización. Si el conteo solo cubre el texto,
no presentarlo como límite exacto de todo el request. La aproximación caracteres/4
no es un control estricto para español/esquemas.
[Conteo oficial](https://aistudio.google.com/docs/tokens).

Reservar el peor costo de cada intento ANTES de emitirlo, incluyendo los dos
posibles intentos por extracción; contabilizar conservadoramente los intentos sin
usage. Parar antes de superar reservas/techo, no solo después de leer la factura.
Es un control del ejecutor local, no un límite bancario/de facturación de Google;
una petición ya emitida puede terminar y facturarse después de Ctrl+C.

## Trazabilidad de entrada y calificación C

Cada registro real tendrá case_id, trial, source=gemini_recording, model,
duration_ms, response (texto original) o error, más `capture` con:

- agent_version y agent_sha256 del código ejecutado;
- corpus_sha256 y contract_sha256;
- input_sha256 = digest del objeto `{"message": probe_message(case),
  "previous_data": probe.previous_data}`;
- request_profile_sha256 del perfil completo aprobado, run_id y sdk_version.

El perfil separado guarda modelo, endpoint, parámetros explícitos/defaults,
política de reintentos/deadlines y límites aprobados, sin credenciales. Los hashes
no autentican una captura fabricada; se confía en los archivos locales revisados.
El evaluador no reetiqueta grabaciones viejas con el agente actual. Las huellas
actuales de evaluación se conservan por separado al calificar.

Para semántica libre, leer la respuesta original y criterios del corpus. Cada
adjudicación JSONL debe incluir `case_id`, `trial`, `check_id`, `response_sha256`
(publicado en el informe preliminar), `reviewer`, `note` justificativa y booleano
`passed`. No usar un segundo LLM como juez en esta fase. Una revisión desconocida,
duplicada, sin explicación o sobre otro hash se rechaza. Revisar 48 respuestas
implica trabajo humano además del pequeño gasto potencial de API.

## Comandos

Disponibles hoy y offline, desde la raíz del repositorio:

```powershell
uv run --no-sync python manage.py test incidents.test_agent_evaluation --settings=config.incident_evaluation --noinput
uv run --no-sync python manage.py check --settings=config.incident_evaluation

# Solo cuando existan capturas autorizadas:
uv run --no-sync python -m incidents.evaluation grade --input .qa/gemini-baseline.jsonl --output .qa/gemini-preliminary.json --agent-version baseline-v2
uv run --no-sync python -m incidents.evaluation grade --input .qa/gemini-baseline.jsonl --reviews .qa/gemini-reviews.jsonl --output .qa/gemini-reviewed.json --agent-version baseline-v2
uv run --no-sync python -m incidents.evaluation compare --baseline .qa/gemini-reviewed.json --candidate .qa/gemini-candidate-reviewed.json
```

Interfaz **propuesta y NO implementada** para B; no ejecutarla todavía:

```powershell
# DISEÑO PENDIENTE: hoy capture no existe. Requiere implementación/revisión
# y autorización humana separada; no se ejecutó en esta intervención.
uv run --no-sync python -m incidents.evaluation capture --profile .qa/gemini-profile-approved.json --agent-version baseline-v2 --trials 3 --max-logical-calls 48 --max-http-attempts 96 --max-run-seconds 2700 --max-input-tokens 16384 --reserve-usd 18 --stop-file .qa/STOP-GEMINI --output .qa/gemini-baseline.jsonl
```

Antes de implementar/autorizar ese ejecutor: aprobar las 16 rúbricas v2 y el
perfil/costo, probarlo con cliente HTTP en memoria y demostrar que parar, errores,
reservas por intento, no sobrescritura y sanitización funcionan. Después de una
línea base completa/adjudicada se elige un cambio de prompt candidato. No se
agrega memoria ni se cambia proveedor durante esta etapa.

## Alcance de cambios y validación

Solo cambian `incidents/evaluation/corpus.json`, `core.py`, `__main__.py`,
`incidents/test_agent_evaluation.py` y documentación de evaluación. No se
modifican `incidents/ai`, servicios productivos, Contenidos, modelos, migraciones,
`.env`, cuentas, incidentes o receptores. Sin commit/push. Los JSON históricos se
conservan y quedan explícitamente incompatibles con la nueva rúbrica v2.

Validación final de esta auditoría: **41 tests del evaluador aprobados** (15
adicionales respecto de los 26 iniciales); **48/48 regresiones seleccionadas y
26/26 casos backend aprobados**, cero omisiones y cero intentos externos
detectados. Django check sin incidencias. No se repitió toda la batería histórica
ni se usaron los dos tests omitidos de entrega exactamente una vez como aprobados.
Los errores Gemini/SMTP de las regresiones son inyecciones locales. La construcción
de prompts se hizo con clientes y sockets bloqueados y registró cero intentos.

La fuente del agente productivo se coteja con la huella de la línea base anterior;
los cambios de esta auditoría afectan exclusivamente evaluación/documentación.
