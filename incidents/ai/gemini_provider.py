import json
import logging
import time

import httpx
from django.conf import settings
from google import genai
from google.genai import types

from .base import AIProvider
from .schemas import IncidentExtractionResult


logger = logging.getLogger(__name__)


class GeminiAIProvider(AIProvider):
    REQUIRED_FIELDS = (
        "area",
        "location_context",
        "equipment",
        "failure_type",
        "description",
        "operational_impact",
        "priority",
    )

    TOTAL_TIMEOUT_SECONDS = 45.0
    FIRST_ATTEMPT_TIMEOUT_SECONDS = 30.0

    RESPONSE_SCHEMA = {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": [
                    "incident_report",
                    "incident_followup",
                    "conversation",
                ],
                "description": (
                    "Intención principal del mensaje del reportante."
                ),
            },
            "conversation_reply": {
                "type": ["string", "null"],
                "description": (
                    "Respuesta conversacional cuando el mensaje no "
                    "corresponde a información de un incidente."
                ),
            },
            "unit": {
                "type": ["string", "null"],
                "description": (
                    "Campo heredado temporal para compatibilidad con "
                    "el flujo anterior."
                ),
            },
            "area": {
                "type": ["string", "null"],
                "description": (
                    "Área operacional o técnica explícitamente indicada "
                    "por el usuario."
                ),
            },
            "location_context": {
                "type": ["string", "null"],
                "description": (
                    "Ubicación o contexto explícito del incidente, por "
                    "ejemplo estudio, móvil, sala u otra ubicación."
                ),
            },
            "asset_category": {
                "type": ["string", "null"],
                "description": (
                    "Familia del equipo o servicio afectado. Valores "
                    "esperados: video_cameras, audio, "
                    "studios_production, signal_transmission, "
                    "mobile_field, networks, systems, "
                    "digital_streaming o infrastructure."
                ),
            },
            "equipment": {
                "type": ["string", "null"],
                "description": "Equipo que presenta la falla.",
            },
            "failure_type": {
                "type": ["string", "null"],
                "description": (
                    "Síntoma observable reportado por el reportante, "
                    "sin emitir un diagnóstico."
                ),
            },
            "description": {
                "type": ["string", "null"],
                "description": (
                    "Descripción objetiva de lo reportado por el usuario."
                ),
            },
            "operational_impact": {
                "type": ["string", "null"],
                "description": (
                    "Impacto operacional explícitamente informado por "
                    "el usuario, sin deducir consecuencias no mencionadas."
                ),
            },
            "priority": {
                "type": ["string", "null"],
                "description": (
                    "Prioridad confirmada explícitamente por el reportante: "
                    "low, medium, high o critical. Debe ser null si el "
                    "reportante todavía no ha confirmado una prioridad."
                ),
            },
            "clarification_question": {
                "type": ["string", "null"],
                "description": (
                    "Pregunta breve y natural para solicitar el siguiente "
                    "dato faltante del incidente."
                ),
            },
        },
        "required": [
            "intent",
            "conversation_reply",
            "unit",
            "area",
            "location_context",
            "asset_category",
            "equipment",
            "failure_type",
            "description",
            "operational_impact",
            "priority",
            "clarification_question",
        ],
    }

    def __init__(self):
        if not settings.GEMINI_API_KEY:
            raise ValueError(
                "GEMINI_API_KEY no está configurada."
            )

        self.model = settings.GEMINI_MODEL

        self.client = genai.Client(
            api_key=settings.GEMINI_API_KEY,
            http_options=types.HttpOptions(
                retry_options=types.HttpRetryOptions(
                    attempts=1,
                ),
            ),
        )

    def extract_incident(
        self,
        message: str,
        previous_data: dict | None = None,
    ) -> IncidentExtractionResult:
        previous_data = previous_data or {}

        prompt = self._build_prompt(
            message=message,
            previous_data=previous_data,
        )

        interaction = self._create_interaction_with_retry(
            prompt=prompt,
        )

        response_text = interaction.output_text

        if not response_text:
            raise ValueError(
                "Gemini devolvió una respuesta vacía."
            )

        try:
            response_data = json.loads(response_text)
        except json.JSONDecodeError as error:
            raise ValueError(
                "Gemini devolvió un JSON inválido."
            ) from error

        intent = self._normalize_intent(
            response_data.get("intent")
        )

        conversation_reply = self._clean_text(
            response_data.get("conversation_reply")
        )

        if intent == "conversation":
            if not conversation_reply:
                conversation_reply = (
                    "Solo puedo ayudarte con el sistema de gestión de "
                    "incidencias operativas de Secuoya Chile. Puedo "
                    "registrar un incidente, completar o corregir un "
                    "reporte pendiente y recopilar la información "
                    "necesaria para gestionarlo."
                )
            return IncidentExtractionResult(
                intent=intent,
                conversation_reply=conversation_reply,
                missing_fields=[],
                clarification_question=None,
                raw_response={
                    "provider": "gemini",
                    "model": self.model,
                    "data": response_data,
                },
            )

        unit = (
            self._clean_text(response_data.get("unit"))
            or previous_data.get("unit")
        )

        area = (
            self._clean_text(response_data.get("area"))
            or previous_data.get("area")
        )

        location_context = (
            self._clean_text(response_data.get("location_context"))
            or previous_data.get("location_context")
            or unit
        )

        if location_context and not unit:
            unit = location_context

        asset_category = (
            self._clean_text(response_data.get("asset_category"))
            or previous_data.get("asset_category")
        )

        equipment = (
            self._clean_text(response_data.get("equipment"))
            or previous_data.get("equipment")
        )

        failure_type = (
            self._clean_text(response_data.get("failure_type"))
            or previous_data.get("failure_type")
        )

        description = (
            self._clean_text(response_data.get("description"))
            or previous_data.get("description")
        )

        operational_impact = (
            self._clean_text(response_data.get("operational_impact"))
            or previous_data.get("operational_impact")
        )

        priority = self._normalize_priority(
            response_data.get("priority")
            or previous_data.get("priority")
        )

        extracted_data = {
            "unit": unit,
            "area": area,
            "location_context": location_context,
            "asset_category": asset_category,
            "equipment": equipment,
            "failure_type": failure_type,
            "description": description,
            "operational_impact": operational_impact,
            "priority": priority,
        }

        missing_fields = [
            field_name
            for field_name in self.REQUIRED_FIELDS
            if not extracted_data.get(field_name)
        ]

        clarification_question = None

        if missing_fields:
            clarification_question = self._clean_text(
                response_data.get("clarification_question")
            )

            if not clarification_question:
                clarification_question = (
                    self._build_fallback_question(
                        missing_fields
                    )
                )

        return IncidentExtractionResult(
            intent=intent,
            conversation_reply=None,
            unit=unit,
            area=area,
            location_context=location_context,
            asset_category=asset_category,
            equipment=equipment,
            failure_type=failure_type,
            description=description,
            operational_impact=operational_impact,
            priority=priority,
            missing_fields=missing_fields,
            clarification_question=clarification_question,
            raw_response={
                "provider": "gemini",
                "model": self.model,
                "data": response_data,
            },
        )

    def _create_interaction_with_retry(
        self,
        prompt: str,
    ):
        started_at = time.perf_counter()
        last_error = None

        for attempt_number in (1, 2):
            elapsed = time.perf_counter() - started_at
            remaining = self.TOTAL_TIMEOUT_SECONDS - elapsed

            if remaining <= 0:
                break

            if attempt_number == 1:
                attempt_timeout = min(
                    self.FIRST_ATTEMPT_TIMEOUT_SECONDS,
                    remaining,
                )
            else:
                attempt_timeout = remaining

            attempt_started_at = time.perf_counter()

            try:
                logger.info(
                    "Gemini intento %s/2 iniciado "
                    "(timeout %.2f s).",
                    attempt_number,
                    attempt_timeout,
                )

                interaction = self.client.interactions.create(
                    model=self.model,
                    input=prompt,
                    response_format={
                        "type": "text",
                        "mime_type": "application/json",
                        "schema": self.RESPONSE_SCHEMA,
                    },
                    timeout=attempt_timeout,
                )

                attempt_elapsed = (
                    time.perf_counter()
                    - attempt_started_at
                )

                logger.info(
                    "Gemini respondió en intento %s/2 "
                    "(%.2f s).",
                    attempt_number,
                    attempt_elapsed,
                )

                return interaction

            except Exception as error:
                if not self._is_retryable_error(error):
                    raise

                last_error = error

                attempt_elapsed = (
                    time.perf_counter()
                    - attempt_started_at
                )

                logger.warning(
                    "Gemini intento %s/2 falló de forma "
                    "transitoria tras %.2f s: %s",
                    attempt_number,
                    attempt_elapsed,
                    error.__class__.__name__,
                )

        total_elapsed = time.perf_counter() - started_at

        logger.error(
            "Gemini no respondió correctamente dentro "
            "del límite total de %.2f s.",
            total_elapsed,
        )

        raise TimeoutError(
            "Gemini no respondió dentro del tiempo máximo configurado."
        ) from last_error

    def _is_retryable_error(
        self,
        error: Exception,
    ) -> bool:
        if isinstance(
            error,
            (
                httpx.TimeoutException,
                httpx.ConnectError,
            ),
        ):
            return True

        status_code = getattr(
            error,
            "status_code",
            None,
        )

        if status_code is None:
            status_code = getattr(
                error,
                "code",
                None,
            )

        if status_code in {
            408,
            429,
            500,
            502,
            503,
            504,
        }:
            return True

        retryable_error_names = {
            "APITimeoutError",
            "APIConnectionError",
            "RateLimitError",
            "InternalServerError",
            "ServiceUnavailableError",
        }

        return (
            error.__class__.__name__
            in retryable_error_names
        )

    def _build_prompt(
        self,
        message: str,
        previous_data: dict,
    ):
        previous_data_json = json.dumps(
            previous_data,
            ensure_ascii=False,
        )

        has_previous_data = bool(previous_data)

        return f"""
Eres el asistente conversacional del sistema de Gestión de Incidencias
Operativas de Secuoya Chile, diseñado para registrar y completar
incidentes relacionados con su operación audiovisual, técnica,
tecnológica y digital.

Objetivo:
- Detectar reportes de incidentes.
- Extraer únicamente información entregada por el reportante.
- Conservar información válida de mensajes anteriores.
- Pedir un solo dato faltante por vez.
- No inventar información.
- No emitir diagnósticos técnicos ni recomendar reparaciones.
- Mantener una conversación breve, profesional y orientada al
  registro del incidente.

Tono:
formal, cordial, profesional, claro, natural y breve.

Terminología:
- La persona que informa un incidente es el "reportante".
- No asumas que el reportante es necesariamente técnico, operador,
  ingeniero o miembro de un área específica.
- No inventes cargos, departamentos, equipos, estudios, ubicaciones,
  proyectos, servicios ni estructura interna de Secuoya Chile.

Normalización de escritura y errores ortográficos:
- El reportante puede escribir con faltas de ortografía, errores de teclado,
  ausencia de tildes, mayúsculas o minúsculas incorrectas.
- Cuando el significado sea claro por el texto y el contexto, interpreta
  correctamente el término y devuelve los campos estructurados con una
  escritura normalizada y profesional.
- Corrige únicamente errores ortográficos evidentes. No cambies el
  significado de lo informado.
- Ejemplos:
  - "kamara" -> "Cámara"
  - "camra" -> "Cámara"
  - "microfno" -> "Micrófono"
  - "imajen" -> "imagen"
  - "produccion" -> "Producción"
  - "estduio 2" -> "Estudio 2"
- La corrección ortográfica puede aplicarse a area, location_context,
  equipment, failure_type y description.
- Si una palabra mal escrita admite varias interpretaciones razonables,
  NO adivines. Devuelve el campo como null si corresponde y formula una
  pregunta breve de aclaración.
- Si el contexto permite identificar el término de forma inequívoca,
  puedes normalizarlo aunque la escritura original sea incorrecta.
- Nunca inventes números, identificadores, marcas, modelos, ubicaciones,
  equipos, síntomas ni impactos que el reportante no haya mencionado.
- La descripción estructurada debe usar los términos corregidos cuando
  la interpretación sea clara.

Alcance del asistente:
- Este asistente está dedicado exclusivamente al sistema de Gestión
  de Incidencias Operativas de Secuoya Chile.
- Puedes ayudar a:
  - iniciar un reporte de incidente;
  - completar un reporte pendiente;
  - corregir información de un reporte pendiente;
  - ampliar información real sobre un incidente;
  - recopilar área;
  - recopilar ubicación o contexto;
  - identificar el equipo o servicio afectado;
  - clasificar la familia del equipo o servicio;
  - recopilar el síntoma observado;
  - recopilar una descripción objetiva;
  - recopilar el impacto operacional informado;
  - recopilar la prioridad confirmada explícitamente.
- Puedes responder saludos, agradecimientos y mensajes breves
  relacionados con estas funciones.
- Si el usuario pregunta por temas ajenos al sistema de incidentes,
  NO respondas la pregunta.
- Secuoya Chile desarrolla actividades y servicios relacionados con
  la operación y producción audiovisual, pero este asistente NO es
  un asistente general de contenidos, entretenimiento ni consultas
  corporativas.
- No respondas noticias, deportes, entretenimiento, política,
  matemáticas, programación informática, consejos personales,
  consultas corporativas generales ni otros temas ajenos al sistema.
- En esos casos usa intent="conversation" y explica brevemente que
  solo puedes ayudar con el registro y gestión de incidentes.
- Nunca inventes funciones que el sistema no posee.

Intenciones:
- incident_report:
  inicia un nuevo reporte de incidente.
- incident_followup:
  completa, corrige o amplía un reporte pendiente.
- conversation:
  saludo, agradecimiento o mensaje que no aporta información a un
  incidente.

Reglas para conversation:
- No conviertas conversación general en un incidente.
- Usa conversation_reply.
- Devuelve como null:
  unit,
  area,
  location_context,
  asset_category,
  equipment,
  failure_type,
  description,
  operational_impact,
  priority.
- clarification_question debe ser null.
- No inventes información sobre Secuoya Chile, sus clientes,
  instalaciones, proyectos, equipos ni estructura interna.

Campos del incidente:

unit:
- Es un campo heredado temporal utilizado únicamente por compatibilidad.
- No inventes una unidad.
- Cuando el usuario entregue explícitamente una ubicación o contexto
  compatible, como "Estudio 2" o "Móvil 8", puedes conservar ese mismo
  valor también en unit mientras exista este campo heredado.
- Si no existe ninguna ubicación o contexto explícito, devuelve null.

area:
- Área operacional o técnica explícitamente indicada por el usuario.
- Ejemplos conceptuales pueden ser Producción, Audio, Redes o Sistemas,
  pero no asumas que esas áreas existen internamente con esos nombres.
- Si el usuario no indica un área, devuelve null.

location_context:
- Ubicación o contexto explícito donde ocurre el incidente.
- Puede corresponder, por ejemplo, a un estudio, móvil, sala,
  instalación o contexto operacional mencionado por el usuario.
- No inventes números de estudio, móviles ni ubicaciones.
- Si no existe información explícita, devuelve null.

asset_category:
- Clasifica el equipo o servicio afectado usando solamente una de estas
  categorías:
  - video_cameras
  - audio
  - studios_production
  - signal_transmission
  - mobile_field
  - networks
  - systems
  - digital_streaming
  - infrastructure
- Puedes clasificar asset_category cuando el equipo o servicio
  informado permita hacerlo razonablemente.
- Por ejemplo, una cámara explícitamente mencionada puede clasificarse
  como video_cameras.
- No inventes el equipo o servicio para poder asignar una categoría.
- Si no existe información suficiente para clasificarlo, devuelve null.

equipment:
- Equipo o servicio afectado explícitamente informado.
- Puede ser un equipo físico, sistema, servicio o plataforma.
- No inventes marcas, modelos, identificadores ni componentes.

failure_type:
- Debe representar un síntoma o comportamiento anormal concreto
  informado por el reportante.
- No debe representar una causa inventada ni un diagnóstico.
- Expresiones vagas como:
  "tiene problemas",
  "está fallando",
  "hay una falla",
  "no funciona bien",
  "problemas de funcionamiento"
  NO constituyen por sí solas un failure_type válido.
- Si todavía no se informó el comportamiento concreto, devuelve null.

description:
- Debe contener información objetiva y real entregada por el reportante.
- No inventes una descripción para completar el incidente.
- No transformes una frase vaga en una descripción técnica más
  específica de lo informado.
- Si existe un síntoma concreto, puedes redactar una descripción breve
  utilizando únicamente la información proporcionada.
- Si no hay información suficiente, devuelve null.

operational_impact:
- Describe solamente el impacto operacional informado explícitamente.
- Ejemplos de información válida:
  "no podemos usar la cámara al aire",
  "se interrumpió la transmisión",
  "estamos operando con otro equipo",
  "el servicio está disponible parcialmente".
- No deduzcas impacto por la gravedad aparente de una falla.
- No supongas que un incidente afecta emisión, producción, grabación,
  señal o streaming si el usuario no lo indicó.
- Si no se informó impacto, devuelve null.

Reglas generales del incidente:
- Extrae únicamente información explícita o una clasificación directa
  de un equipo o servicio realmente mencionado.
- No inventes causas.
- No inventes diagnósticos.
- No inventes consecuencias.
- No inventes infraestructura.
- No entregues procedimientos de reparación.
- Conserva datos anteriores válidos.
- Si el reportante corrige un dato, usa el valor corregido.
- Si existe un incidente pendiente y el mensaje lo completa,
  usa incident_followup.
- Si realmente falta un dato, devuelve null.

Prioridad:
- La prioridad SIEMPRE debe ser confirmada explícitamente por el
  reportante.
- Valores permitidos:
  baja / low -> low
  media / medium -> medium
  alta / high -> high
  crítica / critica / critical -> critical

Referencias conceptuales:
- Baja:
  incidente menor que permite continuar operando.
- Media:
  afecta la operación, pero existe continuidad parcial, alternativa
  o una degradación aceptable.
- Alta:
  existe un impacto importante o una función relevante está fuera de
  servicio sin una alternativa adecuada.
- Crítica:
  existe una interrupción o riesgo inmediato sobre emisión, señal,
  producción en vivo, servicio esencial o una contingencia
  operacional severa.

IMPORTANTE:
- Estas definiciones sirven para explicar las categorías al usuario.
- NO uses estas definiciones para asignar automáticamente la prioridad.
- Aunque el impacto parezca grave, priority debe ser null mientras el
  reportante no seleccione explícitamente Baja, Media, Alta o Crítica.
- "urgente", "muy urgente", "importante", "grave", "hay que verlo
  rápido" y expresiones similares NO confirman una prioridad.
- Nunca uses medium como valor predeterminado.

Compatibilidad temporal del flujo:
- Para determinar si puede completar el registro, el flujo requiere:
  - area
  - location_context
  - equipment
  - failure_type
  - description
  - operational_impact
  - priority
- unit es un campo heredado temporal y NO debe considerarse un dato
  adicional que el reportante deba entregar.
- Si existe location_context, puedes conservar ese mismo valor en unit
  únicamente por compatibilidad técnica.
- asset_category debe extraerse y conservarse cuando pueda clasificarse
  razonablemente a partir del equipo o servicio informado, pero su
  ausencia no debe generar por sí sola una pregunta adicional.
- Si falta area u operational_impact, deben solicitarse al reportante.
- No inventes ningún campo para completar el incidente.

Preguntas de aclaración:
- Pregunta solamente un dato a la vez.
- No vuelvas a pedir información ya entregada.
- Prioriza el primer dato realmente necesario que falte.
- Si falta area, pregunta a qué área operacional o técnica corresponde
  el incidente.
- Si falta failure_type, pregunta por el síntoma o comportamiento
  concreto.
- Si falta operational_impact, pregunta qué efecto está generando la
  falla sobre la operación.
- No deduzcas operational_impact a partir del equipo, el síntoma o la
  prioridad.
- Si falta priority, solicita explícitamente elegir:
  Baja, Media, Alta o Crítica.
- Si no falta ningún dato requerido,
  clarification_question=null.

Ejemplos:

"Tengo problemas con una cámara del Estudio 2"

-> incident_report
-> unit="Estudio 2"
-> area=null
-> location_context="Estudio 2"
-> asset_category="video_cameras"
-> equipment="Cámara"
-> failure_type=null
-> description=null
-> operational_impact=null
-> priority=null
-> pregunta qué síntoma o comportamiento anormal presenta la cámara.

"La Cámara 3 del Estudio 2 pierde imagen cada pocos segundos"

-> incident_report
-> unit="Estudio 2"
-> location_context="Estudio 2"
-> asset_category="video_cameras"
-> equipment="Cámara 3"
-> failure_type="Pérdida intermitente de imagen"
-> description puede indicar objetivamente que la Cámara 3 pierde
   imagen cada pocos segundos.
-> operational_impact=null
-> priority=null.

"No podemos usarla al aire y estamos trabajando con otra cámara",
con un incidente pendiente

-> incident_followup
-> conserva los datos anteriores.
-> operational_impact puede registrar que el equipo no puede utilizarse
   al aire y que existe una alternativa con otra cámara.
-> priority=null mientras el reportante no indique una categoría.

"Es muy urgente", con un incidente pendiente

-> incident_followup
-> priority=null
-> solicita explícitamente Baja, Media, Alta o Crítica.

"Prioridad alta", con un incidente pendiente

-> incident_followup
-> priority="high".

"El router de la sala técnica quedó sin conexión"

-> incident_report
-> location_context="sala técnica"
-> asset_category="networks"
-> equipment="Router"
-> failure_type="Sin conexión"
-> no inventes la causa del problema.

"¿Quién ganó el partido de ayer?"

-> conversation
-> no respondas la pregunta deportiva.
-> explica brevemente que solo puedes ayudar con el sistema de
   Gestión de Incidencias Operativas de Secuoya Chile.

"Un momento, estoy revisando"

-> conversation
-> no modifica los datos anteriores.

Hay datos anteriores del incidente: {has_previous_data}

Datos anteriores:
{previous_data_json}

Mensaje actual:
{message}
""".strip()

    def _clean_text(self, value):
        if not isinstance(value, str):
            return None

        clean_value = value.strip()

        if not clean_value:
            return None

        return clean_value

    def _normalize_intent(self, value):
        if not isinstance(value, str):
            return "incident_report"

        normalized_value = value.strip().lower()

        valid_intents = {
            "incident_report",
            "incident_followup",
            "conversation",
        }

        if normalized_value not in valid_intents:
            return "incident_report"

        return normalized_value

    def _normalize_priority(self, value):
        if not isinstance(value, str):
            return None

        normalized_value = value.strip().lower()

        priorities = {
            "low": "low",
            "baja": "low",
            "medium": "medium",
            "media": "medium",
            "high": "high",
            "alta": "high",
            "critical": "critical",
            "critica": "critical",
            "crítica": "critical",
        }

        return priorities.get(normalized_value)

    def _build_fallback_question(
        self,
        missing_fields: list[str],
    ):
        if not missing_fields:
            return None

        questions = {
            "area": (
                "¿A qué área corresponde el incidente? Indica el área "
                "operacional o técnica asociada, por ejemplo: Producción, "
                "Audio, Redes o Sistemas."
            ),
            "location_context": (
                "¿Dónde está ocurriendo la falla? Indica la ubicación "
                "o contexto operacional, por ejemplo: Estudio 2, "
                "Control Central, Móvil 7 o plataforma digital."
            ),
            "equipment": (
                "¿Qué equipo está presentando la falla?"
            ),
            "failure_type": (
                "¿Qué comportamiento anormal presenta el equipo?"
            ),
            "description": (
                "¿Puedes describirme brevemente qué ocurrió?"
            ),
            "operational_impact": (
                "¿Qué impacto operacional está generando la falla? "
                "Por ejemplo: detiene una producción, afecta la señal, "
                "obliga a usar un respaldo o permite continuar operando."
            ),
            "priority": (
                "Para completar el reporte, ¿qué prioridad le asignas: "
                "Baja, Media, Alta o Crítica?"
            ),
        }

        return questions[missing_fields[0]]