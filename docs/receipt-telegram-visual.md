# Comprensión de recepciones y presentación de Telegram

Intervención del 10 de noviembre de 2026. Entorno exclusivo de demostración:
`config.content_demo`, PostgreSQL `demo_secuoya_contenidos`.

## Diagnóstico comprobado

El mensaje real **76** de Daniela contiene «Ya recibí CT-000004 y voy a comenzar a
trabajar en el diseño de la gráfica del tiempo». Su `Message.result.interpretation`
guardó `action: receive`, sin versión. La respuesta posterior fue la restricción
de Switch. El mensaje **77**, «Voy a comenzar el diseño de CT-000004», guardó
`action: start` y comenzó correctamente el diseño.

La clasificación equivocada de Gemini está comprobada en el registro persistido.
Contribuían dos límites del backend: el contrato no distinguía el objeto recibido
(asignación frente a versión/material), y la adaptación semántica no revisaba ese
caso antes de resolver `receive`. El código CT se resolvía correctamente. Los
permisos impidieron la recepción técnica indebida; no eran la causa del fallo.

Para la presentación, todas las salidas Telegram encontradas pasan por
`TelegramProvider.send_text` y el formateador compartido `telegram_html`. El
formateador destacaba códigos/etiquetas y limitaba emojis existentes, pero no
componía indicadores semánticos. Los mensajes de Contenidos no los generaban.
Muchas respuestas comenzaban con una oración o un código, sin título visual.

## Implementación y archivos

| Archivo | Cambio |
|---|---|
| `content_coordination/ai.py` | Acción `acknowledge`, campo validado `receipt_scope`, instrucciones para distinguir acuse, inicio, avance, entrega y Switch. |
| `content_coordination/interpretation.py` | Adapta actos con evidencia literal antes de autorizar; un acuse y un inicio del mismo trabajo se consolidan. Las etapas incompatibles y recepciones ambiguas piden aclaración. |
| `content_coordination/resolution.py` | Resuelve acuses sobre tareas propias y elimina ruido verbal de referencias genéricas, conservando la exigencia de desambiguar varios trabajos. |
| `content_coordination/processor.py` | Contexto con propiedad/estado real de tareas, registro de `resolved_updates` separado de la extracción original, acuse sin mutación y respuestas estructuradas. |
| `content_coordination/services.py` | Plantillas de creación, distribución, asignación, versiones, seguimiento, avances y plazos; conserva destinatarios, claves de deduplicación, consentimientos y estados de entrega. |
| `content_coordination/reporting.py` | Consulta con indicador de seguimiento y eliminación de una repetición del mismo avance. |
| `incidents/messaging/presentation.py` | Extiende el único compositor con tipos visuales; negritas de título, CT, estado y responsable; compatibilidad visual con plantillas canónicas de Incidencias. |
| `content_coordination/test_receipt_semantics.py` | Nuevos casos de clasificación incorrecta, varias cláusulas, privacidad, estados, negaciones, idempotencia y contexto. |
| `content_coordination/test_telegram_visual.py` | Nuevos recorridos outbox → proveedor → HTTP simulado, con destinatarios separados, HTML, versiones, fragmentos, fallback y acuses. |
| `incidents/test_message_presentation.py` | Regresiones compartidas y recorrido real del handler de Incidencias hasta el HTTP simulado, incluida deduplicación. |

Se reutilizan `direct_action_text` del agente de Incidencias, su patrón de separar
extracción de autorización, los bloqueos, la resolución autorizada y el formateador
existente. No se copiaron sus reglas de negocio al dominio audiovisual ni se
modificó el procesamiento de incidentes.

Un acuse no comienza una tarea. Un inicio solo modifica el diseño propio elegible.
Un avance parcial conserva su texto, negaciones y estimación sin entregar versiones.
Switch sigue requiriendo su rol y una versión en la etapa correcta. Las operaciones
sensibles mantienen la confirmación del resumen entregado y la revisión vigente.

## Rutas de salida y formato

