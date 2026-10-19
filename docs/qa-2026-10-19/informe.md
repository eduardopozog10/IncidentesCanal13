# Secuoya Chile — renovación visual y validación integral

Fecha: **19 de octubre de 2026**, America/Santiago. Repositorio técnico: `IncidentesCanal13`.

**Resultado:** 15 escenarios ejecutados por Telegram y Gemini reales sobre PostgreSQL QA; fallos documentados antes de corregir; solo los casos reales fallidos 11, 12 y 14 repetidos. Resultado final de esos casos: aprobado. **91 pruebas automatizadas aprobadas: 71 existentes + 20 nuevas.** Sin commit, push, servicios contratados, cambios de credenciales ni modificaciones a incidentes originales.

## A. Mejoras visuales

Se revisó la arquitectura real: una vista `/dashboard/` reúne Centro de control, Incidentes, Analítica y Gestión mediante navegación por hash. El detalle y la actualización de estado pertenecen a Gestión. No existe una pantalla propia de login del dashboard; `/admin/login/` es el login estándar de Django.

Cambios aplicados:

- Sistema visual compartido con superficies azul petróleo, navegación marina, acentos cian y colores semánticos para estados y prioridades. Paleta de producto derivada del repositorio; **no se presenta como paleta HEX oficial**.
- Tipografía de sistema, jerarquía menos pesada, espaciado regular, tarjetas compactas y cinco métricas en una fila cuando hay ancho suficiente. Se preservaron todos los indicadores y sus fórmulas.
- Se sustituyeron 997 líneas de capas de acabado contradictorias por `operations.css`. Se conservó el CSS estructural y responsive existente.
- Tablas, filtros, botones, tarjetas móviles, cola de atención y detalle armonizados. La tabla ancha desplaza dentro de su contenedor; el detalle y los controles de gestión pueden alcanzarse desplazando la página.
- Títulos del navegador por sección, navegación con `aria-current`, enlace para saltar al contenido y botones nativos para abrir incidentes desde tablas mediante teclado.
- Foco visible, objetivos táctiles y reducción de movimiento. Estados vacíos y mensajes de actualización conservados.
- Administración y login con título, favicon, paleta y acabados Secuoya, manteniendo los formularios y componentes de Django.

### Identidad y favicon

Se utilizó **exclusivamente** el archivo local `secuoya-chile-logo.png`; el original permanece intacto. Se generó una copia del wordmark sin margen transparente para la navegación. El usuario autorizó expresamente usar un **recorte de la S del mismo archivo**, sin redibujarla, para mejorar legibilidad del favicon.

Formatos: ICO de 16/32/48/64 px, PNG de 32 px y Apple Touch Icon de 180 px. El fragmento `brand_head.html` se comparte entre dashboard y administración. Las URL del favicon incluyen `v=20261019s` para invalidar caché.

