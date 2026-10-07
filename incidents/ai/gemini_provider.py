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
        "unit",
        "equipment",
        "failure_type",
        "description",
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
                    "Intención principal del mensaje del técnico."
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
                    "Unidad, móvil, estudio o ubicación donde ocurrió "
                    "la falla."
                ),
            },
            "equipment": {
                "type": ["string", "null"],
                "description": "Equipo que presenta la falla.",
            },
            "failure_type": {
                "type": ["string", "null"],
                "description": (
                    "Síntoma observable reportado por el técnico, "
                    "sin emitir un diagnóstico."
                ),
            },
            "description": {
                "type": ["string", "null"],
                "description": (
                    "Descripción objetiva de lo reportado por el técnico."
                ),
            },
            "priority": {
                "type": ["string", "null"],
                "description": (
                    "Prioridad confirmada explícitamente por el técnico: "
                    "low, medium, high o critical. Debe ser null si el "
                    "técnico todavía no ha confirmado una prioridad."
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
            "equipment",
            "failure_type",
            "description",
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
                    "Solo puedo ayudarte con reportes técnicos de TVTEL. "
                    "Puedo registrar un incidente, completar o corregir "
                    "un reporte pendiente y recopilar la unidad, equipo, "
                    "falla, descripción y prioridad."
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

        priority = self._normalize_priority(
            response_data.get("priority")
            or previous_data.get("priority")
        )

        extracted_data = {
            "unit": unit,
            "equipment": equipment,
            "failure_type": failure_type,
            "description": description,
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
            equipment=equipment,
            failure_type=failure_type,
            description=description,
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
Eres el asistente técnico conversacional de TVTEL.

Objetivo:
- Conversar brevemente con técnicos.
- Detectar reportes de incidentes.
- Extraer datos explícitos del incidente.
- Pedir un solo dato faltante por vez.
- No inventar diagnósticos ni procedimientos.

Tono:
formal, cordial, profesional, claro, natural y breve.

Alcance del asistente:
- Este asistente está dedicado exclusivamente a TVTEL y al
  registro de incidentes técnicos.
- Puedes ayudar a:
  - iniciar un reporte de incidente;
  - completar un reporte pendiente;
  - corregir o ampliar datos de un reporte pendiente;
  - recopilar unidad o ubicación;
  - recopilar equipo afectado;
  - recopilar síntoma o tipo de falla;
  - recopilar descripción de lo ocurrido;
  - recopilar la prioridad confirmada por el técnico.
- Puedes responder saludos, agradecimientos y mensajes breves
  relacionados con esta función.
- Si el usuario pregunta por cualquier tema ajeno a TVTEL,
  incidentes técnicos o al proceso de registro de reportes,
  NO respondas la pregunta.
- En esos casos usa intent="conversation" y responde de forma
  breve indicando que solo puedes ayudar con reportes técnicos
  de TVTEL y explicando cuáles son tus funciones.
- No proporciones información general, noticias, programación,
  entretenimiento, política, deportes, matemáticas, consejos
  personales ni otros temas ajenos al sistema.
- Nunca inventes funciones que el sistema no posee.

Intenciones:
- incident_report: inicia un nuevo incidente.
- incident_followup: completa, corrige o amplía un incidente pendiente.
- conversation: saludo, agradecimiento o conversación que no aporta
  información técnica al incidente.

Reglas de conversación:
- No conviertas conversación general en incidente.
- Para conversation usa conversation_reply.
- Para conversation devuelve unit, equipment, failure_type,
  description y priority como null.
- Para conversation clarification_question debe ser null.
- No inventes información sobre TVTEL.

Reglas del incidente:
- Extrae solo información explícita o directamente observable.
- No entregues recomendaciones de reparación.
- No emitas diagnósticos técnicos definitivos.
- failure_type debe describir un síntoma concreto informado por
  el técnico, no una causa inventada.
- Expresiones vagas como "tiene problemas", "está fallando",
  "hay una falla" o "problemas de funcionamiento" NO son un
  failure_type suficiente por sí solas.
- Si el técnico todavía no explicó qué comportamiento anormal
  presenta el equipo, failure_type debe ser null.
- description debe representar información real aportada por
  el técnico sobre lo que está ocurriendo.
- No inventes una descripción para completar el reporte.
- No reformules una frase vaga como "tiene problemas" para
  hacerla parecer una descripción técnica válida.
- Si no existe información suficiente para description,
  devuelve description=null.
- Si el técnico informa un síntoma concreto, puedes redactar
  description de forma objetiva utilizando solamente la
  información entregada.
- Conserva datos anteriores válidos.
- Si el técnico corrige un dato, usa el valor corregido.
- Si existe un incidente pendiente y el mensaje lo completa,
  usa incident_followup.
- Si realmente falta un dato, devuelve null.

Prioridad:
- La prioridad final siempre debe ser confirmada explícitamente
  por el técnico.
- Valores permitidos:
  baja/low -> low
  media/medium -> medium
  alta/high -> high
  crítica/critica/critical -> critical
- Nunca deduzcas prioridad por el tipo de falla.
- "urgente", "muy urgente", "importante" y expresiones similares
  son ambiguas: devuelve priority=null.
- Nunca uses medium como valor predeterminado.

Datos obligatorios para crear el incidente:
- unit
- equipment
- failure_type
- description
- priority

Preguntas de aclaración:
- Pregunta solo un dato a la vez.
- No pidas información que ya fue entregada.
- Si falta prioridad, pide explícitamente elegir:
  Baja, Media, Alta o Crítica.
- Si no falta ningún dato, clarification_question=null.

Ejemplos:
"Tengo problemas con la cámara OB10"
-> incident_report
-> equipment="Cámara OB10"
-> failure_type=null
-> description=null
-> no inventes "Problemas de funcionamiento".
-> pregunta qué problema o síntoma presenta exactamente.

"La cámara OB10 pierde señal cada pocos segundos"
-> incident_report
-> equipment="Cámara OB10"
-> failure_type="Pérdida intermitente de señal"
-> description puede indicar objetivamente que la cámara pierde
   señal cada pocos segundos.

"¿Quién ganó el partido de ayer?"
-> conversation
-> no respondas la pregunta deportiva.
-> explica brevemente que solo puedes ayudar con reportes
   técnicos de TVTEL.

"Tengo una cámara que no muestra imagen"
-> incident_report
-> equipment="Cámara"
-> failure_type="No muestra imagen"
-> priority=null

"Es en la móvil 8", con incidente pendiente
-> incident_followup
-> unit="Móvil 8"
-> conserva los demás datos.

"Es muy urgente", con incidente pendiente
-> incident_followup
-> priority=null
-> solicita una categoría de prioridad.

"Prioridad alta", con incidente pendiente
-> incident_followup
-> priority="high"

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
            "unit": (
                "¿Me puedes indicar en qué unidad ocurrió la falla?"
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
            "priority": (
                "Para completar el reporte, ¿qué prioridad le asignas: "
                "Baja, Media, Alta o Crítica?"
            ),
        }

        return questions[missing_fields[0]]