- Respuestas conversacionales de Contenidos: `Processor.finish` → outbox →
  `delivery.deliver_one` → `TelegramProvider.send_text` → HTML compartido.
- Avisos automáticos, asignaciones, versiones, plazos y seguimientos: los mismos
  outbox/adaptador y formateador; las responsabilidades y destinatarios se conservan.
- Respuestas, aclaraciones, errores y avisos de procesamiento de Incidencias:
  `TelegramUpdateHandler` → el mismo `TelegramProvider.send_text`.
- Los saludos sencillos pueden seguir siendo texto breve sin icono. El modo texto
  plano se reserva al fallback existente: rechazo explícito de formato HTTP 400
  o un payload escapado muy grande. Un timeout, 429 o 5xx no dispara otro envío por
  motivos visuales. No se cambiaron sus reintentos ni su lógica de recuperación.

Solo se generan etiquetas `<b>`, escapando los datos dinámicos. Cada fragmento se
formatea después de dividir el texto canónico, manteniendo HTML equilibrado y
límites UTF-16. El icono principal es contextual: ✅ registrado; ❓ pendiente;
📋 solicitud; 📩 asignación; ⏱️ avance/plazo; 🔄 seguimiento; ⚠️ restricción; 🎬 emisión.
El estado encolado nunca se presenta como una entrega confirmada al destinatario.

## Ejemplos ilustrativos

Los códigos de estos ejemplos son ficticios; no se crearon registros reales.

```text
✅ Solicitud registrada
Solicitud: CT-NUEVO · Gráfica del tiempo
Área solicitante: Prensa
Área encargada: Gráfica
Prioridad: Normal
Estado: Pendiente de asignación
```

```text
❓ ¿Confirmas esta asignación?
Solicitud: CT-NUEVO · Gráfica del tiempo
Responsable: Daniela
¿Confirmas asignar el diseño de CT-NUEVO?
Siguiente paso: Responde «confirmo» o indica la corrección.
```

```text
✅ Diseño asignado
Solicitud: CT-NUEVO · Gráfica del tiempo
Responsable: Daniela
Estado: Asignada
La asignación quedó guardada.
```

```text
🔄 Seguimiento pendiente de envío
Responsable: Daniela
Solicitud: CT-NUEVO · Gráfica del tiempo
La consulta quedó pendiente de envío. Te avisaré cuando responda.
```

```text
⏱️ Nuevo avance de CT-NUEVO
Responsable: Daniela
Último avance: La gráfica está avanzada; faltan detalles.
Estado: En producción
Estimación informada: 15 minutos.
```

```text
⚠️ Acción no autorizada
Esta etapa requiere un responsable de Switch autorizado o un administrador.
```

```text
🎬 Emisión confirmada
Solicitud: CT-NUEVO · Gráfica del tiempo
Versión: 1.0
Estado: Emisión confirmada
La versión quedó registrada como emitida.
```

El ejemplo de restricción corresponde a un intento técnico real; un acuse de
asignación ya no debe producirlo. La emisión solo aparece tras su confirmación
autorizada. Los mensajes conservan negritas nativas en los payloads HTML.

## Validación técnica y visual

| Ejecución | Aprobadas | Omitidas | Fallidas |
|---|---:|---:|---:|
| Suite completa de Contenidos | 204 | 0 | 0 |
| Incidencias: presentación, confirmación natural, grupos y seguridad Telegram | 109 | 0 | 0 |

Se añadieron 25 pruebas de Contenidos y 3 de presentación compartida/Incidencias.
Las dos omisiones históricas de la suite completa de Incidencias no forman parte
de esta selección ni se cuentan como aprobadas. No se repitió esa suite completa.
Django `check` terminó sin problemas, `makemigrations --check --dry-run` sin cambios
y `migrate --check` sin migraciones pendientes en la demo. `git diff --check` pasó.