Referencias institucionales consultadas: [Secuoya Chile](https://www.linkedin.com/company/secuoyachile/), [presentación institucional de Secuoya Content Group](https://inversores.secuoyacontentgroup.com/documents/1h-2025-result-presentation-secuoya.pdf), [sedes de Secuoya Studios](https://www.secuoyastudios.com/es). Confirman identidad y contexto audiovisual; no se encontró un manual público verificable de tipografía, códigos HEX ni reglas de reducción del logo chileno. El sitio principal del grupo no fue accesible con el navegador de investigación. No se descargaron fuentes ni recursos externos.

### Verificación visual real

Instancia Django controlada en `127.0.0.1:8765`, exclusivamente con base QA.

| Comprobación | Resultado |
|---|---|
| Centro de control, lista, analítica, gestión y detalle | Revisados en Django con datos QA |
| Modo claro y oscuro | Revisados visualmente |
| Escritorio 1440×900 y pantalla de poca altura 1366×650 | Sin desbordamiento global; sidebar accesible |
| Tablet 768×1024 y teléfono 390×844 | Navegación inferior, tarjetas y gestión accesibles |
| Tabla ancha en escritorio de poca altura | Contenedor 1048 px, contenido 1120 px; página sin desbordamiento |
| Búsqueda «Cámara 4» | Tres resultados en el momento de la comprobación |
| Filtros En atención + Media | Un resultado: SC-000026 |
| Período Mes anterior | Estado vacío real, sin agregar ni borrar datos |
| Apertura de detalle con Enter | Correcta |
| Actualización por formulario con CSRF | SC-000026 pasó a En atención en QA; mensaje de éxito visible |
| Login de administración | Identidad, favicon y error de credenciales ficticias revisados |
| Contraste de seis pares principales | Entre 4,79:1 y 8,93:1; no constituye una auditoría completa WCAG |

Las dimensiones efectivas de contenido descuentan la barra de desplazamiento. Las capturas están en [screenshots](screenshots/). No se verificaron navegadores móviles físicos ni el CRUD de administración autenticado. La prueba de login usó un usuario ficticio inexistente, sin modificar cuentas.

## B. Mejoras conversacionales

Se revisaron proveedor Gemini, esquema estructurado, procesador, borradores, entrada de mensajes, handler Telegram, polling, persistencia y correo.

- El prompt distingue datos de instrucciones, explicita terminología audiovisual chilena, extracción de varios campos, ambigüedad, correcciones múltiples y preguntas sobre prioridad. No promete registro, seguimiento ni asignaciones que Django no haya realizado.
- Validación de objeto JSON, tipos de campos e intención antes de consumir una respuesta de Gemini.
- Django valida campos obligatorios, tamaños máximos y prioridades; una prioridad propuesta por el modelo sin declaración del reportante se descarta. Se revalida el borrador antes de crear el registro.
- «Alta, no crítica» conserva Alta; una pregunta «¿es alta?» no confirma prioridad. Una declaración posterior a otra pregunta sí se conserva.
- Los mensajes que incluyen varios datos pasan por extracción conjunta, evitando que un atajo de un campo absorba impacto y prioridad o ignore otros cambios.
- Se reconocen correcciones naturales durante la recopilación y después del resumen. Se preservan los demás datos y se sincroniza descripción con los campos corregidos.
- Equipo con alternativas exige aclaración. Se conserva la regla anterior de pedir identificador cuando solo se menciona un equipo genérico.
- Preguntas detectadas sobre campos ya completos se reemplazan por una pregunta sobre un dato faltante.
- Respuestas malformadas o fallos recuperables mantienen el borrador y mensajes pendientes. Se conservan los límites existentes de dos intentos y 45 segundos totales del proveedor.
- Un bloqueo PostgreSQL por reportante serializa procesamiento y cancelación. `get_or_create` admite entregas duplicadas simultáneas sin procesarlas dos veces.
- Confirmación repetida devuelve el código real existente sin consultar IA ni crear otro incidente. Un sí sin reporte listo no crea un registro.
- El transporte Telegram exige un resumen completo registrado como enviado correctamente antes de aceptar confirmación. Un envío fallido o un resumen anterior a una corrección no autoriza creación. Los resúmenes extensos se entregan en partes, con la pregunta de confirmación al final y todos los IDs de entrega conservados; no se truncan datos.
- Bienvenida breve que invita a contar la falla con palabras propias.
- Correo posterior al commit conservado; un fallo de correo no revierte el incidente.

No se agregó una cola ni agrupación temporal nueva. El polling existente procesa mensajes secuencialmente; el caso fragmentado conservó los cinco mensajes. Los bloqueos protegen también el acceso concurrente a la conversación. Durante una ráfaga pueden recibirse preguntas correspondientes a mensajes anteriores antes de que se procese el último fragmento; se documenta esta característica y se evita introducir un debounce que cambie la entrega o descarte mensajes.

El proyecto no contiene un inventario relacional equipo–ubicación: no se puede certificar pertenencia física a un estudio o móvil. Se preservan identificadores aportados por el reportante y se vuelven a validar precisión, ambigüedad y campos al corregir. No se inventó un catálogo de activos.

## C. Resultados de los 15 escenarios reales

**Nivel A:** Telegram Web con la cuenta personal autorizada → bot `secuoyachile_incidentes_bot` → Gemini real → procesador → PostgreSQL QA → API/dashboard aislado. El fallo del proveedor de A15 se inyectó de forma controlada; el resto del recorrido y la recuperación usaron servicios reales.

| ID | Escenario | Nivel | Resultado inicial | Error y corrección | Resultado final | Código QA |
|---|---|---|---|---|---|---|
| 01 | Cámara, información completa | A | Aprobado | Sin preguntas redundantes | Aprobado, sin repetir | SC-000013 |
| 02 | Consola Yamaha CL5 | A | Aprobado | Audio, cortes y respaldo conservados | Aprobado, sin repetir | SC-000014 |
| 03 | Micrófono incompleto | A | Aprobado | Pidió identificador y aprovechó datos complementarios | Aprobado, sin repetir | SC-000015 |
| 04 | Enlace de transmisión caído | A | Aprobado | Prioridad crítica explícita | Aprobado, sin repetir | SC-000016 |
| 05 | Intercom del móvil en terreno | A | Aprobado | Diferenció móvil de equipo; Audio | Aprobado, sin repetir | SC-000017 |
| 06 | Switcher ATEM | A | Aprobado | Estudios/Producción según clasificación vigente | Aprobado, sin repetir | SC-000018 |
| 07 | Red y conectividad | A | Aprobado | Router, ubicación e impacto conservados | Aprobado, sin repetir | SC-000019 |
| 08 | Streaming digital | A | Aprobado | Digital/Streaming, sin confundir con señal tradicional | Aprobado, sin repetir | SC-000020 |
| 09 | Alimentación eléctrica UPS 2 | A | Aprobado | Infraestructura y circuito alternativo | Aprobado, sin repetir | SC-000021 |
| 10 | Cinco mensajes consecutivos | A | Aprobado | Conservó Cámara 3, Móvil 2, síntoma y demás datos | Aprobado, sin repetir | SC-000022 |
| 11 | Corrección de equipo, ubicación y prioridad | A | Fallo funcional | Atajo de un campo ignoraba cambios; extracción conjunta y negación de prioridad | Aprobado tras reprueba | SC-000031 |
| 12 | Equipo ambiguo y corrección del síntoma | A | Fallo funcional | Aceptaba alternativas; aclaración y actualización del síntoma durante recopilación | Aprobado tras ajuste de reprueba | SC-000032 |
| 13 | Coloquial, errores ortográficos y emoji | A | Aprobado | Normalizó escritura sin perder contexto | Aprobado, sin repetir | SC-000023 |
| 14 | Sí previo y confirmación repetida | A | Fallo conversacional | Ya evitaba duplicado, pero pedía iniciar otro reporte; ahora devuelve código existente | Aprobado tras reprueba | Inicial SC-000024; reprueba SC-000033 |
| 15 | Interrupción y recuperación | A + fallo C | Aprobado | Timeout de una llamada; reanudó desde PostgreSQL sin repetir datos | Aprobado, sin repetir | SC-000025 |

Casos 11 y 12 iniciales: borradores cancelados tras documentar el fallo; no se confirmó información incorrecta ni se fabricó un incidente.

### Evidencia y separación de niveles

- [Ronda A inicial](telegram-initial.json): 78 mensajes, 15 borradores y 13 incidentes; datos extraídos, respuestas, mensajes y relaciones.
- [Primera reprueba](telegram-retest-1.json): conserva el detalle del síntoma todavía incorrecto en A12 antes del ajuste final.
- [Ronda A final acumulada](telegram-final.json): 100 mensajes, 18 borradores, 16 incidentes. Contiene las tres repruebas reales y las confirmaciones repetidas.
- [Ronda B inicial](initial.json): 15 escenarios internos con Gemini real sin transporte Telegram. Se conserva íntegra, incluidos errores del evaluador.
- [Auditoría de instrumentación B](initial-audited.json) y [repruebas B de 05, 11, 12, 14 y 15](retest.json). No se volvió a ejecutar toda la matriz B.
- Nivel C: las pruebas nuevas aíslan proveedor malformado, fallo de correo, persistencia, concurrencia y prioridades sin consumir servicios externos. Dos reportantes se verificaron con identidades sintéticas independientes en C; no se usó otra cuenta real de Telegram.

Los informes automáticos B comprueban campos y persistencia; la revisión manual de conversaciones también encontró H03/H04 aunque el registro terminara creado. Esos comportamientos siguen registrados como fallos en la bitácora y no se presentan como aprobación conversacional.

## D. Fallos documentados y correcciones

La [fotografía inicial](primera-ronda.md) fue guardada antes de modificar la lógica.

| Hallazgo | Impacto / severidad | Causa y módulo | Estado |
|---|---|---|---|
| H01: corrección múltiple ignorada | Datos erróneos, alta | Atajos y alternativa de prioridad negada; procesador | Corregido; A11 y regresión |
| H02: equipo ambiguo y corrección posterior | Datos erróneos, alta | Alternativas sin validar y corrección natural durante recopilación | Corregido; A12 y regresión |
| H03: impacto absorbe prioridad | Pregunta repetida, media | Detección insuficiente de respuesta compuesta | Corregido; B15 y regresión |
| H04: segunda confirmación pide iniciar reporte | Confusión, baja | No había respuesta determinista sin borrador activo | Corregido; A14, B14 y regresión |
| H05: carreras de entrega y confirmación | Riesgo de pérdida/duplicado, alta | Lectura antes de INSERT y conversación sin bloqueo | Corregido; dos pruebas concurrentes PostgreSQL |
| H06: JSON inválido, tipos y tamaños | Pérdida de continuidad/error SQL, alta | Validación insuficiente del proveedor y borrador listo | Corregido; regresiones de validación y recuperación |
| H07: favicon ausente y CSS contradictorio | Identidad y legibilidad, media | Assets y acabado compartido | Corregido y revisado en navegador |
| H08: READY antes de entregar el resumen | Confirmación sin presentación, alta | Estado de datos no distinguía entrega del transporte | Corregido; tres regresiones C para envío fallido, resumen anterior y resumen extenso con emojis |
| Reprueba A12: descripción y síntoma diferentes | Resumen inconsistente, media | Cambio natural fuera del modo de corrección explícito | Corregido antes de confirmar; SC-000032 guarda Imagen intermitente |

Errores del evaluador, documentados sin atribuirlos al producto:

1. Usaba una instancia ORM de borrador anterior a la confirmación, causando falsos avisos de persistencia. Se comprobó la relación real en PostgreSQL y se añadió `refresh_from_db`.
2. En B05 respondía «intercom» reiteradamente cuando la regla vigente pedía identificador. Se mantuvo la regla y se repitió solo ese caso con Intercom Riedel 1.
3. Esperaba Video/Cámaras para el switcher; se aceptó la clasificación vigente Estudios/Producción sin cambiar negocio.
4. El timeout preparado en B15 inicial no se activó porque un atajo interceptó el mensaje. Se probó correctamente en A15 y en la reprueba B con respuesta compuesta.
5. Una regresión inicial de proveedor malformado tampoco alcanzaba la IA; se ajustó el estímulo para forzar la extracción, conservando el borrador.

Los 71 tests existentes pasaron antes de cambios. Durante la implementación dos tests históricos detectaron que se estaban enviando a IA nombres válidos de un solo campo; se ajustó el detector sin eliminar ni editar esos tests. La primera ejecución de las 11 regresiones tenía 5 fallos y 4 errores. Todo quedó corregido en la suite final.

## E. Verificación posterior y protección de datos

- Repetidos por Telegram: **solo 11, 12 y 14**. A12 requirió un ajuste más y se continuó el mismo borrador, sin crear un duplicado.
- Ronda B: solo 05 (instrumentación), 11, 12, 14 y 15. Los cinco llegaron al registro; códigos SC-000026 a SC-000030.
- QA terminó con **33 incidentes y 33 borradores completados**, además de borradores cancelados o incompletos de las rondas iniciales. No se borraron registros fallidos para aparentar éxito.
- Auditoría SQL/ORM de los ocho campos: **0 diferencias** entre borrador completado e incidente. **0 mensajes de borradores completados sin incidente vinculado**.
- Cada confirmación válida produjo un único registro; las confirmaciones repetidas y pruebas concurrentes no produjeron duplicados. Los casos repetidos entre rondas son fixtures deliberados, identificados por sus reportantes/rondas.
- Estado inicial Reportado en los registros de evidencia; SC-000026 cambió después a En atención exclusivamente para la verificación visual del formulario.
- Códigos únicos dentro de QA, reportantes aislados, fecha/hora America/Santiago y relación con mensajes comprobados. Los códigos de QA pertenecen a una base distinta de los códigos de producción.
- Base principal `incidentes_canal13_db`: **34 incidentes antes y después**, máximo ID final 37, comprobación final en transacción SQL de solo lectura. No se ejecutó ninguna escritura, migración, limpieza ni reset de secuencias sobre ella. No se tomó un hash inicial de contenido; la comprobación cuantitativa no se presenta como una comparación de hashes.
- QA: `qa_secuoya_20261019`; tests: `test_qa_secuoya_20261019`, en la misma instancia local y usando el rol del propio proyecto. Correos QA en memoria, sin SMTP ni destinatarios reales. No se cambiaron credenciales.
- Bot confirmado mediante getMe y ausencia de webhook. Receptores locales identificados; el receptor previo dejó de aparecer sin intervención del agente. Las únicas detenciones realizadas fueron de receptores QA iniciados por este trabajo.
- Receptor QA detenido al terminar. Comprobación local final: **0 receptores del bot de este repositorio**. Consulta Telegram sin offset: **0 mensajes pendientes**, sin descartar actualizaciones.

## F. Calidad técnica

| Verificación | Resultado |
|---|---|
| `uv run python manage.py check` | Sin incidencias |
| `uv run python manage.py makemigrations --check --dry-run` | No changes detected |
| `uv run python manage.py test incidents --settings=config.qa --noinput` | **91/91 OK** |
| `git diff --check` | Sin errores |
| Regresiones agregadas | **20**; ninguna prueba histórica eliminada |
| Servicios adicionales o cambios de arquitectura de producción | Ninguno |
| Commit / push | No realizados |

Salida de los checks: [quality.json](quality.json). El traceback de correo en el log local de tests corresponde a la simulación deliberada `OSError: Fallo QA`; la suite termina OK.

Riesgos y límites concretos:

- Gemini es no determinista: la matriz validada no demuestra cobertura de todas las expresiones futuras. Persistencia y confirmación siguen controladas por Django.
- No hay inventario de pertenencia de activos a ubicaciones ni agrupación temporal de mensajes nueva.
- Se preservaron los permisos existentes. El dashboard y los endpoints operativos no incorporan una nueva capa de autenticación en este lote; el endurecimiento para una exposición pública requiere un trabajo separado. Esta validación se hizo en localhost.
- UI revisada en navegador de escritorio con viewport responsive, sin dispositivos móviles físicos ni recorrido de administración autenticada.
- La base QA y sus evidencias se conservan para revisión. No eliminarlas ni confundirla con la base principal.

## G. Archivos y próximos pasos

Modificados:

- `.gitignore`, `README.md`, `config/settings.py` (directorio de templates para sobrescribir branding del admin).
- `incidents/ai/gemini_provider.py`.
- `incidents/messaging/telegram_update_handler.py`.
- `incidents/services/incident_processor.py`, `incoming_message_service.py`, `incident_draft_service.py`.
- `incidents/static/incidents/dashboard.css`, `incidents/templates/incidents/dashboard.html`.

Nuevos:

- `config/qa.py`.
- `incidents/static/incidents/operations.css`, `admin-brand.css` y cuatro assets de marca/navegador.
- `incidents/templates/incidents/brand_head.html`, `templates/admin/base_site.html`.
- `incidents/test_operational_regressions.py`.
- `scripts/qa_scenarios.py`, `qa_telegram_receiver.py`, `qa_export.py`, `build_brand_assets.py`.
- Este informe, bitácora, JSON de rondas y capturas dentro de `docs/qa-2026-10-19/`.

Temporales: `.qa/` contiene logs y una ruta de caché local, ignorados por Git; `__pycache__` también está ignorado. La base de tests fue destruida automáticamente por Django. El logotipo original se conserva sin modificaciones. No se guardaron tokens, contraseñas ni identificadores personales de Telegram en los informes; los scripts reciben la cuenta autorizada por variable de entorno.

Para retomar normalmente el bot, con `.env` habitual y sin otro receptor activo:

```powershell
uv run python manage.py run_telegram_bot
```

No usar `--discard-pending` salvo decisión explícita. El README incluye comandos separados para revisar QA, exportar evidencia y simular una llamada fallida. La instancia visual QA queda disponible en [Centro de control QA](http://127.0.0.1:8765/dashboard/); el receptor Telegram permanece detenido.

Recomendación de commit para revisión manual en GitHub Desktop:

> Renueva la interfaz Secuoya y refuerza las conversaciones, confirmaciones y pruebas QA

Revisar el diff, los informes y las capturas antes de hacer ese commit. No hay cambios pendientes que requieran contratar o desplegar servicios.
