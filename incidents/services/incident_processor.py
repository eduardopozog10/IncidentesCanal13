import re
import unicodedata

from django.db import transaction
from django.utils import timezone

from incidents.ai.factory import get_ai_provider
from incidents.ai.base import AIConfigurationError, AITemporaryError
from incidents.models import (
    ChannelMessage,
    Incident,
    IncidentDraft,
    Technician,
)
from incidents.services.conversation_lock import conversation_lock
from incidents.models import IncidentNotification


class _ExtractionNeeded(Exception):
    def __init__(self, message, previous_data):
        self.message, self.previous_data = message, previous_data


class IncidentProcessor:
    REQUIRED_FIELDS = (
        "area",
        "location_context",
        "equipment",
        "failure_type",
        "description",
        "operational_impact",
        "priority",
    )
    CHANGE_LABELS = {
        "area": "Área", "location_context": "Ubicación", "equipment": "Equipo",
        "asset_category": "Familia", "failure_type": "Síntoma",
        "description": "Descripción", "operational_impact": "Impacto operacional", "priority": "Prioridad",
    }

    AREA_CANONICAL_LABELS = {
        "audio": "Audio",
        "produccion": "Producción",
        "operaciones": "Operaciones",
        "redes": "Redes",
        "sistemas": "Sistemas",
        "transmision": "Transmisión",
        "senal": "Señal",
        "video": "Video",
        "camaras": "Cámaras",
        "iluminacion": "Iluminación",
        "infraestructura": "Infraestructura",
        "digital": "Digital",
        "streaming": "Streaming",
        "estudios": "Estudios",
        "control tecnico": "Control Técnico",
        "tecnologia": "Tecnología",
    }

    PENDING_MESSAGES_KEY = "_pending_messages"

    FALLBACK_QUESTIONS = {
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
        "equipment": "¿Qué equipo presenta la falla?",
        "failure_type": (
            "¿Qué problema presenta exactamente el equipo? "
            "Por ejemplo: pérdida de señal, imagen intermitente, "
            "no enciende, ruido, cortes u otro síntoma."
        ),
        "description": (
            "¿Puedes describir con un poco más de detalle "
            "qué está ocurriendo con el equipo?"
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

    GUIDED_QUESTIONS = {
        "area": (
            "Necesito saber a qué área corresponde la falla. "
            "Por ejemplo: Producción, Audio o Redes. "
            "¿A qué área corresponde el incidente?"
        ),
        "location_context": (
            "Me refiero al lugar donde ocurre la falla. "
            "Por ejemplo: Estudio 2, Control Central o Móvil 7. "
            "¿Cuál es la ubicación del incidente?"
        ),
        "equipment": (
            "Necesito identificar el equipo o servicio afectado. "
            "Por ejemplo: Cámara 3, Consola de audio o Router principal. "
            "¿Qué equipo presenta la falla?"
        ),
        "failure_type": (
            "Necesito saber qué comportamiento anormal observaste. "
            "Por ejemplo: imagen intermitente, sin audio o no enciende. "
            "¿Qué problema presenta exactamente el equipo?"
        ),
        "description": (
            "Necesito que describas brevemente qué ocurre. "
            "Por ejemplo: la imagen se corta durante varios segundos "
            "y luego vuelve. ¿Puedes describir qué está pasando?"
        ),
        "operational_impact": (
            "Me refiero a cómo afecta la falla a tu trabajo. "
            "Por ejemplo: interrumpe una grabación, obliga a usar "
            "un respaldo o permite continuar normalmente. "
            "¿Qué impacto operacional tiene?"
        ),
        "priority": (
            "Necesito que elijas una prioridad explícitamente. "
            "Por ejemplo, puedes responder: Alta. "
            "¿Qué prioridad seleccionas: Baja, Media, Alta o Crítica?"
        ),
    }

    CORRECTION_PROMPT = (
        "De acuerdo. Indícame qué dato quieres corregir "
        "antes de crear el incidente."
    )

    CORRECTION_QUESTIONS = {
        "area": (
            "Entendido. ¿Cuál es el área correcta?"
        ),
        "location_context": (
            "Entendido. ¿Cuál es la ubicación o contexto correcto?"
        ),
        "equipment": (
            "Entendido. ¿Qué equipo o servicio es el correcto?"
        ),
        "failure_type": (
            "Entendido. ¿Cuál es el síntoma correcto?"
        ),
        "description": (
            "Entendido. ¿Cuál es la descripción correcta?"
        ),
        "operational_impact": (
            "Entendido. ¿Cuál es el impacto operacional correcto?"
        ),
        "priority": (
            "Entendido. ¿Cuál es la prioridad correcta: "
            "Baja, Media, Alta o Crítica?"
        ),
    }

    CORRECTION_FIELD_ALIASES = {
        "area": (
            "area",
        ),
        "location_context": (
            "ubicacion",
            "contexto",
            "lugar",
        ),
        "equipment": (
            "equipo",
            "servicio",
        ),
        "failure_type": (
            "sintoma",
        ),
        "description": (
            "descripcion",
        ),
        "operational_impact": (
            "impacto operacional",
            "impacto",
        ),
        "priority": (
            "prioridad",
        ),
    }

    GENERIC_FAILURE_TYPES = {
        "problema",
        "problemas",
        "problema de funcionamiento",
        "problemas de funcionamiento",
        "falla",
        "falla general",
        "falla de funcionamiento",
        "mal funcionamiento",
    }

    GENERIC_DESCRIPTION_MARKERS = (
        "se reportan problemas de funcionamiento",
        "se reporta un problema de funcionamiento",
        "presenta problemas de funcionamiento",
        "se presentan problemas de funcionamiento",
    )

    PRIORITY_GUIDANCE = (
        "Para finalizar el reporte necesito que selecciones una prioridad:\n\n"
        "• Baja: incidente menor, sin impacto operacional relevante y que "
        "puede esperar.\n"
        "• Media: existe afectación, pero la operación puede mantenerse "
        "mediante una alternativa, redundancia o una degradación aceptable.\n"
        "• Alta: una función importante está fuera de servicio o existe un "
        "impacto significativo en la operación o producción, sin una "
        "alternativa adecuada.\n"
        "• Crítica: existe una interrupción o riesgo inmediato para la "
        "emisión, señal, producción en vivo, un servicio esencial o una "
        "contingencia operacional severa.\n\n"
        "La prioridad debe ser seleccionada explícitamente por ti. "
        "¿Cuál corresponde: Baja, Media, Alta o Crítica?"
    )

    AI_TIMEOUT_REPLY = (
        "El procesamiento está tardando más de lo esperado. "
        "Guardé la información enviada y el reporte sigue pendiente. "
        "En unos segundos puedes escribir “continuar” o enviar el "
        "siguiente dato; no es necesario repetir lo anterior."
    )

    AI_TIMEOUT_INITIAL_REPLY = (
        "El procesamiento está tardando más de lo esperado y no pude "
        "determinar si tu mensaje corresponde a un incidente. "
        "Por favor, vuelve a enviar el reporte en unos segundos."
    )

    CONFIRMATION_POSITIVE = {
        "si",
        "si confirmo",
        "si confirmo los datos",
        "si confirmo la informacion",
        "confirmo",
        "confirmo los datos",
        "confirmo la informacion",
        "confirmo el reporte",
        "confirmado",
        "correcto",
        "todo correcto",
        "esta correcto",
        "esta todo correcto",
        "los datos son correctos",
        "crear reporte",
        "crear incidente",
        "esta bien",
        "bien",
        "ok",
    }

    CONFIRMATION_NEGATIVE = {
        "no",
        "no confirmo",
        "quiero corregir",
        "corregir",
        "quiero cambiar",
        "cambiar",
        "esta mal",
    }

    def __init__(self, ai_provider=None, *, require_delivered_summary=False):
        self.ai_provider = ai_provider or get_ai_provider()
        self.require_delivered_summary = require_delivered_summary

    def process(self, message: ChannelMessage):
        with conversation_lock(f"reporter:{message.technician_id}"):
            outcome = None
            for _ in range(3):
                try:
                    with transaction.atomic():
                        before = IncidentDraft.objects.filter(technician_id=message.technician_id,
                            status__in=["collecting", "ready"]).order_by("-updated_at").first()
                        previous = dict(before.extracted_data or {}) if before else None
                        was_correcting = self._is_draft_correction_mode(before)
                        result = self._process_atomic(message, outcome)
                        result = self._add_saved_changes(message, result, previous, was_correcting)
                        ChannelMessage.objects.filter(pk=message.pk).update(processing_result=result)
                        ChannelMessage.objects.filter(in_reply_to=message).update(draft_id=message.draft_id, incident_id=message.incident_id)
                    return result
                except _ExtractionNeeded as needed:
                    # El intento de planificación se revierte antes de Gemini.
                    # La exclusión lógica por reportante conserva el contexto.
                    try:
                        extracted = self.ai_provider.extract_incident(message=needed.message, previous_data=needed.previous_data)
                        outcome = (needed.message, needed.previous_data, extracted, None)
                    except (AITemporaryError, AIConfigurationError):
                        # El servicio conserva tipo de fallo y pausa indicada.
                        raise
                    except (TimeoutError, ValueError, ConnectionError) as error:
                        outcome = (needed.message, needed.previous_data, None, error)
                    message.refresh_from_db()
            raise RuntimeError("El contexto cambió repetidamente; mensaje pendiente de recuperación.")

    def _add_saved_changes(self, message, result, previous, was_correcting):
        """Comparar con PostgreSQL dentro del mismo commit que cachea la respuesta.

        No se anuncia una interpretación de IA: se anuncia el borrador guardado.
        El resultado cacheado conserva esta misma respuesta para los replays.
        """
        if previous is None or result.get("action") != "ask_clarification" or not result.get("draft_id"):
            return result
        draft = IncidentDraft.objects.get(pk=result["draft_id"])
        current = draft.extracted_data
        changes = [{"field": name, "previous": previous.get(name), "value": current.get(name)}
                   for name in self.CHANGE_LABELS if previous.get(name) != current.get(name)]
        is_correction = was_correcting or self._looks_like_natural_correction_statement(message.content)
        is_correction = is_correction or any(previous.get(item["field"]) for item in changes)
        if not is_correction:
            return result
        result["changes"] = changes
        prefix = None
        if changes:
            lines = []
            for change in changes:
                name, value = change["field"], change["value"]
                if name == "priority" and value:
                    value = Incident.Priority(value).label
                elif name == "asset_category" and value:
                    value = Incident.AssetCategory(value).label
                lines.append(f"• {self.CHANGE_LABELS[name]}: {value or 'Por confirmar'}")
            prefix = "Entendido, actualicé:\n\n" + "\n".join(lines)
        elif result.get("unchanged_fields"):
            prefix = "Ese dato ya estaba registrado correctamente."
        if prefix:
            result["reply"] = prefix + "\n\n" + result["reply"]
            # La autorización de creación exige haber entregado exactamente
            # este resumen, incluida la confirmación de los cambios guardados.
            if draft.status == IncidentDraft.Status.READY:
                draft.last_question = result["reply"]
                draft.save(update_fields=["last_question", "updated_at"])
        return result

    @transaction.atomic
    def _process_atomic(self, message: ChannelMessage, outcome=None):
        if message.technician is None:
            raise ValueError(
                "El mensaje debe estar asociado a un reportante."
            )

        # Bloquear al reportante, incluso antes de que exista un borrador.
        # Así se serializan webhooks, polling y confirmaciones concurrentes.
        Technician.objects.select_for_update().get(pk=message.technician_id)

        message.status = ChannelMessage.Status.PROCESSING
        message.save(update_fields=["status"])

        draft = (
            IncidentDraft.objects.filter(
                technician=message.technician,
                status__in=[
                    IncidentDraft.Status.COLLECTING,
                    IncidentDraft.Status.READY,
                ],
            )
            .order_by("-updated_at")
            .first()
        )

        normalized_message = self._normalize_quick_text(message.content)
        if draft is None and normalized_message in self.CONFIRMATION_POSITIVE:
            last_draft = IncidentDraft.objects.filter(
                technician=message.technician,
            ).order_by("-updated_at").first()
            incident = (
                last_draft.incident
                if last_draft and last_draft.status == IncidentDraft.Status.COMPLETED
                else None
            )
            message.draft = last_draft if incident else None
            message.incident = incident
            message.status = ChannelMessage.Status.PROCESSED
            message.processed_at = timezone.now()
            message.save(update_fields=["draft", "incident", "status", "processed_at"])
            return {
                "action": "incident_already_created" if incident else "conversation",
                "incident_created": False,
                "incident_code": incident.code if incident else None,
                "reply": (
                    f"El reporte {incident.code} ya fue creado. Puedes contarme otra falla si necesitas un nuevo reporte."
                    if incident else
                    "Primero cuéntame qué falla necesitas reportar. Revisaremos el resumen antes de crear el incidente."
                ),
            }

        if (
            draft is not None
            and draft.status == IncidentDraft.Status.READY
        ):
            confirmation_result = (
                self._handle_ready_draft_confirmation(
                    message=message,
                    draft=draft,
                )
            )

            if confirmation_result is not None:
                return confirmation_result

        quick_reply = self._get_quick_conversation_reply(
            message.content
        )

        if quick_reply is not None:
            message.status = ChannelMessage.Status.PROCESSED
            message.processed_at = timezone.now()
            message.save(
                update_fields=[
                    "status",
                    "processed_at",
                ]
            )

            return {
                "action": "conversation",
                "incident_created": False,
                "draft_id": draft.pk if draft else None,
                "reply": quick_reply,
            }

        raw_previous_data = (
            dict(draft.extracted_data or {})
            if draft
            else {}
        )

        pending_messages = self._get_pending_messages(
            raw_previous_data
        )

        previous_data = {
            key: value
            for key, value in raw_previous_data.items()
            if key != self.PENDING_MESSAGES_KEY
        }

        help_field = self._get_pending_help_field(draft)

        if (
            help_field is not None
            and self._is_clarification_request(message.content)
        ):
            reply = self.GUIDED_QUESTIONS[help_field]

            draft.last_question = reply
            draft.save(
                update_fields=[
                    "last_question",
                    "updated_at",
                ]
            )

            message.draft = draft
            message.status = ChannelMessage.Status.PROCESSED
            message.processed_at = timezone.now()
            message.save(
                update_fields=[
                    "draft",
                    "status",
                    "processed_at",
                ]
            )

            return {
                "action": "ask_clarification",
                "incident_created": False,
                "draft_id": draft.pk,
                "missing_fields": draft.missing_fields,
                "reply": reply,
                "extracted_data": previous_data,
            }

        correction_mode = self._is_draft_correction_mode(
            draft
        )
        if draft is not None and self._looks_like_natural_correction_statement(message.content):
            correction_mode = True
        current_evidence = None
        unchanged_fields = []
        requires_update_clarification = False

        direct_draft_result = self._try_process_direct_draft_reply(
            draft=draft,
            text=message.content,
            previous_data=previous_data,
            pending_messages=pending_messages,
        )

        if direct_draft_result is not None:
            extracted_data, clarification_question = (
                direct_draft_result
            )
            if correction_mode and extracted_data == previous_data and clarification_question is None:
                unchanged_fields = [self._infer_correction_target_field(draft=draft, text=message.content, previous_data=previous_data)]

        elif (
            self._is_waiting_only_for_priority(draft)
            and not correction_mode
            and not self._looks_like_compound_reply(message.content)
            and not self._has_operational_information(message.content, previous_data)
        ):
            priority = self._extract_explicit_priority(
                message.content
            )

            if priority is None:
                draft.last_question = self.PRIORITY_GUIDANCE
                draft.status = IncidentDraft.Status.COLLECTING
                draft.save(
                    update_fields=[
                        "last_question",
                        "status",
                        "updated_at",
                    ]
                )

                message.draft = draft
                message.status = ChannelMessage.Status.PROCESSED
                message.processed_at = timezone.now()
                message.save(
                    update_fields=[
                        "draft",
                        "status",
                        "processed_at",
                    ]
                )

                return {
                    "action": "ask_clarification",
                    "incident_created": False,
                    "draft_id": draft.pk,
                    "missing_fields": ["priority"],
                    "reply": self.PRIORITY_GUIDANCE,
                    "extracted_data": previous_data,
                }

            extracted_data = {
                "unit": previous_data.get("unit"),
                "area": self._normalize_area_value(
                    previous_data.get("area")
                ),
                "location_context": previous_data.get(
                    "location_context"
                ),
                "asset_category": previous_data.get(
                    "asset_category"
                ),
                "equipment": previous_data.get("equipment"),
                "failure_type": previous_data.get(
                    "failure_type"
                ),
                "description": previous_data.get(
                    "description"
                ),
                "operational_impact": previous_data.get(
                    "operational_impact"
                ),
                "priority": priority,
            }

            clarification_question = None

        else:
            ai_message = self._build_ai_message(
                current_message=message.content,
                pending_messages=pending_messages,
            )

            if outcome is None or outcome[:2] != (ai_message, previous_data):
                raise _ExtractionNeeded(ai_message, previous_data)
            try:
                if outcome[3] is not None:
                    raise outcome[3]
                extraction = outcome[2]
            except (TimeoutError, ValueError, ConnectionError):
                return self._handle_ai_timeout(
                    message=message,
                    draft=draft,
                    previous_data=previous_data,
                    pending_messages=pending_messages,
                )

            if not previous_data.get("priority") and extraction.priority:
                declared = {
                    self._extract_explicit_priority(text)
                    for text in [message.content, *pending_messages]
                }
                if extraction.priority not in declared:
                    # El proveedor no puede decidir una prioridad sin una
                    # declaración explícita del reportante.
                    extraction.priority = None

            if (
                extraction.intent == "conversation"
                and not correction_mode
            ):
                reply = (
                    extraction.conversation_reply
                    or "De acuerdo. ¿En qué puedo ayudarte?"
                )

                message.status = ChannelMessage.Status.PROCESSED
                message.processed_at = timezone.now()
                message.save(
                    update_fields=[
                        "status",
                        "processed_at",
                    ]
                )

                return {
                    "action": "conversation",
                    "incident_created": False,
                    "draft_id": draft.pk if draft else None,
                    "reply": reply,
                }

            current_evidence = getattr(extraction, "field_evidence", None)
            update_kind = getattr(extraction, "update_kind", None)
            if draft and (update_kind in {"ambiguous", "new_incident"} or self._is_uncertain_update(message.content)):
                correction_mode = True
                requires_update_clarification = True
                extracted_data = dict(previous_data)
                clarification_question = (
                    "¿Se trata de otra incidencia? Mantengo el borrador actual. Si deseas descartarlo para comenzar otro, utiliza /cancelar."
                    if update_kind == "new_incident" else
                    extraction.clarification_question or "¿Cuál es el dato correcto y su nuevo valor? Mantengo los datos anteriores hasta que lo aclares."
                )
            elif (correction_mode and update_kind != "additional") or (draft and update_kind in {"correction", "evolution"}):
                correction_mode = True
                correction_target_field = (
                    self._infer_correction_target_field(
                        draft=draft,
                        text=message.content,
                        previous_data=previous_data,
                    )
                )
                if self._contains_multiple_fields(message.content):
                    correction_target_field = None
                if current_evidence and len(current_evidence) > 1:
                    correction_target_field = None

                (
                    extracted_data,
                    correction_applied,
                ) = self._merge_ai_correction(
                    previous_data=previous_data,
                    extraction=extraction,
                    message_text=message.content,
                    target_field=correction_target_field,
                )

                if correction_applied:
                    clarification_question = None

                else:
                    unchanged_fields = [name for name in (current_evidence or {})
                                        if getattr(extraction, name, None) and previous_data.get(name)
                                        and extracted_data.get(name) == previous_data.get(name)]
                    clarification_question = (
                        self.CORRECTION_QUESTIONS.get(
                            correction_target_field,
                            self.CORRECTION_PROMPT,
                        )
                    )
                    if unchanged_fields:
                        clarification_question = None

            else:
                previous_location = previous_data.get(
                    "location_context"
                )

                updated_location = self._choose_refined_value(
                    previous_value=previous_location,
                    candidate_value=extraction.location_context,
                    field_name="location_context",
                )

                updated_equipment = self._choose_refined_value(
                    previous_value=previous_data.get(
                        "equipment"
                    ),
                    candidate_value=extraction.equipment,
                    field_name="equipment",
                )

                unit = (
                    previous_data.get("unit")
                    or extraction.unit
                )

                if (
                    previous_location
                    and updated_location
                    != previous_location
                ):
                    unit = updated_location

                extracted_data = {
                    "unit": unit,
                    "area": self._normalize_area_value(
                        previous_data.get("area")
                        or extraction.area
                    ),
                    "location_context": updated_location,
                    "asset_category": (
                        previous_data.get(
                            "asset_category"
                        )
                        or extraction.asset_category
                    ),
                    "equipment": updated_equipment,
                    "failure_type": (
                        previous_data.get(
                            "failure_type"
                        )
                        or extraction.failure_type
                    ),
                    "description": (
                        extraction.description
                        or previous_data.get(
                            "description"
                        )
                    ),
                    "operational_impact": (
                        previous_data.get(
                            "operational_impact"
                        )
                        or extraction.operational_impact
                    ),
                    "priority": (
                        previous_data.get(
                            "priority"
                        )
                        or extraction.priority
                    ),
                }

                clarification_question = (
                    extraction.clarification_question
                )
                if draft and update_kind == "additional":
                    # Un dato complementario no reemplaza otro ya válido.
                    for name, old_value in previous_data.items():
                        if old_value:
                            extracted_data[name] = old_value
                    detail = (current_evidence or {}).get("description")
                    if detail and detail.casefold() not in str(previous_data.get("description", "")).casefold():
                        extracted_data["description"] = f"{previous_data.get('description') or ''} {detail}".strip()

        extracted_data["asset_category"] = (
            self._refine_asset_category_from_equipment(
                equipment=extracted_data.get("equipment"),
                current_category=extracted_data.get(
                    "asset_category"
                ),
            )
        )

        self._validate_equipment_change(previous_data, extracted_data, message.content, current_evidence)

        generic_equipment = extracted_data.get(
            "equipment"
        )

        equipment_was_generic = (
            self._is_generic_equipment_value(
                generic_equipment
            )
        )

        if equipment_was_generic:
            extracted_data["equipment"] = None

        ambiguous_equipment = self._has_equipment_alternatives(
            extracted_data.get("equipment")
        )
        if ambiguous_equipment:
            extracted_data["equipment"] = None

        location_context = (
            extracted_data.get("location_context")
            or extracted_data.get("unit")
        )

        if location_context:
            extracted_data["location_context"] = (
                location_context
            )

            if not extracted_data.get("unit"):
                extracted_data["unit"] = location_context

        details_were_generic = (
            self._sanitize_incident_details(
                extracted_data
            )
        )

        valid_priorities = {
            choice[0]
            for choice in Incident.Priority.choices
        }

        if (
            extracted_data["priority"] is not None
            and extracted_data["priority"] not in valid_priorities
        ):
            extracted_data["priority"] = None

        # Los límites de modelos y sus opciones se validan en Django,
        # independientemente del esquema que declare el proveedor.
        for field_name in self.REQUIRED_FIELDS:
            value = extracted_data.get(field_name)
            model_field = Incident._meta.get_field(field_name)
            if value is not None and (
                not isinstance(value, str)
                or not value.strip()
                or (model_field.max_length and len(value) > model_field.max_length)
            ):
                extracted_data[field_name] = None
        if extracted_data.get("asset_category") not in Incident.AssetCategory.values:
            extracted_data["asset_category"] = None
        unit_value = extracted_data.get("unit")
        if not isinstance(unit_value, str) or len(unit_value) > Incident._meta.get_field("unit").max_length:
            extracted_data["unit"] = extracted_data.get("location_context")
        missing_fields = [name for name in self.REQUIRED_FIELDS if not extracted_data.get(name)]

        if ambiguous_equipment:
            clarification_question = "¿Cuál de los equipos presenta la falla? Confirma su número o identificador."
        elif equipment_was_generic:
            clarification_question = (
                f"Para identificar {str(generic_equipment).lower()}, "
                "¿puedes indicar su número, nombre o identificador? "
                f"Por ejemplo: {generic_equipment} 3."
            )

        elif details_were_generic:
            clarification_question = self.FALLBACK_QUESTIONS[
                "failure_type"
            ]

        elif missing_fields and not clarification_question:
            clarification_question = self.FALLBACK_QUESTIONS[
                missing_fields[0]
            ]

        if missing_fields and clarification_question and not requires_update_clarification:
            questioned_field = self._infer_expected_field_from_last_question(clarification_question)
            if questioned_field and questioned_field not in missing_fields:
                clarification_question = self.FALLBACK_QUESTIONS[missing_fields[0]]

        # Mostrar ejemplos solamente cuando no exista una
        # aclaración más específica para la respuesta recibida.
        if (
            help_field is not None
            and not requires_update_clarification
            and help_field in missing_fields
            and not (
                direct_draft_result is not None
                and clarification_question is not None
            )
            and not any(
                extracted_data.get(field) != previous_data.get(field)
                and extracted_data.get(field)
                for field in self.REQUIRED_FIELDS
            )
        ):
            clarification_question = self.GUIDED_QUESTIONS[
                help_field
            ]

        if draft is None:
            draft = IncidentDraft.objects.create(
                technician=message.technician,
                initial_message=message,
                extracted_data=extracted_data,
                missing_fields=missing_fields,
                last_question=clarification_question or "",
                status=(
                    IncidentDraft.Status.COLLECTING
                    if missing_fields
                    else IncidentDraft.Status.READY
                ),
            )

        else:
            draft.extracted_data = extracted_data
            draft.missing_fields = missing_fields
            draft.last_question = clarification_question or ""
            draft.status = (
                IncidentDraft.Status.COLLECTING
                if missing_fields
                else IncidentDraft.Status.READY
            )
            draft.save(
                update_fields=[
                    "extracted_data",
                    "missing_fields",
                    "last_question",
                    "status",
                    "updated_at",
                ]
            )

        message.draft = draft

        if missing_fields:
            message.status = ChannelMessage.Status.PROCESSED
            message.processed_at = timezone.now()
            message.save(
                update_fields=[
                    "draft",
                    "status",
                    "processed_at",
                ]
            )

            return {
                "action": "ask_clarification",
                "incident_created": False,
                "draft_id": draft.pk,
                "missing_fields": missing_fields,
                "reply": clarification_question,
                "extracted_data": extracted_data,
                "unchanged_fields": unchanged_fields,
            }

        # Si una corrección directa necesita una aclaración,
        # mostrarla antes de volver al resumen de confirmación.
        if (
            clarification_question is not None
            and (
                direct_draft_result is not None
                or correction_mode
            )
        ):
            draft.last_question = clarification_question
            draft.status = IncidentDraft.Status.COLLECTING
            draft.save(
                update_fields=[
                    "last_question",
                    "status",
                    "updated_at",
                ]
            )

            message.draft = draft
            message.status = ChannelMessage.Status.PROCESSED
            message.processed_at = timezone.now()
            message.save(
                update_fields=[
                    "draft",
                    "status",
                    "processed_at",
                ]
            )

            return {
                "action": "ask_clarification",
                "incident_created": False,
                "draft_id": draft.pk,
                "missing_fields": missing_fields,
                "reply": clarification_question,
                "extracted_data": extracted_data,
                "unchanged_fields": unchanged_fields,
            }

        confirmation_reply = self._build_confirmation_reply(
            extracted_data
        )

        draft.last_question = confirmation_reply
        draft.status = IncidentDraft.Status.READY
        draft.save(
            update_fields=[
                "last_question",
                "status",
                "updated_at",
            ]
        )

        message.status = ChannelMessage.Status.PROCESSED
        message.processed_at = timezone.now()
        message.save(
            update_fields=[
                "draft",
                "status",
                "processed_at",
            ]
        )

        return {
            "action": "ask_clarification",
            "incident_created": False,
            "draft_id": draft.pk,
            "missing_fields": [],
            "reply": confirmation_reply,
            "extracted_data": extracted_data,
            "unchanged_fields": unchanged_fields,
        }

    def _handle_ready_draft_confirmation(
        self,
        message: ChannelMessage,
        draft: IncidentDraft,
    ):
        normalized_message = self._normalize_quick_text(
            message.content
        )

        if normalized_message in self.CONFIRMATION_POSITIVE:
            invalid = [name for name in self.REQUIRED_FIELDS if not draft.extracted_data.get(name)]
            for name in self.REQUIRED_FIELDS:
                value = draft.extracted_data.get(name)
                field = Incident._meta.get_field(name)
                if value and (not isinstance(value, str) or (field.max_length and len(value) > field.max_length)):
                    invalid.append(name)
            if draft.extracted_data.get("priority") not in Incident.Priority.values:
                invalid.append("priority")
            if self._has_equipment_alternatives(draft.extracted_data.get("equipment")):
                invalid.append("equipment")
            if invalid:
                draft.status = IncidentDraft.Status.COLLECTING
                draft.missing_fields = list(dict.fromkeys(invalid))
                draft.last_question = self.FALLBACK_QUESTIONS[draft.missing_fields[0]]
                draft.save(update_fields=["status", "missing_fields", "last_question", "updated_at"])
                message.draft = draft
                message.status = ChannelMessage.Status.PROCESSED
                message.processed_at = timezone.now()
                message.save(update_fields=["draft", "status", "processed_at"])
                return {"action": "ask_clarification", "incident_created": False, "draft_id": draft.pk,
                        "missing_fields": draft.missing_fields, "reply": draft.last_question}
            if self.require_delivered_summary and not ChannelMessage.objects.filter(
                provider=message.provider,
                draft=draft,
                direction=ChannelMessage.Direction.OUTBOUND,
                status=ChannelMessage.Status.SENT,
                content=draft.last_question,
            ).exists():
                # READY significa datos válidos. En Telegram, la confirmación
                # también exige haber entregado ese mismo resumen completo.
                reply = self._build_confirmation_reply(draft.extracted_data)
                draft.last_question = reply
                draft.save(update_fields=["last_question", "updated_at"])
                message.draft = draft
                message.status = ChannelMessage.Status.PROCESSED
                message.processed_at = timezone.now()
                message.save(update_fields=["draft", "status", "processed_at"])
                return {"action": "ask_clarification", "incident_created": False,
                        "draft_id": draft.pk, "missing_fields": [], "reply": reply}
            return self._create_incident_from_draft(
                message=message,
                draft=draft,
            )

        requested_field = (
            self._extract_requested_correction_field(
                message.content
            )
        )

        if (
            requested_field is not None
            and self._is_correction_field_selection(
                text=message.content,
                field_name=requested_field,
            )
        ):
            reply = self.CORRECTION_QUESTIONS[
                requested_field
            ]

            draft.status = IncidentDraft.Status.COLLECTING
            draft.last_question = reply
            draft.save(
                update_fields=[
                    "status",
                    "last_question",
                    "updated_at",
                ]
            )

            message.draft = draft
            message.status = ChannelMessage.Status.PROCESSED
            message.processed_at = timezone.now()
            message.save(
                update_fields=[
                    "draft",
                    "status",
                    "processed_at",
                ]
            )

            return {
                "action": "ask_clarification",
                "incident_created": False,
                "draft_id": draft.pk,
                "missing_fields": [],
                "reply": reply,
                "extracted_data": dict(
                    draft.extracted_data or {}
                ),
            }

        if self._looks_like_natural_correction_statement(
            message.content
        ):
            draft.status = IncidentDraft.Status.COLLECTING
            draft.last_question = self.CORRECTION_PROMPT
            draft.save(
                update_fields=[
                    "status",
                    "last_question",
                    "updated_at",
                ]
            )

            # No terminamos el procesamiento aquí.
            # El mismo mensaje seguirá hacia la IA para que
            # interprete la corrección usando el contexto
            # completo del borrador.
            return None

        is_negative_confirmation = (
            normalized_message in self.CONFIRMATION_NEGATIVE
            or normalized_message.startswith("no ")
        )

        if is_negative_confirmation:
            reply = self.CORRECTION_PROMPT

            draft.status = IncidentDraft.Status.COLLECTING
            draft.last_question = reply
            draft.save(
                update_fields=[
                    "status",
                    "last_question",
                    "updated_at",
                ]
            )

            message.draft = draft
            message.status = ChannelMessage.Status.PROCESSED
            message.processed_at = timezone.now()
            message.save(
                update_fields=[
                    "draft",
                    "status",
                    "processed_at",
                ]
            )

            return {
                "action": "ask_clarification",
                "incident_created": False,
                "draft_id": draft.pk,
                "missing_fields": [],
                "reply": reply,
                "extracted_data": dict(
                    draft.extracted_data or {}
                ),
            }

        return None

    def _extract_requested_correction_field(
        self,
        text: str,
    ) -> str | None:
        normalized_text = self._normalize_quick_text(
            text
        )

        detected_fields = []

        for (
            field_name,
            aliases,
        ) in self.CORRECTION_FIELD_ALIASES.items():
            field_detected = any(
                re.search(
                    (
                        r"(?<!\w)"
                        + re.escape(alias)
                        + r"(?!\w)"
                    ),
                    normalized_text,
                )
                is not None
                for alias in aliases
            )

            if field_detected:
                detected_fields.append(
                    field_name
                )

        detected_fields = list(
            dict.fromkeys(
                detected_fields
            )
        )

        if len(detected_fields) != 1:
            return None

        return detected_fields[0]

    def _is_correction_field_selection(
        self,
        text: str,
        field_name: str,
    ) -> bool:
        normalized_text = self._normalize_quick_text(
            text
        )

        raw_text = text.strip().lower()

        # "No quiero cambiar el equipo" significa que no
        # desea hacer esa corrección.
        #
        # "No, quiero cambiar el equipo" sí significa que
        # rechazó el resumen y quiere corregir ese campo.
        if (
            normalized_text.startswith(
                "no quiero "
            )
            and re.match(
                r"^\s*no\s*[,;:]",
                raw_text,
            )
            is None
        ):
            return False

        aliases = self.CORRECTION_FIELD_ALIASES.get(
            field_name,
            (),
        )

        for alias in aliases:
            patterns = (
                (
                    r"^(?:no\s+)?"
                    r"(?:(?:quiero\s+)?"
                    r"(?:cambiar|corregir|modificar)\s+)"
                    r"(?:el\s+|la\s+)?"
                    + re.escape(alias)
                    + r"$"
                ),
                (
                    r"^(?:no\s+)?"
                    r"(?:me\s+equivoque|me\s+confundi)\s+"
                    r"(?:con|en)\s+"
                    r"(?:el\s+|la\s+)?"
                    + re.escape(alias)
                    + r"$"
                ),
                (
                    r"^(?:no\s+)?"
                    r"(?:el\s+|la\s+)?"
                    + re.escape(alias)
                    + r"$"
                ),
            )

            if any(
                re.fullmatch(
                    pattern,
                    normalized_text,
                )
                is not None
                for pattern in patterns
            ):
                return True

        return False

    def _looks_like_natural_correction_statement(
        self,
        text: str,
    ) -> bool:
        if self._is_correction_field_selection(
            text=text,
            field_name=(
                self._extract_requested_correction_field(
                    text
                )
                or ""
            ),
        ):
            return False

        normalized_text = self._normalize_quick_text(
            text
        )

        raw_text = text.strip().lower()

        if (
            normalized_text.startswith(
                "no quiero cambiar"
            )
            and re.match(
                r"^\s*no\s*[,;:]",
                raw_text,
            )
            is None
        ):
            return False

        correction_markers = (
            # Negaciones y rectificaciones.
            "no era ",
            "no es ",
            "no esta ",
            "no fue ",
            "no son ",
            "no corresponde ",
            "no se trata de ",
            "no es el ",
            "no es la ",

            # Correcciones explícitas.
            "en realidad",
            "me equivoque",
            "me confundi",
            "quise decir",
            "queria decir",
            "corrijo ",
            "correccion ",
            "rectifico ",
            "rectificacion ",
            "perdon, era ",
            "disculpa, era ",
            "disculpa, es ",
            "quise poner ",

            # Cambio de ubicación.
            "era en ",
            "es en ",
            "ocurre en ",
            "ocurria en ",
            "fue en ",
            "la ubicacion correcta es ",
            "el lugar correcto es ",
            "el estudio correcto es ",
            "el movil correcto es ",
            "cambia la ubicacion ",
            "cambiar la ubicacion ",
            "cambia el lugar ",
            "cambia el estudio ",

            # Cambio de equipo o servicio.
            "lo que falla es ",
            "el que falla es ",
            "la que falla es ",
            "el equipo correcto es ",
            "el equipo afectado es ",
            "la camara correcta es ",
            "el microfono correcto es ",
            "cambia el equipo ",
            "cambiar el equipo ",

            # Cambio de síntoma o descripción.
            "la falla real es ",
            "el problema real es ",
            "el sintoma correcto es ",
            "lo que pasa es ",
            "lo que sucede es ",
            "el problema es que ",
            "en vez de ",
            "en lugar de ",
            "mejor dicho ",

            # Cambio de prioridad o impacto.
            "la prioridad correcta es ",
            "cambia la prioridad ",
            "cambiar la prioridad ",
            "la prioridad es ",
            "el impacto real es ",
            "el impacto correcto es ",
            "cambia el impacto ",

            # Instrucciones naturales de edición.
            "revisa ",
            "revise ",
            "modifica ",
            "modificar ",
            "actualiza ",
            "actualizar ",
            "reemplaza ",
            "reemplazar ",
            "sustituye ",
            "sustituir ",
            "pon en su lugar ",
            "deberia decir ",
            "debe decir ",
            "agrega que ",
            "anade que ",
            "ademas afecta ",
        )

        if re.search(r"\b(?:perdon|disculpa)\b.*\b(?:era|es|estamos|falla)\b", normalized_text):
            return True

        if any(
            marker in normalized_text
            for marker in correction_markers
        ):
            return True

        return (
            self._extract_requested_correction_field(
                text
            )
            is not None
        )

    def _is_draft_correction_mode(
        self,
        draft: IncidentDraft | None,
    ) -> bool:
        if (
            draft is None
            or draft.status
            != IncidentDraft.Status.COLLECTING
        ):
            return False

        normalized_question = (
            self._normalize_quick_text(
                draft.last_question or ""
            )
        )

        if normalized_question == self._normalize_quick_text(
            self.CORRECTION_PROMPT
        ):
            return True

        for question in self.CORRECTION_QUESTIONS.values():
            if (
                normalized_question
                == self._normalize_quick_text(
                    question
                )
            ):
                return True

        return False

    def _infer_correction_target_field(
        self,
        draft: IncidentDraft | None,
        text: str,
        previous_data: dict,
    ) -> str | None:
        if self._contains_multiple_fields(text):
            return None
        explicit = self._extract_explicit_field_correction(text)
        if explicit:
            return explicit[0]
        requested = self._extract_requested_correction_field(text)
        if requested:
            return requested
        # Una rectificación natural sin etiqueta puede referirse a otro campo;
        # la última pregunta no debe restringir la extracción completa.
        if self._looks_like_natural_correction_statement(text):
            return None
        if draft is not None:
            normalized_question = (
                self._normalize_quick_text(
                    draft.last_question or ""
                )
            )

            for (
                field_name,
                question,
            ) in self.CORRECTION_QUESTIONS.items():
                if (
                    normalized_question
                    == self._normalize_quick_text(
                        question
                    )
                ):
                    return field_name

        requested_field = (
            self._extract_requested_correction_field(
                text
            )
        )

        if requested_field is not None:
            return requested_field

        explicit_priority = (
            self._extract_explicit_priority(
                text
            )
        )

        if (
            explicit_priority is not None
            and explicit_priority
            != previous_data.get("priority")
        ):
            return "priority"

        normalized_text = (
            self._normalize_quick_text(
                text
            )
        )

        fields_to_compare = (
            "equipment",
            "location_context",
            "failure_type",
            "operational_impact",
        )

        for field_name in fields_to_compare:
            previous_value = previous_data.get(
                field_name
            )

            if not previous_value:
                continue

            normalized_previous = (
                self._normalize_quick_text(
                    str(previous_value)
                )
            )

            if (
                normalized_previous
                and len(
                    normalized_previous.split()
                ) >= 2
                and normalized_previous
                in normalized_text
            ):
                return field_name

        return None

    def _validate_equipment_change(self, previous, current, message, evidence=None):
        """No trasladar datos de otra familia de equipo sin evidencia nueva.

        Los cambios de identificador dentro de una familia conservan datos.
        Ante otra familia (o una desconocida), pedir de nuevo los detalles
        que el mensaje no haya indicado expresamente. Nunca inventarlos.
        """
        old, new = previous.get("equipment"), current.get("equipment")
        if not old or not new or self._normalize_quick_text(old) == self._normalize_quick_text(new):
            return

        def family(value):
            value = self._normalize_quick_text(value)
            for pattern, category in (
                (r"\b(?:camara|monitor|video)\b", "video_cameras"),
                (r"\b(?:consola|microfono|audio|mezclador)\b", "audio"),
                (r"\b(?:router|switch|red)\b", "networks"),
                (r"\b(?:servidor|sistema|software)\b", "systems"),
            ):
                if re.search(pattern, value):
                    return category
            return None

        old_family, new_family = family(old), family(new)
        if old_family and old_family == new_family:
            return
        current["asset_category"] = new_family
        normalized_message = self._normalize_quick_text(message)
        for field in ("failure_type", "description", "operational_impact"):
            candidate = current.get(field)
            # La extracción puede repetir el dato previo o reformularlo sin
            # evidencia; solo aceptarlo si está expresado en este mensaje.
            if not candidate or (not (evidence or {}).get(field) and self._normalize_quick_text(candidate) not in normalized_message):
                current[field] = None

    def _merge_ai_correction(
        self,
        *,
        previous_data: dict,
        extraction,
        message_text: str,
        target_field: str | None,
    ) -> tuple[dict, bool]:
        extracted_data = dict(
            previous_data
        )

        candidate_values = {
            "area": extraction.area,
            "location_context": extraction.location_context,
            "equipment": extraction.equipment,
            "failure_type": extraction.failure_type,
            "operational_impact": extraction.operational_impact,
            "priority": extraction.priority,
        }
        declared_priority = self._extract_explicit_priority(message_text)
        if declared_priority:
            candidate_values["priority"] = declared_priority

        structural_fields = (
            "area",
            "location_context",
            "equipment",
            "failure_type",
            "operational_impact",
            "priority",
        )

        if (
            target_field is not None
            and target_field != "description"
        ):
            fields_to_process = (
                target_field,
            )
        elif target_field == "description":
            fields_to_process = ()
        else:
            fields_to_process = structural_fields

        changed_fields = []
        evidence = getattr(extraction, "field_evidence", None)

        for field_name in fields_to_process:
            if (evidence is not None and not evidence.get(field_name)
                    and not (field_name == "priority" and declared_priority)):
                continue
            candidate_value = candidate_values.get(
                field_name
            )

            if not candidate_value:
                continue

            previous_value = previous_data.get(
                field_name
            )

            if (
                previous_value
                and self._normalize_quick_text(
                    str(previous_value)
                )
                == self._normalize_quick_text(
                    str(candidate_value)
                )
            ):
                continue

            if field_name == "priority":
                explicit_priority = (
                    self._extract_explicit_priority(
                        message_text
                    )
                )

                if explicit_priority is None:
                    continue

                candidate_value = explicit_priority

            elif field_name == "area":
                candidate_value = (
                    self._normalize_area_value(
                        candidate_value
                    )
                )

            elif field_name == "location_context":
                candidate_value = (
                    self._clean_location_value(
                        str(candidate_value)
                    )
                )

            elif field_name == "equipment":
                candidate_value = (
                    self._clean_equipment_value(
                        str(candidate_value)
                    )
                )

            elif field_name == "operational_impact":
                candidate_value = (
                    self._clean_operational_impact_value(
                        str(candidate_value)
                    )
                )

            else:
                candidate_value = (
                    self._clean_direct_value(
                        str(candidate_value)
                    )
                )

            if not candidate_value:
                continue

            self._apply_direct_field_value(
                extracted_data=extracted_data,
                field_name=field_name,
                value=candidate_value,
            )

            if extracted_data.get(field_name) != previous_data.get(field_name):
                changed_fields.append(field_name)

        should_process_description = (
            target_field == "description"
            or (
                "equipment" in changed_fields
                and extraction.description
                and self._normalize_quick_text(extraction.description)
                in self._normalize_quick_text(message_text)
            )
            or (
                target_field is None
                and not changed_fields
            )
            or "failure_type" in changed_fields
            or "operational_impact" in changed_fields
        )
        if evidence is not None and not evidence.get("description"):
            should_process_description = False

        if should_process_description:
            candidate_description = (
                extraction.description
            )

            if candidate_description:
                candidate_description = (
                    self._clean_direct_value(
                        str(candidate_description)
                    )
                )

                previous_description = (
                    previous_data.get(
                        "description"
                    )
                )

                description_changed = (
                    candidate_description
                    and (
                        not previous_description
                        or self._normalize_quick_text(
                            str(previous_description)
                        )
                        != self._normalize_quick_text(
                            candidate_description
                        )
                    )
                )

                if (
                    description_changed
                    and len(
                        candidate_description.split()
                    ) >= 3
                ):
                    extracted_data["description"] = (
                        candidate_description
                    )

                    changed_fields.append(
                        "description"
                    )

        if "equipment" in changed_fields:
            valid_asset_categories = {
                choice[0]
                for choice in Incident.AssetCategory.choices
            }

            if (
                extraction.asset_category
                in valid_asset_categories
            ):
                extracted_data["asset_category"] = (
                    extraction.asset_category
                )

        if extracted_data.get("location_context"):
            extracted_data["unit"] = (
                extracted_data[
                    "location_context"
                ]
            )

        return (
            extracted_data,
            bool(changed_fields),
        )

    def _build_confirmation_reply(
        self,
        extracted_data: dict,
    ) -> str:
        asset_category_labels = dict(
            Incident.AssetCategory.choices
        )

        priority_labels = dict(
            Incident.Priority.choices
        )

        area = (
            extracted_data.get("area")
            or "No informada"
        )

        location_context = (
            extracted_data.get("location_context")
            or extracted_data.get("unit")
            or "No informada"
        )

        asset_category = asset_category_labels.get(
            extracted_data.get("asset_category"),
            "No informada",
        )

        equipment = (
            extracted_data.get("equipment")
            or "No informado"
        )

        failure_type = (
            extracted_data.get("failure_type")
            or "No informado"
        )

        description = (
            extracted_data.get("description")
            or "No informada"
        )

        operational_impact = (
            extracted_data.get("operational_impact")
            or "No informado"
        )

        priority = priority_labels.get(
            extracted_data.get("priority"),
            "No informada",
        )

        return (
            "Antes de crear el incidente, revisa los datos:\n\n"
            f"• Área: {area}\n"
            f"• Ubicación/contexto: {location_context}\n"
            f"• Familia: {asset_category}\n"
            f"• Equipo/servicio: {equipment}\n"
            f"• Síntoma: {failure_type}\n"
            f"• Descripción: {description}\n"
            f"• Impacto operacional: {operational_impact}\n"
            f"• Prioridad: {priority}\n\n"
            "¿Confirmas la creación del incidente? "
            "Responde Sí para crearlo o No para corregir "
            "algún dato."
        )

    def _create_incident_from_draft(
        self,
        message: ChannelMessage,
        draft: IncidentDraft,
    ):
        extracted_data = {
            key: value
            for key, value in dict(
                draft.extracted_data or {}
            ).items()
            if key != self.PENDING_MESSAGES_KEY
        }

        initial_message_content = ""

        if draft.initial_message is not None:
            initial_message_content = (
                draft.initial_message.content
            )

        incident = Incident.objects.create(
            technician=message.technician,
            unit=(
                extracted_data.get("unit")
                or extracted_data["location_context"]
            ),
            area=extracted_data.get("area") or "",
            location_context=(
                extracted_data.get("location_context")
                or extracted_data.get("unit")
                or ""
            ),
            asset_category=(
                extracted_data.get("asset_category")
                or ""
            ),
            equipment=extracted_data["equipment"],
            failure_type=extracted_data["failure_type"],
            description=extracted_data["description"],
            operational_impact=(
                extracted_data.get("operational_impact")
                or ""
            ),
            priority=extracted_data["priority"],
            status=Incident.Status.REPORTED,
            original_message=initial_message_content,
        )

        draft.incident = incident
        draft.status = IncidentDraft.Status.COMPLETED
        draft.missing_fields = []
        draft.last_question = ""
        draft.completed_at = timezone.now()
        draft.save(
            update_fields=[
                "incident",
                "status",
                "missing_fields",
                "last_question",
                "completed_at",
                "updated_at",
            ]
        )

        ChannelMessage.objects.filter(
            draft=draft,
        ).update(
            incident=incident,
        )

        message.draft = draft
        message.incident = incident
        message.status = ChannelMessage.Status.PROCESSED
        message.processed_at = timezone.now()
        message.save(
            update_fields=[
                "draft",
                "incident",
                "status",
                "processed_at",
            ]
        )

        # Persistir la intención en el mismo commit que la incidencia. El
        # receptor envía el correo desde su worker local, fuera de esta ruta.
        IncidentNotification.objects.get_or_create(incident=incident)

        return {
            "action": "incident_created",
            "incident_created": True,
            "incident_id": incident.pk,
            "incident_code": incident.code,
            "reply": (
                f"Reporte {incident.code} creado correctamente."
            ),
            "extracted_data": extracted_data,
        }

    def _sanitize_incident_details(
        self,
        extracted_data: dict,
    ) -> bool:
        details_were_generic = False

        failure_type = extracted_data.get(
            "failure_type"
        )

        if failure_type:
            normalized_failure_type = (
                self._normalize_quick_text(
                    str(failure_type)
                )
            )

            if (
                normalized_failure_type
                in self.GENERIC_FAILURE_TYPES
            ):
                extracted_data["failure_type"] = None
                details_were_generic = True

        description = extracted_data.get(
            "description"
        )

        if description:
            normalized_description = (
                self._normalize_quick_text(
                    str(description)
                )
            )

            description_is_generic = any(
                marker in normalized_description
                for marker
                in self.GENERIC_DESCRIPTION_MARKERS
            )

            if (
                description_is_generic
                and len(
                    normalized_description.split()
                ) <= 14
            ):
                extracted_data["description"] = None
                details_were_generic = True

        return details_were_generic

    def _handle_ai_timeout(
        self,
        message: ChannelMessage,
        draft: IncidentDraft | None,
        previous_data: dict,
        pending_messages: list[str],
    ):
        if draft is None:
            message.status = ChannelMessage.Status.PROCESSED
            message.processed_at = timezone.now()
            message.save(
                update_fields=[
                    "status",
                    "processed_at",
                ]
            )

            return {
                "action": "ai_timeout",
                "incident_created": False,
                "draft_id": None,
                "missing_fields": [],
                "reply": self.AI_TIMEOUT_INITIAL_REPLY,
                "extracted_data": previous_data,
            }

        saved_pending_messages = list(
            pending_messages
        )

        if self._should_store_pending_message(
            message.content
        ):
            saved_pending_messages.append(
                message.content.strip()
            )

        timeout_data = dict(previous_data)
        timeout_data[self.PENDING_MESSAGES_KEY] = (
            saved_pending_messages
        )

        missing_fields = [
            field_name
            for field_name in self.REQUIRED_FIELDS
            if not previous_data.get(field_name)
        ]

        draft.extracted_data = timeout_data
        draft.missing_fields = missing_fields
        draft.status = IncidentDraft.Status.COLLECTING
        draft.save(
            update_fields=[
                "extracted_data",
                "missing_fields",
                "status",
                "updated_at",
            ]
        )

        message.draft = draft
        message.status = ChannelMessage.Status.PROCESSED
        message.processed_at = timezone.now()
        message.save(
            update_fields=[
                "draft",
                "status",
                "processed_at",
            ]
        )

        return {
            "action": "ai_timeout",
            "incident_created": False,
            "draft_id": draft.pk,
            "missing_fields": missing_fields,
            "reply": self.AI_TIMEOUT_REPLY,
            "extracted_data": previous_data,
        }

    def _get_pending_messages(
        self,
        extracted_data: dict,
    ) -> list[str]:
        pending_messages = extracted_data.get(
            self.PENDING_MESSAGES_KEY,
            [],
        )

        if not isinstance(
            pending_messages,
            list,
        ):
            return []

        return [
            item.strip()
            for item in pending_messages
            if isinstance(item, str) and item.strip()
        ]

    def _build_ai_message(
        self,
        current_message: str,
        pending_messages: list[str],
    ) -> str:
        if not pending_messages:
            return current_message

        normalized_current = (
            self._normalize_quick_text(
                current_message
            )
        )

        if normalized_current in {
            "continuar",
            "continua",
            "continuemos",
            "seguir",
            "sigamos",
        }:
            return "\n".join(
                pending_messages
            )

        pending_text = "\n".join(
            f"- {pending_message}"
            for pending_message in pending_messages
        )

        return (
            "Mensajes anteriores del reportante que quedaron "
            "pendientes de procesamiento:\n"
            f"{pending_text}\n\n"
            "Mensaje actual del reportante:\n"
            f"{current_message}"
        )

    def _should_store_pending_message(
        self,
        text: str,
    ) -> bool:
        normalized_text = (
            self._normalize_quick_text(text)
        )

        return normalized_text not in {
            "continuar",
            "continua",
            "continuemos",
            "seguir",
            "sigamos",
        }

    def _get_pending_help_field(
        self,
        draft: IncidentDraft | None,
    ) -> str | None:
        if (
            draft is None
            or draft.status != IncidentDraft.Status.COLLECTING
        ):
            return None

        missing_fields = draft.missing_fields or []

        field = self._infer_expected_field_from_last_question(
            draft.last_question
        )

        if field in missing_fields:
            return field

        if len(missing_fields) == 1:
            return missing_fields[0]

        return None

    def _is_clarification_request(self, text: str) -> bool:
        normalized = self._normalize_quick_text(text)

        exact_phrases = {
            "no se",
            "ni idea",
            "no tengo idea",
            "no lo se",
            "no entiendo",
            "no entendi",
            "no comprendo",
            "no cacho",
            "no cache",
            "no me queda claro",
            "un ejemplo",
            "ayuda",
            "que",
            "como",
            "como asi",
        }

        explanation_phrases = (
            "no entiendo ",
            "no comprendo ",
            "no cacho ",
            "no se a que te refieres",
            "no se que responder",
            "no se que poner",
            "no se como responder",
            "que significa",
            "a que te refieres",
            "que quieres decir",
            "que tengo que responder",
            "que tengo que poner",
            "dame un ejemplo",
            "me das un ejemplo",
            "puedes darme un ejemplo",
            "me puedes dar un ejemplo",
            "puedes explicarme",
            "me puedes explicar",
            "explicame",
        )

        return (
            normalized in exact_phrases
            or any(
                normalized.startswith(phrase)
                for phrase in explanation_phrases
            )
        )

    def _try_process_direct_draft_reply(
        self,
        draft: IncidentDraft | None,
        text: str,
        previous_data: dict,
        pending_messages: list[str],
    ) -> tuple[dict, str | None] | None:
        if draft is None:
            return None

        if self._is_uncertain_update(text):
            return None

        # Una respuesta con varios datos debe extraerse completa. Los atajos
        # de un campo no deben consumir también ubicación, impacto o prioridad.
        if self._contains_multiple_fields(text):
            return None

        normalized_text = (
            self._normalize_quick_text(text)
        )

        continuation_words = {
            "continuar",
            "continua",
            "continuemos",
            "seguir",
            "sigamos",
        }

        candidate_text = text

        if normalized_text in continuation_words:
            if len(pending_messages) != 1:
                return None

            candidate_text = pending_messages[0]

        is_correction_mode = (
            self._is_draft_correction_mode(
                draft
            )
        )

        if is_correction_mode:
            requested_field = (
                self._extract_requested_correction_field(
                    candidate_text
                )
            )

            if (
                requested_field is not None
                and self._is_correction_field_selection(
                    text=candidate_text,
                    field_name=requested_field,
                )
            ):
                return (
                    dict(previous_data),
                    self.CORRECTION_QUESTIONS[
                        requested_field
                    ],
                )

        correction = (
            self._extract_explicit_field_correction(
                candidate_text
            )
        )

        if (correction is None
                and re.search(r"\bprioridad\b", normalized_text)
                and self._extract_explicit_priority(candidate_text)):
            correction = ("priority", candidate_text)

        if (
            correction is None
            and is_correction_mode
        ):
                natural_failure = (
                    self._extract_natural_failure_correction(
                        candidate_text
                    )
                )

                if natural_failure is not None:
                    correction = (
                        "failure_type",
                        natural_failure,
                    )

        if correction is None:
            natural_location = (
                self._extract_natural_location_correction(
                    candidate_text
                )
            )

            if natural_location is not None:
                correction = (
                    "location_context",
                    natural_location,
                )

        if correction is not None:
            field_name, value = correction

            extracted_data = dict(
                previous_data
            )

            if field_name == "priority":
                priority = (
                    self._extract_explicit_priority(
                        value
                    )
                )

                if priority is None:
                    return (
                        extracted_data,
                        self.PRIORITY_GUIDANCE,
                    )

                value = priority

            else:
                value = self._clean_direct_value(
                    value
                )

                is_valid_for_field = (
                    self._is_valid_direct_field_value(
                        field_name=field_name,
                        value=value,
                        previous_data=extracted_data,
                    )
                )

                if (
                    field_name == "area"
                    and not is_valid_for_field
                    and self._is_valid_direct_field_value(
                        field_name="location_context",
                        value=value,
                        previous_data=extracted_data,
                    )
                ):
                    suggested_location = re.sub(
                        r"^(?:el\s+|la\s+)",
                        "",
                        value,
                        count=1,
                        flags=re.IGNORECASE,
                    ).strip()

                    if suggested_location:
                        suggested_location = (
                            suggested_location[0].upper()
                            + suggested_location[1:]
                        )

                    return (
                        extracted_data,
                        (
                            f"Entiendo. {suggested_location} corresponde "
                            "a una ubicación, no a un área. "
                            "Si quieres corregir la ubicación, puedes "
                            f"responder: “La ubicación correcta es "
                            f"{suggested_location}”."
                        ),
                    )

                if not is_valid_for_field:
                    return None

            self._apply_direct_field_value(
                extracted_data=extracted_data,
                field_name=field_name,
                value=value,
            )

            return (
                extracted_data,
                None,
            )

        if normalized_text in continuation_words:
            return None

        # El resumen contiene todos los nombres de campo; no es una pregunta
        # pendiente de prioridad. Un mensaje nuevo requiere interpretación.
        if draft.status == IncidentDraft.Status.READY and not is_correction_mode:
            return None

        expected_field = (
            self._infer_expected_field_from_last_question(
                draft.last_question
            )
        )

        if expected_field is None:
            return None

        # Los atajos de respuesta simple ya se evaluaron. Una rectificación
        # no debe responder accidentalmente a la última pregunta de prioridad.
        if (
            self._looks_like_natural_correction_statement(candidate_text)
            and not (
                expected_field == "equipment"
                and self._is_ambiguous_equipment_reply(candidate_text)
            )
        ):
            return None

        if (
            self._looks_like_compound_reply(
                text
            )
            and not (
                is_correction_mode
                and expected_field == "equipment"
            )
        ):
            return None

        extracted_data = dict(
            previous_data
        )

        if expected_field == "priority":
            priority = (
                self._extract_explicit_priority(
                    text
                )
            )

            if priority is None:
                if self._has_operational_information(text, previous_data):
                    return None
                return (
                    extracted_data,
                    self.PRIORITY_GUIDANCE,
                )

            self._apply_direct_field_value(
                extracted_data=extracted_data,
                field_name="priority",
                value=priority,
            )

            return (
                extracted_data,
                None,
            )

        value = self._clean_direct_value(
            text
        )

        if not value:
            return None

        if expected_field == "operational_impact":
            value = self._clean_operational_impact_value(
                value
            )

        if (
            expected_field == "equipment"
            and self._is_ambiguous_equipment_reply(
                value
            )
        ):
            return (
                extracted_data,
                (
                    "Entiendo el problema, pero necesito identificar "
                    "con precisión el equipo afectado. "
                    "¿Qué equipo o servicio específico presenta la falla? "
                    "Por ejemplo: Micrófono 2, Intercom 4, "
                    "Consola principal o Router principal."
                ),
            )

        if (
            expected_field == "operational_impact"
            and self._is_valid_direct_field_value(
                field_name="failure_type",
                value=value,
                previous_data=extracted_data,
            )
        ):
            self._append_failure_detail_to_description(
                extracted_data=extracted_data,
                value=value,
            )

            return (
                extracted_data,
                (
                    "Entendido. Agregué ese síntoma al reporte. "
                    "Ahora necesito el impacto operacional: "
                    "¿qué efecto tiene esta falla sobre la operación? "
                    "Por ejemplo, ¿interrumpe la grabación, afecta "
                    "la señal, obliga a usar un respaldo o permite "
                    "continuar operando?"
                ),
            )

        if not self._is_valid_direct_field_value(
            field_name=expected_field,
            value=value,
            previous_data=extracted_data,
        ):
            return None

        self._apply_direct_field_value(
            extracted_data=extracted_data,
            field_name=expected_field,
            value=value,
        )

        return (
            extracted_data,
            None,
        )

    def _is_valid_direct_field_value(
        self,
        field_name: str,
        value: str,
        previous_data: dict,
    ) -> bool:
        normalized_value = (
            self._normalize_quick_text(
                value
            )
        )

        if not normalized_value:
            return False

        obviously_unrelated = {
            "tengo hambre",
            "tengo sueño",
            "no se",
            "no sé",
            "ni idea",
            "hola",
            "gracias",
            "ok",
            "si",
            "no",
            "da lo mismo",
            "no importa",
        }

        if normalized_value in obviously_unrelated:
            return False

        if field_name == "area":
            valid_areas = {
                "audio",
                "produccion",
                "operaciones",
                "redes",
                "sistemas",
                "transmision",
                "señal",
                "video",
                "camaras",
                "iluminacion",
                "infraestructura",
                "digital",
                "streaming",
                "estudios",
                "control tecnico",
            }

            return normalized_value in valid_areas

        if field_name == "location_context":
            location_markers = (
                "estudio",
                "sala",
                "control",
                "movil",
                "unidad movil",
                "plataforma",
                "piso",
                "edificio",
                "set",
                "terreno",
            )

            return any(
                marker in normalized_value
                for marker in location_markers
            )

        if field_name == "equipment":
            equipment_markers = (
                "camara",
                "microfono",
                "consola",
                "router",
                "switch",
                "servidor",
                "monitor",
                "encoder",
                "decodificador",
                "transmisor",
                "receptor",
                "antena",
                "enlace",
                "computador",
                "pc",
                "mezclador",
                "intercom",
                "luminaria",
                "panel",
            )

            return any(
                marker in normalized_value
                for marker in equipment_markers
            )

        if field_name == "failure_type":
            if (
                normalized_value
                in self.GENERIC_FAILURE_TYPES
            ):
                return False

            failure_markers = (
                "sin imagen",
                "sin audio",
                "sin señal",
                "sin conexion",
                "ruido",
                "corte",
                "cortes",
                "intermit",
                "no enciende",
                "no responde",
                "no se conecta",
                "no conecta",
                "no logra conectar",
                "no se puede conectar",
                "no se pueden conectar",
                "no se escucha",
                "no se puede escuchar",
                "no se oye",
                "sin retorno",
                "no hay retorno",
                "congel",
                "distors",
                "interferencia",
                "desconexion",
                "pierde",
                "perdida",
                "latencia",
                "desfase",
            )

            return any(
                marker in normalized_value
                for marker in failure_markers
            )

        if field_name == "operational_impact":
            impact_markers = (
                "detiene",
                "detener",
                "interrumpe",
                "interrumpir",
                "impide",
                "impedir",
                "retrasa",
                "retraso",
                "demora",
                "degrada",
                "afecta",
                "afectacion",
                "sin impacto",
                "no afecta",
                "respaldo",
                "alternativa",
                "obliga",
                "obligar",
                "no permite",
                "no deja",
                "deja sin",
                "puede continuar",
                "podemos continuar",
                "permite continuar",
                "continuar operando",
                "continua operando",
                "no puede continuar",
                "no podemos continuar",
                "no puede operar",
                "no podemos operar",
                "no puede grabar",
                "no podemos grabar",
                "no puede transmitir",
                "no podemos transmitir",
                "no puede emitir",
                "no podemos emitir",
                "no puede iniciar",
                "no podemos iniciar",
                "paraliza",
                "suspende",
                "bloquea",
            )

            return any(
                marker in normalized_value
                for marker in impact_markers
            )

            return any(
                marker in normalized_value
                for marker in impact_markers
            )

        if field_name == "description":
            words = normalized_value.split()

            if len(words) < 4:
                return False

            contextual_values = (
                previous_data.get("equipment"),
                previous_data.get("failure_type"),
                previous_data.get(
                    "location_context"
                ),
            )

            for contextual_value in contextual_values:
                if not contextual_value:
                    continue

                normalized_context = (
                    self._normalize_quick_text(
                        str(contextual_value)
                    )
                )

                if (
                    normalized_context
                    and normalized_context
                    in normalized_value
                ):
                    return True

            description_markers = (
                "ruido",
                "corte",
                "cortes",
                "señal",
                "audio",
                "imagen",
                "enciende",
                "intermit",
                "conexion",
                "grabacion",
                "produccion",
                "transmision",
            )

            return any(
                marker in normalized_value
                for marker in description_markers
            )

        return False

    def _is_generic_equipment_value(
        self,
        value: str | None,
    ) -> bool:
        if not value:
            return False

        normalized_value = self._normalize_quick_text(
            str(value)
        )

        generic_equipment_values = {
            "camara",
            "microfono",
            "monitor",
            "intercom",
            "switch",
            "servidor",
            "computador",
            "pc",
            "encoder",
            "decodificador",
            "transmisor",
            "receptor",
            "antena",
            "consola",
            "mezclador",
            "luminaria",
            "panel",
        }

        return normalized_value in generic_equipment_values

    def _has_equipment_alternatives(self, value):
        if not isinstance(value, str):
            return False
        text = self._normalize_quick_text(value)
        return bool(re.search(r"\b(?:o|u)\b", text))

    def _is_ambiguous_equipment_reply(
        self,
        value: str,
    ) -> bool:
        normalized_value = self._normalize_quick_text(
            value
        )

        generic_equipment_markers = (
            "equipo de audio",
            "equipos de audio",
            "equipo de video",
            "equipos de video",
            "equipo de red",
            "equipos de red",
            "equipo de produccion",
            "equipos de produccion",
            "equipo tecnico",
            "equipos tecnicos",
        )

        failure_markers = (
            "no se conecta",
            "no conecta",
            "no se puede conectar",
            "no se pueden conectar",
            "sin conexion",
            "no funciona",
            "no responde",
            "no enciende",
            "no se escucha",
            "sin audio",
            "sin imagen",
            "sin señal",
            "ruido",
            "corte",
            "pierde",
        )

        mentions_generic_equipment = any(
            marker in normalized_value
            for marker in generic_equipment_markers
        )

        mentions_failure = any(
            marker in normalized_value
            for marker in failure_markers
        )

        return (
            mentions_generic_equipment
            and mentions_failure
        )

    def _refine_asset_category_from_equipment(
        self,
        *,
        equipment: str | None,
        current_category: str | None,
    ) -> str | None:
        if not equipment:
            return current_category

        normalized_equipment = (
            self._normalize_quick_text(
                str(equipment)
            )
        )

        audio_markers = (
            "consola de audio",
            "microfono",
            "mezclador de audio",
            "mesa de audio",
        )

        if any(
            marker in normalized_equipment
            for marker in audio_markers
        ):
            return Incident.AssetCategory.AUDIO

        return current_category

    def _choose_refined_value(
        self,
        *,
        previous_value: str | None,
        candidate_value: str | None,
        field_name: str,
    ) -> str | None:
        """
        Conserva información válida y acepta valores más precisos.
        """

        if not previous_value:
            return candidate_value

        if not candidate_value:
            return previous_value

        previous_normalized = self._normalize_quick_text(
            str(previous_value)
        )

        candidate_normalized = self._normalize_quick_text(
            str(candidate_value)
        )

        if previous_normalized == candidate_normalized:
            return previous_value

        if field_name == "equipment":
            if (
                candidate_normalized.startswith(
                    f"{previous_normalized} "
                )
                and len(candidate_normalized.split())
                <= len(previous_normalized.split()) + 3
            ):
                return candidate_value

        if field_name == "location_context":
            contains_equipment = (
                self._is_valid_direct_field_value(
                    field_name="equipment",
                    value=str(previous_value),
                    previous_data={},
                )
            )

            has_extraneous_clause = (
                contains_equipment
                and (
                    "," in str(previous_value)
                    or " esta en " in f" {previous_normalized} "
                    or " queda en " in f" {previous_normalized} "
                )
            )

            if (
                has_extraneous_clause
                and candidate_normalized in previous_normalized
                and self._is_valid_direct_field_value(
                    field_name="location_context",
                    value=str(candidate_value),
                    previous_data={},
                )
            ):
                return candidate_value

        return previous_value

    def _looks_like_compound_reply(
        self,
        text: str,
    ) -> bool:
        normalized_text = (
            self._normalize_quick_text(
                text
            )
        )

        # «Impacto ... Prioridad media» o una corrección con dos categorías
        # contiene más información que una respuesta corta de prioridad.
        priority_clause = re.search(r"\bprioridad\s+(?:(?:es|correcta)\s+)*(?:baja|media|alta|critica)\b", normalized_text)
        if priority_clause and len(normalized_text.split()) > 9:
            return True
        if re.search(r"\b(?:camara|microfono|router|consola)\b", normalized_text) and re.search(r"\b(?:estudio|movil|sala)\b", normalized_text):
            return True

        if (
            self._is_valid_direct_field_value(
                field_name="equipment",
                value=text,
                previous_data={},
            )
            and self._is_valid_direct_field_value(
                field_name="location_context",
                value=text,
                previous_data={},
            )
        ):
            return True

        compound_markers = (
            "area ",
            "unidad movil",
            "estudio ",
            "prioridad ",
            "impacto ",
            "obliga a usar",
            "detiene la produccion",
            "afecta la señal",
        )

        detected_markers = sum(
            1
            for marker in compound_markers
            if marker in normalized_text
        )

        return detected_markers >= 2

    def _contains_multiple_fields(self, text):
        """Detectar datos independientes sin confundir nombres de equipos."""
        normalized = self._normalize_quick_text(text)

        has_priority = bool(
            re.search(r"\bprioridad\b", normalized)
        )

        has_asset = bool(
            re.search(
                r"\b(?:camara|microfono|router|consola|switcher|encoder)"
                r"\s+(?:\w+)|\b(?:equipo|servicio)\b",
                normalized,
            )
        )

        has_location = bool(
            re.search(
                r"\b(?:estudio|movil|sala)\s+\d+\b"
                r"|\bsala tecnica\b"
                r"|\b(?:estadio|recinto)\s+\w+\b"
                r"|\b(?:era|es|estamos|ocurre|fue)\s+en\s+\w+"
                r"|\bubicacion\b",
                normalized,
            )
        )

        has_impact = bool(
            re.search(
                r"\b(?:impide|afecta|obliga|detiene|usamos|podemos|impacto)\b"
                r"|\b(?:no (?:tenemos|hay)|sin) respaldo\b"
                r"|\b(?:transmision|salida al aire) (?:sin|interrumpida)\b",
                normalized,
            )
        )

        has_area = bool(
            re.search(r"\barea\b", normalized)
        )

        has_failure = bool(
            re.search(
                r"\b(?:intermitente|corta|cortes|enciende)\b"
                r"|\bsin (?:audio|imagen|conexion)\b"
                r"|\b(?:perdimos|perdio|perdida|dejo de entregar|ya no entrega)\b",
                normalized,
            )
        )

        if has_area and not (
            has_priority or has_asset or has_impact
        ):
            correction = self._extract_explicit_field_correction(text)

            if correction and correction[0] == "area":
                return False

        return sum(
            [
                has_priority,
                has_asset,
                has_location,
                has_impact,
                has_area,
                has_failure,
            ]
        ) >= 2

    def _is_uncertain_update(self, text):
        return bool(re.search(r"\b(?:quizas?|tal vez|no se si|no estoy segur[oa]|podria ser)\b",
                              self._normalize_quick_text(text)))

    def _has_operational_information(self, text, previous_data):
        evolution = re.search(r"\b(?:dejo de|ya no|perdimos|empeoro|ahora falla)\b", self._normalize_quick_text(text))
        return bool(evolution) or any(
            self._is_valid_direct_field_value(field_name=name, value=text, previous_data=previous_data)
            for name in ("failure_type", "operational_impact"))

    def _infer_expected_field_from_last_question(
        self,
        question: str,
    ) -> str | None:
        if not question:
            return None

        normalized_question = (
            self._normalize_quick_text(
                question
            )
        )

        if "prioridad" in normalized_question:
            return "priority"

        impact_context_markers = (
            "operacion",
            "produccion",
            "senal",
            "emision",
            "servicio",
        )

        asks_about_impact = (
            "impacto operacional" in normalized_question
            or "que impacto" in normalized_question
            or "que efecto" in normalized_question
            or "como afecta" in normalized_question
        )

        mentions_operational_context = any(
            marker in normalized_question
            for marker in impact_context_markers
        )

        if (
            asks_about_impact
            and mentions_operational_context
        ):
            return "operational_impact"

        if "area" in normalized_question:
            return "area"

        if (
            "donde esta ocurriendo" in normalized_question
            or "ubicacion" in normalized_question
            or "contexto operacional"
            in normalized_question
        ):
            return "location_context"

        if (
            "sintoma" in normalized_question
            or "comportamiento anormal"
            in normalized_question
            or "que problema presenta"
            in normalized_question
        ):
            return "failure_type"

        if (
            "descripcion" in normalized_question
            or "puedes describir"
            in normalized_question
            or "describe con un poco mas de detalle"
            in normalized_question
        ):
            return "description"

        if "que equipo" in normalized_question:
            return "equipment"

        return None

    def _extract_natural_failure_correction(
        self,
        text: str,
    ) -> str | None:
        cleaned_text = " ".join(
            text.strip().split()
        )

        normalized_text = (
            self._normalize_quick_text(
                cleaned_text
            )
        )

        correction_markers = (
            "era que",
            "en realidad",
            "me equivoque",
            "quise decir",
            "queria decir",
        )

        if not any(
            marker in normalized_text
            for marker in correction_markers
        ):
            return None

        value = cleaned_text

        prefix_patterns = (
            r"^era\s+que\s+",
            (
                r"^en\s+realidad\s+"
                r"(?:era\s+que\s+|es\s+que\s+|es\s+)?"
            ),
            (
                r"^me\s+equivoqu[eé]"
                r"\s*[,;:\-]?\s*"
            ),
            (
                r"^quise\s+decir"
                r"(?:\s+que)?\s+"
            ),
            (
                r"^quer[ií]a\s+decir"
                r"(?:\s+que)?\s+"
            ),
        )

        for pattern in prefix_patterns:
            updated_value = re.sub(
                pattern,
                "",
                value,
                count=1,
                flags=re.IGNORECASE,
            )

            if updated_value != value:
                value = updated_value
                break

        value = re.sub(
            (
                r"[\s,;:\-]*"
                r"(?:me\s+equivoqu[eé]|perd[oó]n)"
                r"\s*$"
            ),
            "",
            value,
            count=1,
            flags=re.IGNORECASE,
        )

        value = self._clean_direct_value(
            value
        )

        if not value:
            return None

        if not self._is_valid_direct_field_value(
            field_name="failure_type",
            value=value,
            previous_data={},
        ):
            return None

        return (
            value[0].upper()
            + value[1:]
        )

    def _extract_natural_location_correction(
        self,
        text: str,
    ) -> str | None:
        cleaned_text = " ".join(
            text.strip().split()
        )

        patterns = (
            (
                r"^(?:(?:me\s+equivoqu[eé]|perd[oó]n)"
                r"\s*[,;:\-]?\s*)?"
                r"(?:en\s+realidad\s+)?"
                r"(?:es|era|ser[ií]a)\s+en\s+"
                r"(?:el\s+|la\s+)?(.+)$"
            ),
            (
                r"^(?:(?:me\s+equivoqu[eé]|perd[oó]n)"
                r"\s*[,;:\-]?\s*)?"
                r"(?:en\s+realidad\s+)?"
                r"(?:es|era|ser[ií]a)\s+"
                r"(?:el\s+|la\s+)?(.+)$"
            ),
        )

        location_markers = (
            "estudio",
            "movil",
            "unidad movil",
            "control central",
            "sala",
            "plataforma",
        )

        for pattern in patterns:
            match = re.match(
                pattern,
                cleaned_text,
                flags=re.IGNORECASE,
            )

            if match is None:
                continue

            value = self._clean_direct_value(
                match.group(1)
            )

            normalized_value = (
                self._normalize_quick_text(
                    value
                )
            )

            if not any(
                marker in normalized_value
                for marker in location_markers
            ):
                continue

            if self._looks_like_compound_reply(value):
                continue

            if value:
                value = (
                    value[0].upper()
                    + value[1:]
                )

            return value

        return None

    def _extract_explicit_field_correction(
        self,
        text: str,
    ) -> tuple[str, str] | None:
        cleaned_text = " ".join(
            text.strip().split()
        )
        cleaned_text = re.sub(
            r"^no\s*[,;:\-]\s*",
            "",
            cleaned_text,
            count=1,
            flags=re.IGNORECASE,
        )

        patterns = (
            (
                "equipment",
                (
                    r"^(?:el\s+)?equipo"
                    r"(?:\s+correcto)?"
                    r"\s+(?:es|sería|seria)\s+(.+)$"
                ),
            ),
            (
                "area",
                (
                    r"^quiero\s+corregir\s+"
                    r"(?:el\s+|la\s+)?[áa]rea"
                    r"\s*[,;:\-]?\s*"
                    r"(?:la\s+correcta\s+)?"
                    r"(?:es|sería|seria)\s+(.+)$"
                ),
            ),
            (
                "area",
                (
                    r"^(?:el\s+|la\s+)?[áa]rea"
                    r"(?:\s+correcta)?"
                    r"\s+(?:es|sería|seria)\s+(.+)$"
                ),
            ),
            (
                "location_context",
                (
                    r"^(?:la\s+)?ubicaci[óo]n"
                    r"(?:\s+correcta)?"
                    r"\s+(?:es|sería|seria)\s+(.+)$"
                ),
            ),
            (
                "operational_impact",
                (
                    r"^(?:el\s+)?impacto"
                    r"(?:\s+operacional)?"
                    r"(?:\s+correcto)?"
                    r"\s+(?:es|sería|seria)\s+(.+)$"
                ),
            ),
            (
                "priority",
                (
                    r"^(?:la\s+)?prioridad"
                    r"(?:\s+correcta)?"
                    r"\s+(?:es|sería|seria)\s+(.+)$"
                ),
            ),
            (
                "failure_type",
                (
                    r"^(?:el\s+)?s[íi]ntoma"
                    r"(?:\s+correcto)?"
                    r"\s+(?:es|sería|seria)\s+(.+)$"
                ),
            ),
            (
                "description",
                (
                    r"^(?:la\s+)?descripci[óo]n"
                    r"(?:\s+correcta)?"
                    r"\s+(?:es|sería|seria)\s+(.+)$"
                ),
            ),
        )

        for field_name, pattern in patterns:
            match = re.match(
                pattern,
                cleaned_text,
                flags=re.IGNORECASE,
            )

            if match is not None:
                value = self._clean_direct_value(
                    match.group(1)
                )

                if value:
                    return (
                        field_name,
                        value,
                    )

        return None

    def _apply_direct_field_value(
        self,
        extracted_data: dict,
        field_name: str,
        value: str,
    ) -> None:
        previous_value = extracted_data.get(
            field_name
        )

        if field_name == "area" and value:
            value = self._normalize_area_value(
                value
            )

        if field_name == "location_context" and value:
            value = self._clean_location_value(
                value
            )

        if field_name == "equipment" and value:
            value = self._clean_equipment_value(
                value
            )

        if field_name == "operational_impact":
            value = (
                self._clean_operational_impact_value(
                    value
                )
            )

        if previous_value and self._normalize_quick_text(str(previous_value)) == self._normalize_quick_text(str(value)):
            return

        extracted_data[field_name] = value

        if field_name == "location_context":
            extracted_data["unit"] = value

        if field_name == "failure_type":
            self._synchronize_description_after_failure_correction(
                extracted_data=extracted_data,
                previous_value=previous_value,
                new_value=value,
            )

        elif field_name in {
            "equipment",
            "location_context",
        }:
            self._synchronize_description_after_direct_correction(
                extracted_data=extracted_data,
                previous_value=previous_value,
                new_value=value,
            )

    def _append_failure_detail_to_description(
        self,
        extracted_data: dict,
        value: str,
    ) -> None:
        detail = self._clean_direct_value(
            value
        )

        if not detail:
            return

        description = (
            extracted_data.get("description")
            or ""
        ).strip()

        normalized_detail = (
            self._normalize_quick_text(
                detail
            )
        )

        if (
            description
            and normalized_detail
            in self._normalize_quick_text(
                description
            )
        ):
            return

        detail = (
            detail[0].upper()
            + detail[1:]
        )

        if not detail.endswith("."):
            detail += "."

        if description:
            description = description.rstrip()

            if not description.endswith("."):
                description += "."

            extracted_data["description"] = (
                f"{description} {detail}"
            )

        else:
            extracted_data["description"] = (
                detail
            )

    def _synchronize_description_after_failure_correction(
        self,
        extracted_data: dict,
        previous_value: str | None,
        new_value: str,
    ) -> None:
        description = str(
            extracted_data.get("description") or ""
        ).strip()

        equipment = self._clean_direct_value(
            str(
                extracted_data.get("equipment")
                or ""
            )
        )

        new_value = self._clean_direct_value(
            str(new_value or "")
        )

        if not equipment or not new_value:
            return

        normalized_new_value = (
            self._normalize_quick_text(
                new_value
            )
        )

        normalized_equipment = (
            self._normalize_quick_text(
                equipment
            )
        )

        feminine_equipment_prefixes = (
            "camara",
            "consola",
            "antena",
            "luminaria",
            "pantalla",
        )

        if normalized_equipment.startswith(
            feminine_equipment_prefixes
        ):
            subject = f"La {equipment}"
        else:
            subject = f"El {equipment}"

        lower_new_value = (
            new_value[0].lower()
            + new_value[1:]
        )

        direct_predicate_prefixes = (
            "no ",
            "pierde ",
            "presenta ",
            "tiene ",
            "queda ",
            "quedo ",
            "se ",
            "esta ",
        )

        if normalized_new_value.startswith(
            "sin "
        ):
            main_sentence = (
                f"{subject} está "
                f"{lower_new_value}."
            )

        elif normalized_new_value.startswith(
            direct_predicate_prefixes
        ):
            main_sentence = (
                f"{subject} "
                f"{lower_new_value}."
            )

        else:
            main_sentence = (
                f"{subject} presenta "
                f"{lower_new_value}."
            )

        details = []

        if description:
            sentences = [
                sentence.strip()
                for sentence in re.split(
                    r"(?<=[.!?])\s+",
                    description,
                )
                if sentence.strip()
            ]

            if sentences:
                first_sentence = sentences[0]

                inline_detail_match = re.search(
                    r"\s+y\s+(.+)$",
                    first_sentence,
                    flags=re.IGNORECASE,
                )

                if inline_detail_match is not None:
                    inline_detail = (
                        self._clean_direct_value(
                            inline_detail_match.group(1)
                        )
                    )

                    if inline_detail:
                        details.append(
                            inline_detail
                        )

                details.extend(
                    sentences[1:]
                )

        previous_normalized = (
            self._normalize_quick_text(
                str(previous_value)
            )
            if previous_value
            else ""
        )

        seen_details = {
            self._normalize_quick_text(
                main_sentence
            )
        }

        formatted_details = []

        for detail in details:
            clean_detail = (
                self._clean_direct_value(
                    str(detail)
                )
            )

            if not clean_detail:
                continue

            normalized_detail = (
                self._normalize_quick_text(
                    clean_detail
                )
            )

            if not normalized_detail:
                continue

            if normalized_detail in seen_details:
                continue

            if (
                previous_normalized
                and normalized_detail
                == previous_normalized
            ):
                continue

            clean_detail = (
                clean_detail[0].upper()
                + clean_detail[1:]
            )

            if not clean_detail.endswith(
                (".", "!", "?")
            ):
                clean_detail += "."

            formatted_details.append(
                clean_detail
            )

            seen_details.add(
                normalized_detail
            )

        extracted_data["description"] = " ".join(
            [
                main_sentence,
                *formatted_details,
            ]
        )

    def _synchronize_description_after_direct_correction(
        self,
        extracted_data: dict,
        previous_value: str | None,
        new_value: str,
    ) -> None:
        description = extracted_data.get(
            "description"
        )

        if (
            not previous_value
            or not new_value
        ):
            return

        previous_value = str(
            previous_value
        ).strip()

        new_value = str(
            new_value
        ).strip()

        if (
            not previous_value
            or previous_value == new_value
        ):
            return

        pattern = re.compile(
            (
                r"(?<!\w)"
                + re.escape(previous_value)
                + r"(?!\w)"
            ),
            flags=re.IGNORECASE,
        )

        for field_name in ("description", "failure_type", "operational_impact"):
            value = extracted_data.get(field_name)
            if isinstance(value, str):
                extracted_data[field_name] = pattern.sub(new_value, value)

    def _clean_equipment_value(
        self,
        value: str,
    ) -> str:
        clean_value = self._clean_direct_value(
            value
        )

        if not clean_value:
            return clean_value

        # Eliminar explicaciones agregadas después del nombre
        # del equipo.
        clean_value = re.sub(
            (
                r"\s+que\s+"
                r"(?:tiene|presenta)\s+"
                r"(?:problemas?|fallas?|una\s+falla)"
                r"(?:\s+.*)?$"
            ),
            "",
            clean_value,
            count=1,
            flags=re.IGNORECASE,
        ).strip()

        # Eliminar introducciones conversacionales:
        # "Es la consola...", "Es el router...", etc.
        clean_value = re.sub(
            r"^(?:es\s+)?(?:el|la)\s+",
            "",
            clean_value,
            count=1,
            flags=re.IGNORECASE,
        ).strip()

        if not clean_value:
            return value

        return (
            clean_value[0].upper()
            + clean_value[1:]
        )

    def _clean_location_value(
        self,
        value: str,
    ) -> str:
        clean_value = self._clean_direct_value(
            value
        )

        if not clean_value:
            return clean_value

        prefix_patterns = (
            r"^estamos\s+en\s+",
            r"^estoy\s+en\s+",
            r"^esta\s+en\s+",
            r"^está\s+en\s+",
            r"^se\s+encuentra\s+en\s+",
            r"^queda\s+en\s+",
            r"^la\s+ubicacion\s+es\s+",
            r"^la\s+ubicación\s+es\s+",
            r"^ubicacion\s+es\s+",
            r"^ubicación\s+es\s+",
            r"^es\s+en\s+",
            r"^era\s+en\s+",
            r"^seria\s+en\s+",
            r"^sería\s+en\s+",
            r"^aca\s+en\s+",
            r"^acá\s+en\s+",
            r"^aqui\s+en\s+",
            r"^aquí\s+en\s+",
        )

        for pattern in prefix_patterns:
            updated_value = re.sub(
                pattern,
                "",
                clean_value,
                count=1,
                flags=re.IGNORECASE,
            )

            if updated_value != clean_value:
                clean_value = updated_value.strip()
                break

        clean_value = re.sub(
            r"^(?:el|la)\s+",
            "",
            clean_value,
            count=1,
            flags=re.IGNORECASE,
        ).strip()

        if not clean_value:
            return value

        return (
            clean_value[0].upper()
            + clean_value[1:]
        )

    def _clean_direct_value(
        self,
        text: str,
    ) -> str:
        value = " ".join(
            text.strip().split()
        )

        return value.strip(" .")

    def _clean_operational_impact_value(
        self,
        value: str,
    ) -> str:
        clean_value = self._clean_direct_value(
            value
        )

        if not clean_value:
            return clean_value

        filler_pattern = (
            r"(?:"
            r"ah+m*|"
            r"eh+m*|"
            r"em+|"
            r"hm+|"
            r"mm+|"
            r"uh+m*|"
            r"este+|"
            r"si|"
            r"sí|"
            r"claro|"
            r"correcto|"
            r"exacto|"
            r"ya|"
            r"bueno|"
            r"ok|"
            r"okay|"
            r"vale|"
            r"pues|"
            r"entonces|"
            r"mira|"
            r"oye|"
            r"bueno\s+si|"
            r"bueno\s+sí|"
            r"claro\s+si|"
            r"claro\s+sí|"
            r"a\s+ver|"
            r"en\s+realidad"
            r")"
        )

        clean_value = re.sub(
            (
                r"^(?:"
                + filler_pattern
                + r"[\s,;:\-]*)+"
            ),
            "",
            clean_value,
            flags=re.IGNORECASE,
        ).strip()

        conversational_replacements = (
            (
                r"^nos\s+obliga\s+a\s+",
                "Obliga a ",
            ),
            (
                r"^nos\s+impide\s+",
                "Impide ",
            ),
            (
                r"^no\s+nos\s+permite\s+",
                "No permite ",
            ),
            (
                r"^no\s+nos\s+deja\s+",
                "No permite ",
            ),
            (
                r"^nos\s+deja\s+sin\s+",
                "Deja sin ",
            ),
        )

        for pattern, replacement in conversational_replacements:
            updated_value = re.sub(
                pattern,
                replacement,
                clean_value,
                count=1,
                flags=re.IGNORECASE,
            )

            if updated_value != clean_value:
                clean_value = updated_value.strip()
                break

        if not clean_value:
            return value

        return (
            clean_value[0].upper()
            + clean_value[1:]
        )
    def _normalize_area_value(
        self,
        value: str | None,
    ) -> str | None:
        if not value:
            return None

        clean_value = " ".join(
            str(value).strip().split()
        )

        normalized_value = (
            self._normalize_quick_text(
                clean_value
            )
        )

        return self.AREA_CANONICAL_LABELS.get(
            normalized_value,
            clean_value,
        )

    def _is_waiting_only_for_priority(
        self,
        draft: IncidentDraft | None,
    ) -> bool:
        if draft is None:
            return False

        missing_fields = (
            draft.missing_fields
            or []
        )

        return (
            len(missing_fields) == 1
            and missing_fields[0] == "priority"
        )

    def _extract_explicit_priority(
        self,
        text: str,
    ) -> str | None:
        # Ignorar preguntas, conservando una declaración independiente:
        # «¿Puedes ayudarme? La prioridad es alta» sí declara Alta.
        text = re.sub(r"¿[^?]*(?:\?|$)|[^.!?¿]*\?", " ", text)
        if not text.strip():
            return None
        normalized_text = (
            self._normalize_quick_text(
                text
            )
        )
        normalized_text = re.sub(
            r"\bno\s+(?:(?:es|era|sea|la|de|prioridad)\s+)*(?:baja|media|alta|critica|low|medium|high|critical)\b",
            "", normalized_text,
        )

        priority_words = {
            "baja": Incident.Priority.LOW,
            "low": Incident.Priority.LOW,
            "media": Incident.Priority.MEDIUM,
            "medium": Incident.Priority.MEDIUM,
            "alta": Incident.Priority.HIGH,
            "high": Incident.Priority.HIGH,
            "critica": Incident.Priority.CRITICAL,
            "critical": Incident.Priority.CRITICAL,
        }

        words = normalized_text.split()

        detected_priorities = {
            priority_words[word]
            for word in words
            if word in priority_words
        }

        if len(detected_priorities) != 1:
            return None

        return detected_priorities.pop()

    def _normalize_quick_text(
        self,
        text: str,
    ) -> str:
        normalized_text = (
            text.strip().lower()
        )

        normalized_text = (
            unicodedata.normalize(
                "NFD",
                normalized_text,
            )
        )

        normalized_text = "".join(
            character
            for character in normalized_text
            if unicodedata.category(
                character
            ) != "Mn"
        )

        for character in (
            "¿",
            "?",
            "¡",
            "!",
            ".",
            ",",
            ";",
            ":",
        ):
            normalized_text = (
                normalized_text.replace(
                    character,
                    "",
                )
            )

        normalized_text = " ".join(
            normalized_text.split()
        )

        return normalized_text

    def _get_quick_conversation_reply(
        self,
        text: str,
    ) -> str | None:
        normalized_text = (
            self._normalize_quick_text(
                text
            )
        )

        greetings = {
            "hola": (
                "Hola. ¿En qué puedo ayudarte?"
            ),
            "buenos dias": (
                "Buenos días. ¿En qué puedo ayudarte?"
            ),
            "buenas tardes": (
                "Buenas tardes. ¿En qué puedo ayudarte?"
            ),
            "buenas noches": (
                "Buenas noches. ¿En qué puedo ayudarte?"
            ),
            "hola buenos dias": (
                "Buenos días. ¿En qué puedo ayudarte?"
            ),
            "hola buenas tardes": (
                "Buenas tardes. ¿En qué puedo ayudarte?"
            ),
            "hola buenas noches": (
                "Buenas noches. ¿En qué puedo ayudarte?"
            ),
            "hola como estas": (
                "Hola. Muy bien, gracias. ¿En qué puedo ayudarte?"
            ),
        }

        if normalized_text in greetings:
            return greetings[
                normalized_text
            ]

        thanks = {
            "gracias",
            "muchas gracias",
            "gracias por la ayuda",
            "gracias por tu ayuda",
            "perfecto gracias",
            "ok gracias",
        }

        if normalized_text in thanks:
            return (
                "De nada. Quedo atento si necesitas "
                "registrar otro incidente."
            )

        waiting_messages = {
            "un momento",
            "un segundo",
            "espera un momento",
            "dame un momento",
            "estoy revisando",
            "estoy verificando",
            "un momento estoy revisando",
            "un momento estoy verificando",
            "dejame revisar",
        }

        if normalized_text in waiting_messages:
            return (
                "De acuerdo. Quedo atento mientras realizas "
                "la revisión."
            )

        farewells = {
            "adios",
            "hasta luego",
            "nos vemos",
            "que estes bien",
        }

        if normalized_text in farewells:
            return (
                "Hasta luego. Quedo disponible si necesitas "
                "registrar algún incidente."
            )

        return None