La lectura final, en una transacción `READ ONLY`, comparó huellas de las solicitudes
y sus historiales: CT-000003 sigue en producción/revisión 3 y CT-000004 en
producción/revisión 4. Solicitudes e historiales coinciden con la referencia inicial;
también coinciden participantes, consentimientos y la transferencia activa
`bdf62dea-4d35-474e-8fad-5cebf91215b8`. Evidencia en
`.qa/receipt-visual-integrity-final.log`.

Resultados y logs finales: `.qa/receipt-visual-contents-final.log` y
`.qa/receipt-visual-incidents-final.log`. Los tests utilizan PostgreSQL temporal,
IA simulada y HTTP Telegram simulado. No se contactó el bot real.

Se exportaron 22 payloads generados por cinco recorridos de pruebas a
`.qa/message-preview/receipt-visual-payloads.json`; la vista correspondiente es
`.qa/message-preview/receipt-visual.html`, con columnas de escritorio y móvil
(360 px). El script `.qa/render_receipt_visual_preview.py` permite regenerarla
utilizando exclusivamente la base temporal de tests.

La apertura de `file://` fue bloqueada por la política del navegador de revisión.
No se sorteó ese bloqueo. La inspección visual mediante navegador y la apariencia
real en clientes Telegram móvil/escritorio quedan pendientes de revisión manual;
no se declaran comprobadas. Sí se verificaron automáticamente los payloads reales
del adaptador, el escape, las negritas, los tamaños, los fragmentos y el fallback.

## Reinicio y prueba manual

1. Detén con Ctrl+C **la instancia existente** del receptor. No abras otra en
   paralelo. Este desarrollo no inició ni detuvo receptores.
2. Desde la raíz del repositorio:

```powershell
uv run python manage.py check --settings=config.content_demo
uv run python manage.py run_secuoya_demo --settings=config.content_demo --preflight
```

Si hay pendientes, detente e inspecciona cada ID con el procedimiento existente:

```powershell
uv run python manage.py run_secuoya_demo --settings=config.content_demo --inspect-pending-id ID_REAL
```

No uses opciones para descartar ni autorices mensajes anteriores a ciegas.
Con la cola revisada y vacía, inicia una sola instancia:

```powershell
uv run python manage.py run_secuoya_demo --settings=config.content_demo --live
```

3. Conserva CT-000003 y CT-000004. Fernando crea una solicitud **nueva**, confirma
   el resumen, solicita asignarla a Daniela y confirma la asignación.
4. Daniela debe recibir 📩 y puede decir «Ya recibí CT-NUEVO y voy a comenzar a
   trabajar en el diseño de la gráfica del tiempo». Sustituye el código por el
   generado. Debe responder ✅ Diseño iniciado, sin invocar permisos de Switch.
5. Fernando consulta el avance y autoriza el seguimiento ofrecido. El bot debe
   diferenciar la oferta, el aviso encolado y la consulta recibida por Daniela.
6. Daniela informa «Me faltan 15 minutos». Comprueba ⏱️, el avance en PostgreSQL/
   dashboard y la respuesta a Fernando. No debe aparecer una versión entregada,
   aprobada, recibida ni emitida por indicar tiempo restante.
7. Comprueba las negritas y separaciones en Telegram de escritorio y móvil; abre
   también el HTML local si deseas comparar los ejemplos de las pruebas.

No restaures ni reemplaces la transferencia temporal para realizar este recorrido.

## Límites y riesgos pendientes

- La recepción sin objeto («ya recibí CT…») pide aclarar si es asignación o Switch.
- Un mensaje con varias etapas independientes se revisa de una en una; no se
  ejecutan automáticamente operaciones técnicas por encontrar varios verbos.
- Varias tareas propias sin CT todavía requieren desambiguación.
- Un inicio diferido, citado, negado o condicional no se registra como comienzo.
- Las nuevas formulaciones de Gemini y el formato visual requieren la repetición
  real con ambas cuentas tras reiniciar. No se cambió el modelo ni sus parámetros.
- No hubo modificaciones de `.env`, dependencias, migraciones, estados reales,
  consentimientos, vínculos, transferencia, base operativa, commit ni push.
