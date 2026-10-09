import re
import unicodedata

from django.db import transaction
from django.utils import timezone

from incidents.ai.factory import get_ai_provider
from incidents.models import (
    ChannelMessage,
    Incident,
    IncidentDraft,
)


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
        "confirmo",
        "confirmado",
        "correcto",
        "crear reporte",
        "crear incidente",
    }

    CONFIRMATION_NEGATIVE = {
        "no",
        "no confirmo",
        "quiero corregir",
        "corregir",
        "quiero cambiar",
        "cambiar",
    }

    def __init__(self, ai_provider=None):
        self.ai_provider = ai_provider or get_ai_provider()

    @transaction.atomic
    def process(self, message: ChannelMessage):
        if message.technician is None:
            raise ValueError(
                "El mensaje debe estar asociado a un reportante."
            )

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

        elif self._is_waiting_only_for_priority(draft):
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
                "area": previous_data.get("area"),
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
                "description": previous_data.get("description"),
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

            try:
                extraction = self.ai_provider.extract_incident(
                    message=ai_message,
                    previous_data=previous_data,
                )
            except TimeoutError:
                return self._handle_ai_timeout(
                    message=message,
                    draft=draft,
                    previous_data=previous_data,
                    pending_messages=pending_messages,
                )

            if extraction.intent == "conversation":
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

            extracted_data = {
                "unit": (
                    extraction.unit
                    or previous_data.get("unit")
                ),
                "area": (
                    extraction.area
                    or previous_data.get("area")
                ),
                "location_context": (
                    extraction.location_context
                    or previous_data.get("location_context")
                ),
                "asset_category": (
                    extraction.asset_category
                    or previous_data.get("asset_category")
                ),
                "equipment": (
                    extraction.equipment
                    or previous_data.get("equipment")
                ),
                "failure_type": (
                    extraction.failure_type
                    or previous_data.get("failure_type")
                ),
                "description": (
                    extraction.description
                    or previous_data.get("description")
                ),
                "operational_impact": (
                    extraction.operational_impact
                    or previous_data.get("operational_impact")
                ),
                "priority": (
                    extraction.priority
                    or previous_data.get("priority")
                ),
            }

            clarification_question = (
                extraction.clarification_question
            )

        location_context = (
            extracted_data.get("location_context")
            or extracted_data.get("unit")
        )

        if location_context:
            extracted_data["location_context"] = location_context

            if not extracted_data.get("unit"):
                extracted_data["unit"] = location_context

        details_were_generic = self._sanitize_incident_details(
            extracted_data
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

        missing_fields = [
            field_name
            for field_name in self.REQUIRED_FIELDS
            if not extracted_data.get(field_name)
        ]

        if details_were_generic:
            clarification_question = self.FALLBACK_QUESTIONS[
                "failure_type"
            ]

        elif missing_fields and not clarification_question:
            clarification_question = self.FALLBACK_QUESTIONS[
                missing_fields[0]
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
            return self._create_incident_from_draft(
                message=message,
                draft=draft,
            )

        if normalized_message in self.CONFIRMATION_NEGATIVE:
            reply = (
                "De acuerdo. Indícame qué dato quieres corregir "
                "antes de crear el incidente."
            )

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

        area = extracted_data.get("area") or "No informada"

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
                extracted_data.get("asset_category") or ""
            ),
            equipment=extracted_data["equipment"],
            failure_type=extracted_data["failure_type"],
            description=extracted_data["description"],
            operational_impact=(
                extracted_data.get("operational_impact") or ""
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

        saved_pending_messages = list(pending_messages)

        if self._should_store_pending_message(message.content):
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
        draft.last_question = self.AI_TIMEOUT_REPLY
        draft.status = IncidentDraft.Status.COLLECTING
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

        if not isinstance(pending_messages, list):
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

        normalized_current = self._normalize_quick_text(
            current_message
        )

        if normalized_current in {
            "continuar",
            "continua",
            "continuemos",
            "seguir",
            "sigamos",
        }:
            return "\n".join(pending_messages)

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
        normalized_text = self._normalize_quick_text(text)

        return normalized_text not in {
            "continuar",
            "continua",
            "continuemos",
            "seguir",
            "sigamos",
        }

    def _try_process_direct_draft_reply(
        self,
        draft: IncidentDraft | None,
        text: str,
        previous_data: dict,
        pending_messages: list[str],
    ) -> tuple[dict, str | None] | None:
        if draft is None:
            return None

        normalized_text = self._normalize_quick_text(text)

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

        correction = self._extract_explicit_field_correction(
            candidate_text
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

            extracted_data = dict(previous_data)

            if field_name == "priority":
                priority = self._extract_explicit_priority(
                    value
                )

                if priority is None:
                    return (
                        extracted_data,
                        self.PRIORITY_GUIDANCE,
                    )

                value = priority

            else:
                value = self._clean_direct_value(value)

                if not self._is_valid_direct_field_value(
                    field_name=field_name,
                    value=value,
                    previous_data=extracted_data,
                ):
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

        expected_field = (
            self._infer_expected_field_from_last_question(
                draft.last_question
            )
        )

        if expected_field is None:
            return None

        if self._looks_like_compound_reply(text):
            return None

        extracted_data = dict(previous_data)

        if expected_field == "priority":
            priority = self._extract_explicit_priority(
                text
            )

            if priority is None:
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

        value = self._clean_direct_value(text)

        if not value:
            return None

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
        normalized_value = self._normalize_quick_text(
            value
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
            if normalized_value in self.GENERIC_FAILURE_TYPES:
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
                "interrumpe",
                "impide",
                "retrasa",
                "degrada",
                "afecta",
                "sin impacto",
                "no afecta",
                "respaldo",
                "puede continuar",
                "permite continuar",
                "continuar operando",
                "continua operando",
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
                previous_data.get("location_context"),
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

    def _looks_like_compound_reply(
        self,
        text: str,
    ) -> bool:
        normalized_text = self._normalize_quick_text(text)

        if text.count(",") >= 2:
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

    def _infer_expected_field_from_last_question(
        self,
        question: str,
    ) -> str | None:
        if not question:
            return None

        normalized_question = self._normalize_quick_text(
            question
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
            or "contexto operacional" in normalized_question
        ):
            return "location_context"

        if (
            "sintoma" in normalized_question
            or "comportamiento anormal" in normalized_question
            or "que problema presenta" in normalized_question
        ):
            return "failure_type"

        if (
            "puedes describir" in normalized_question
            or "describe con un poco mas de detalle"
            in normalized_question
        ):
            return "description"

        if "que equipo" in normalized_question:
            return "equipment"

        return None
    
    def _extract_natural_location_correction(
        self,
        text: str,
    ) -> str | None:
        cleaned_text = " ".join(
            text.strip().split()
        )

        patterns = (
            (
                r"^(?:en\s+realidad\s+)?"
                r"(?:es|sería|seria)\s+en\s+"
                r"(?:el\s+|la\s+)?(.+)$"
            ),
            (
                r"^(?:en\s+realidad\s+)?"
                r"(?:es|sería|seria)\s+"
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

        patterns = (
            (
                "equipment",
                (
                    r"^(?:el\s+)?equipo(?:\s+correcto)?"
                    r"\s+(?:es|sería|seria)\s+(.+)$"
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
            value = (
                value[0].upper()
                + value[1:]
            )

        extracted_data[field_name] = value

        if field_name == "location_context":
            extracted_data["unit"] = value

        if field_name in {
            "equipment",
            "location_context",
            "failure_type",
        }:
            self._synchronize_description_after_direct_correction(
                extracted_data=extracted_data,
                previous_value=previous_value,
                new_value=value,
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
            not description
            or not previous_value
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

        if pattern.search(description) is None:
            return

        extracted_data["description"] = pattern.sub(
            new_value,
            description,
            count=1,
        )

    def _clean_direct_value(
        self,
        text: str,
    ) -> str:
        value = " ".join(
            text.strip().split()
        )

        return value.strip(" .")

    def _is_waiting_only_for_priority(
        self,
        draft: IncidentDraft | None,
    ) -> bool:
        if draft is None:
            return False

        missing_fields = draft.missing_fields or []

        return (
            len(missing_fields) == 1
            and missing_fields[0] == "priority"
        )

    def _extract_explicit_priority(
        self,
        text: str,
    ) -> str | None:
        normalized_text = self._normalize_quick_text(text)

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

    def _normalize_quick_text(self, text: str) -> str:
        normalized_text = text.strip().lower()

        normalized_text = unicodedata.normalize(
            "NFD",
            normalized_text,
        )

        normalized_text = "".join(
            character
            for character in normalized_text
            if unicodedata.category(character) != "Mn"
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
            normalized_text = normalized_text.replace(
                character,
                "",
            )

        normalized_text = " ".join(
            normalized_text.split()
        )

        return normalized_text

    def _get_quick_conversation_reply(
        self,
        text: str,
    ) -> str | None:
        normalized_text = self._normalize_quick_text(text)

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
            return greetings[normalized_text]

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