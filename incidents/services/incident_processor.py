import re
import logging
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
from incidents.services.conversation_content import (
    join_description, preserve_modality, query_fields, time_fact, TIME,
    is_diagnostic_uncertainty, preserve_description_context, replace_time, unknown_area_statement,
    unknown_information, backup_reference, backup_transition, operational_description,
    sentences, faithful_rewrite, rewrite_requested, natural_confirmation, natural_cancel,
    observable_intermittence,
    direct_action_text,
    report_preparation_requested,
)
from incidents.services.incident_draft_service import IncidentDraftService
from incidents.services.incident_dialogue import explanation_requested, saved_data_query, priority_update_statement

logger = logging.getLogger(__name__)
from incidents.services.conversation_plan import actions, fact_updates, authorized, merge_operational_facts


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

    IMPACT_EXPLANATION = (
        "Me refiero a cómo afecta la falla a tu trabajo. "
        "Por ejemplo: interrumpe una grabación, obliga a usar "
        "un respaldo o permite continuar normalmente."
    )

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
        "operational_impact": IMPACT_EXPLANATION + " ¿Qué impacto operacional tiene?",
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
            "¿Cuál es el área correcta?"
        ),
        "location_context": (
            "¿Cuál es la ubicación o contexto correcto?"
        ),
        "equipment": (
            "¿Qué equipo o servicio es el correcto?"
        ),
        "failure_type": (
            "¿Cuál es el síntoma correcto?"
        ),
        "description": (
            "¿Cuál es la descripción correcta?"
        ),
        "operational_impact": (
            "¿Cuál es el impacto operacional correcto?"
        ),
        "priority": (
            "¿Cuál es la prioridad correcta: "
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

    PRIORITY_EXPLANATIONS = {
        "low": "incidente menor, sin impacto operacional relevante y que puede esperar.",
        "medium": "existe afectación, pero la operación puede mantenerse mediante una alternativa, redundancia o una degradación aceptable.",
        "high": "una función importante está fuera de servicio o existe un impacto significativo en la operación o producción, sin una alternativa adecuada.",
        "critical": "existe una interrupción o riesgo inmediato para la emisión, señal, producción en vivo, un servicio esencial o una contingencia operacional severa.",
    }
    PRIORITY_REFERENCE = "\n".join(f"• {Incident.Priority(value).label}: {detail}" for value, detail in PRIORITY_EXPLANATIONS.items())
    PRIORITY_GUIDANCE = (
        "Para finalizar el reporte necesito que selecciones una prioridad:\n\n"
        + PRIORITY_REFERENCE + "\n\n"
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
        "si confirmo la creacion del incidente",
        "si confirmo la creacion de la incidencia",
        "confirmo la creacion del incidente",
        "confirmo la creacion de la incidencia",
        "correcto crealo", "esta todo bien registralo", "dale crealo", "ya dale",
        "todo bien puedes registrarlo", "procede con el reporte", "perfecto envialo",
        "registra la incidencia", "registra el incidente", "si crealo", "si registralo",
        "ya po dale", "dale nomas crealo",
    }

    CONFIRMATION_HOLD = {
        "si no espera", "creo que si pero dejame revisar", "si aunque hay algo malo en el resumen",
        "todavia no", "espera", "no lo registres todavia", "no lo registres",
        "aun no", "no estoy seguro", "no estoy segura",
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

    def _confirmation_text(self, text):
        normalized = re.sub(r"[^\w\s]", " ", self._normalize_quick_text(text))
        normalized = " ".join(normalized.split())
        # Erratas acotadas de la palabra completa; nunca coincidencias parciales.
        return re.sub(r"\b(?:confimo|confrimo)\b", "confirmo", normalized)

    def _is_confirmation(self, text, draft=None):
        return (
            direct_action_text(text) is not None
            and (self._confirmation_text(text) in self.CONFIRMATION_POSITIVE
                 or natural_confirmation(text, [draft.extracted_data.get(name) for name in ('equipment', 'location_context')] if draft else ()))
        )

    @staticmethod
    def _conversation_reply(draft=None):
        # La IA interpreta datos; solo la aplicación puede describir su estado.
        if draft and draft.status == IncidentDraft.Status.READY:
            return "El borrador sigue pendiente de confirmación. Revisa el resumen y envía una confirmación explícita para registrarlo, o indica qué dato quieres corregir."
        if draft:
            return "El reporte sigue en borrador. Podemos completar los datos pendientes o corregir los que ya me indicaste; todavía no he confirmado su registro."
        return "Puedo ayudarte a registrar una incidencia operativa. Cuéntame qué falla y dónde ocurre; revisaremos un resumen antes de registrarla."

    def _answer_draft_query(self, draft, fields):
        if draft is None:
            return "No tienes un borrador activo. Cuéntame qué falla necesitas reportar."
        data = draft.extracted_data or {}
        if "missing" in fields:
            missing = [self.CHANGE_LABELS[name] for name in self.REQUIRED_FIELDS if not data.get(name)]
            status = "Datos pendientes: " + ", ".join(missing) if missing else "El borrador está completo y pendiente de confirmación."
            return (self._answer_draft_query(draft, fields-{'missing'}) + '\n\n' + status) if len(fields)>1 else status
        if "summary" in fields:
            reply = self._build_confirmation_reply(data).split("\n\n¿Confirmas", 1)[0]
            return reply.replace("Antes de crear el incidente, revisa los datos:", "Información guardada en el borrador:", 1) + "\n\n" + self._answer_draft_query(draft, {"missing"})
        lines = []
        for name in (*self.CHANGE_LABELS, "time", "backup"):
            if name not in fields: continue
            value = time_fact(data.get("description") or "") if name == "time" else data.get(name)
            if name == "backup":
                clauses = [part for part in sentences(data.get("operational_impact")) if backup_reference(part)]
                if not clauses:
                    clauses = [part for part in sentences(data.get("description")) if backup_reference(part) and not part.startswith(('Antecedente:', 'Antes de'))]
                value = join_description(*clauses) or 'No se ha informado el estado del respaldo.'
            if name == "time" and not value and data.get("description"):
                value = "No hay una antigüedad identificada por separado. Descripción guardada: " + data["description"]
            if name == "priority" and value in Incident.Priority.values:
                value = Incident.Priority(value).label
            if name == "asset_category" and value in Incident.AssetCategory.values:
                value = Incident.AssetCategory(value).label
            label = {'time': 'Antigüedad/inicio', 'backup': 'Respaldo'}.get(name) or self.CHANGE_LABELS[name]
            lines.append(f"• {label}: {value or 'No informado'}")
        return "Según el borrador guardado:\n\n" + "\n".join(lines)

    def _unknown_area_reply(self, draft, text):
        if not draft or not unknown_information(text):
            return None
        normalized = self._normalize_quick_text(text)
        field = next((name for name, pattern in {
            'area':r'\barea\b', 'equipment':r'\b(?:equipo|identificarlo)\b',
            'asset_category':r'\b(?:clasificacion|categoria|familia)\b',
            'failure_type':r'\b(?:sintoma|contexto tecnico)\b',
        }.items() if re.search(pattern, normalized)), None)
        # «No sé si es Cámara 2 o 3» no implica desconocer otro campo
        # pendiente. La ayuda genérica sigue usando el guiado existente.
        if not field or draft.extracted_data.get(field):
            return None
        if self._extract_explicit_priority(text) or time_fact(text):
            return None
        draft.extracted_data["_unknown_fields"] = list(dict.fromkeys([*draft.extracted_data.get('_unknown_fields', []), field]))
        other = [name for name in self.REQUIRED_FIELDS if name != field and not draft.extracted_data.get(name)]
        question = self.FALLBACK_QUESTIONS[other[0]] if other else (
            "Cuando puedas, consulta al responsable de la operación a qué área corresponde. "
            "Mientras tanto puedes consultar el resumen o corregir otros datos; conservaré el borrador sin registrarlo."
        )
        guidance = self._field_guidance(field, draft.extracted_data)
        reply = guidance + ' ' + question
        draft.last_question = question
        draft.save(update_fields=["extracted_data", "last_question", "updated_at"])
        return reply

    def _field_guidance(self, field, data=None):
        if field == 'area':
            data = data or {}
            context = ('Para ' + data['equipment'] + (' en ' + data['location_context'] if data.get('location_context') else '') + ', ') if data.get('equipment') else ''
            return (context + 'no hay problema si no conoces el área; no la voy a suponer. '
                    'El área indica quién gestiona la operación o el equipo, no solo su tipo. '
                    'Audio se relaciona con la operación de sonido; Producción con la coordinación de la producción. '
                    'Eso no determina la asignación interna de Secuoya. ¿Qué función cumple el equipo y quién lo opera o gestiona?')
        return self.GUIDED_QUESTIONS.get(field, 'Describe lo que observas sin adivinar una clasificación.')

    def _answer_explanation(self, field, priority=None):
        if field == "operational_impact":
            return self.IMPACT_EXPLANATION
        if priority:
            return (f"Como referencia:\n\n• {Incident.Priority(priority).label}: "
                    + self.PRIORITY_EXPLANATIONS[priority] + "\n\nLa categoría la eliges tú.")
        return ("La prioridad expresa la urgencia operacional del reporte:\n\n"
                + self.PRIORITY_REFERENCE + "\n\nTú eliges la categoría; no la asigno automáticamente.")

    @staticmethod
    def _dialogue_result(message, draft, action, reply):
        message.draft = draft
        message.status = ChannelMessage.Status.PROCESSED
        message.processed_at = timezone.now()
        message.save(update_fields=['draft', 'status', 'processed_at'])
        return {'action': action, 'incident_created': False, 'draft_id': draft.pk if draft else None, 'reply': reply}

    def _cancel_draft(self, message):
        draft = IncidentDraftService().cancel_active_draft(technician=message.technician)
        return self._dialogue_result(message, draft, 'draft_cancelled' if draft else 'no_active_draft',
            'El reporte pendiente fue cancelado.' if draft else 'No tienes ningún reporte pendiente.')

    def _semantic_dialogue(self, message, draft, extraction):
        action = getattr(extraction, 'dialogue_action', None)
        acts = actions(extraction)
        text = self._text(message)
        previous = draft.extracted_data if draft else {}
        has_facts = fact_updates(extraction, previous, text)
        if draft is None and 'confirm' in acts:
            last = IncidentDraft.objects.filter(technician=message.technician).order_by('-updated_at').first()
            if last and last.status == IncidentDraft.Status.COMPLETED and authorized(extraction, text, last, 'confirm'):
                message.incident = last.incident
                message.save(update_fields=['incident'])
                result = self._dialogue_result(message, last, 'incident_already_created', f'El reporte {last.incident.code} ya fue registrado. No creé otra incidencia.')
                result['incident_code'] = last.incident.code
                return result
        if draft and authorized(extraction, text, draft, 'cancel'):
            return self._cancel_draft(message)
        if draft and authorized(extraction, text, draft, 'confirm'):
            if draft.status == IncidentDraft.Status.READY:
                return self._handle_ready_draft_confirmation(message, draft, semantic_authorized=True)
            missing = [name for name in self.REQUIRED_FIELDS if not draft.extracted_data.get(name)]
            reply = self.FALLBACK_QUESTIONS[missing[0]] if missing else self._build_confirmation_reply(draft.extracted_data)
            draft.status = IncidentDraft.Status.COLLECTING if missing else IncidentDraft.Status.READY
            draft.last_question = reply
            draft.save(update_fields=['status', 'last_question', 'updated_at'])
            return self._dialogue_result(message, draft, 'ask_clarification', reply)
        if action in {'query', 'help'} and (has_facts or 'update' in acts):
            return None
        if action == 'rewrite' and any(name != 'description' and quote and quote in text for name,quote in (extraction.field_evidence or {}).items()):
            return None
        if action == 'rewrite' and not rewrite_requested(text):
            return None
        evidence = getattr(extraction, 'action_evidence', None)
        if not evidence or evidence not in self._text(message):
            return None
        if action == 'query':
            fields = set(getattr(extraction, 'requested_fields', []))
            valid = {*self.CHANGE_LABELS, 'time', 'backup', 'summary', 'missing'}
            if fields and fields <= valid and not self._looks_like_natural_correction_statement(self._text(message)) and not self._priority_for_message(message):
                return self._dialogue_result(message, draft, 'draft_query', self._answer_draft_query(draft, fields))
        if action == 'help':
            field = getattr(extraction, 'help_field', None)
            if field in self.GUIDED_QUESTIONS or field == 'asset_category':
                return self._dialogue_result(message, draft, 'draft_help', self._field_guidance(field, previous))
        if action == 'rewrite' and draft:
            rewrite = faithful_rewrite(draft.extracted_data.get('description'), getattr(extraction, 'rewrite_clauses', []))
            if rewrite is None:
                return self._dialogue_result(message, draft, 'draft_rewrite_pending',
                    'Conservo la descripción: no pude verificar una reescritura que mantenga todos los hechos. Indica qué parte quieres ordenar o corregir.')
            previous = draft.extracted_data.get('description')
            if rewrite == previous:
                return self._dialogue_result(message, draft, 'draft_query', 'La descripción ya conserva esos hechos sin repeticiones.\n\n' + rewrite)
            draft.extracted_data['description'] = rewrite
            # Un cambio de redacción también invalida la autorización anterior.
            reply = 'Ordené la descripción conservando sus hechos.\n\n' + self._build_confirmation_reply(draft.extracted_data) if draft.status == IncidentDraft.Status.READY else 'Descripción actualizada:\n\n' + rewrite
            draft.last_question = reply
            draft.save(update_fields=['extracted_data', 'last_question', 'updated_at'])
            return self._dialogue_result(message, draft, 'draft_rewritten', reply)
        return None

    @staticmethod
    def _text(message):
        return getattr(message, "processing_text", message.content)

    def _priority_for_message(self, message):
        ids = message.processing_result.get("_group_member_ids", [])
        if len(ids) > 1:
            rows = list(ChannelMessage.objects.filter(pk__in=ids, technician_id=message.technician_id))
            rows.sort(key=lambda row: row.raw_payload["message"]["message_id"], reverse=True)
            for row in rows:
                if saved_data_query(row.content): continue
                value = self._extract_explicit_priority(row.content)
                if value or re.search(r"\bprioridad\b", self._normalize_quick_text(row.content)):
                    return value
            return None
        return self._extract_explicit_priority(self._text(message))

    def process(self, message: ChannelMessage):
        group_metadata = {key: value for key, value in message.processing_result.items() if key.startswith("_group_")}
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
                        interpretation = outcome[2] if outcome else None
                        if interpretation and actions(interpretation):
                            result = self._compose_semantic_reply(message, result, previous, interpretation)
                        logger.info('conversation.completed message=%s draft=%s action=%s changes=%s', message.pk, result.get('draft_id'), result.get('action'), len(result.get('changes', [])))
                        result.update(group_metadata)
                        if result.get("action") == "ask_clarification" and not result.get('guidance') and not (interpretation and actions(interpretation)):
                            fields = query_fields(self._text(message)) or set()
                            for text in ChannelMessage.objects.filter(pk__in=group_metadata.get("_group_member_ids", []), technician_id=message.technician_id).values_list("content", flat=True):
                                fields.update(query_fields(text) or set())
                            if fields:
                                draft = IncidentDraft.objects.filter(pk=result.get("draft_id")).first()
                                result["reply"] = self._answer_draft_query(draft, fields) + "\n\n" + result["reply"]
                                if draft and draft.status == "ready":
                                    draft.last_question = result["reply"]
                                    draft.save(update_fields=["last_question", "updated_at"])
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

    def _compose_semantic_reply(self, message, result, previous, extraction):
        """Comunicar solo el resultado guardado; nunca prosa libre de IA."""
        if result.get('action') not in {'ask_clarification','draft_rewritten'} or not result.get('draft_id'):
            return result
        draft = IncidentDraft.objects.get(pk=result['draft_id'])
        data, acts = draft.extracted_data, actions(extraction)
        fields = set(extraction.requested_fields or [])
        explicit_fields = query_fields(self._text(message)) or set()
        fields.update(explicit_fields)
        if explicit_fields:
            acts.add('query')
        changed = [name for name in self.CHANGE_LABELS if previous and previous.get(name) != data.get(name)]
        result['semantic_actions'] = sorted(acts)
        # Una pregunta para identificar un dato pide orientación, aunque el
        # lector local también encuentre su nombre como campo consultado.
        if 'help' in acts and extraction.help_field:
            result['reply'] = self._field_guidance(extraction.help_field, data)
            return result
        # Los campos del reporte mencionados antes de «ayúdame a prepararlo»
        # no son por sí solos una solicitud de lectura de esos campos.
        if report_preparation_requested(self._text(message)) and not explicit_fields:
            acts.discard('query')
            result['semantic_actions'] = sorted(acts | {'help'})
        if 'query' in acts and not fields:
            fields = set(changed) or set((extraction.field_evidence or {})).intersection(self.CHANGE_LABELS)
        if 'query' in acts and fields and 'summary' not in fields:
            acknowledgement = ('Guardé los cambios de ' + ', '.join(self.CHANGE_LABELS[name].lower() for name in changed) + '.\n\n') if changed else ''
            focus = set(changed) or set((extraction.field_evidence or {})).intersection(self.CHANGE_LABELS)
            if fields == {'missing'} and 'update' in acts and 0 < len(focus) <= 2:
                result['reply'] = acknowledgement + self._answer_draft_query(draft, focus) + '\n\n' + self._answer_draft_query(draft, fields)
            else:
                result['reply'] = acknowledgement + self._answer_draft_query(draft, fields)
            if (extraction.authorization or {}).get('delayed'):
                result['reply'] += '\n\nEl reporte sigue en borrador; todavía no se ha registrado.'
            # READY solo valida datos. El resumen completo que quedó en
            # last_question NO se entrega aquí: confirmar lo mostrará primero.
            return result
        if draft.status != IncidentDraft.Status.READY:
            return result
        summary = self._build_confirmation_reply(data).split('\n\n', 1)[1]
        if not previous:
            intro = f"Claro, preparé un borrador de {data.get('equipment')} en {data.get('location_context')}. Revisemos los antecedentes antes de registrarlo."
        elif changed:
            intro = 'Ya quedaron guardados los cambios de ' + ', '.join(self.CHANGE_LABELS[name].lower() for name in changed) + '. Revisa cómo quedó.'
        else:
            intro = 'El borrador conserva los datos indicados. Puedes revisarlos antes de autorizar su registro.'
        result['reply'] = intro + '\n\n' + summary
        draft.last_question = result['reply']
        draft.save(update_fields=['last_question', 'updated_at'])
        return result

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
        is_correction = was_correcting or self._looks_like_natural_correction_statement(self._text(message))
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
            prefix = "Actualicé:\n\n" + "\n".join(lines)
            if draft.status == IncidentDraft.Status.READY:
                # El resumen completo ya incluye cada valor nuevo. No duplicarlo
                # en una segunda lista de cambios delante del mismo resumen.
                prefix = "Actualicé el borrador."
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

        if draft is None:
            # Repetición exacta de una autorización ya ejecutada en ESTE
            # canal/reportante. Solo reconocer el resultado persistido; no
            # generar otro borrador por una reinterpretación variable de IA.
            prior = ChannelMessage.objects.filter(
                technician=message.technician, channel_account_id=message.channel_account_id,
                provider=message.provider, direction=ChannelMessage.Direction.INBOUND,
                status=ChannelMessage.Status.PROCESSED, content=self._text(message),
                processing_result__action='incident_created', draft__status=IncidentDraft.Status.COMPLETED,
                incident__isnull=False, created_at__lt=message.created_at,
            ).select_related('incident','draft').order_by('-created_at').first()
            if prior:
                message.incident = prior.incident
                message.save(update_fields=['incident'])
                result = self._dialogue_result(message, prior.draft, 'incident_already_created',
                    f'El reporte {prior.incident.code} ya fue registrado. No creé otra incidencia.')
                result['incident_code'] = prior.incident.code
                return result

        if natural_cancel(self._text(message)):
            return self._cancel_draft(message)
        explanation = explanation_requested(self._text(message))
        if explanation:
            return self._dialogue_result(message, draft, "draft_help", self._answer_explanation(*explanation))
        fields = query_fields(self._text(message)) if saved_data_query(self._text(message)) else None
        if draft is None and fields and not re.search(r"\b(?:borrador|resumen|guardad\w*|registrad\w*|indique|indicado|falta\w*)\b", self._normalize_quick_text(self._text(message))):
            # Sin borrador, «¿Es prioridad alta?» puede pertenecer a un reporte
            # incompleto. Conservar la clasificación existente del proveedor.
            fields = None
        unknown_reply = self._unknown_area_reply(draft, self._text(message))
        if unknown_reply: fields = None
        if fields or unknown_reply:
            message.draft = draft
            message.status = ChannelMessage.Status.PROCESSED
            message.processed_at = timezone.now()
            message.save(update_fields=["draft", "status", "processed_at"])
            return {"action": "draft_query" if fields else "ask_clarification", "incident_created": False,
                    "guidance": bool(unknown_reply),
                    "draft_id": draft.pk if draft else None,
                    "extracted_data": dict(draft.extracted_data) if draft else {},
                    "missing_fields": list(draft.missing_fields) if draft else [],
                    "reply": self._answer_draft_query(draft, fields) if fields else unknown_reply}

        if outcome and outcome[2] is not None:
            semantic_result = self._semantic_dialogue(message, draft, outcome[2])
            if semantic_result is not None:
                return semantic_result

        last_context = IncidentDraft.objects.filter(technician=message.technician).order_by('-updated_at').first() if draft is None else draft
        if draft is None and self._is_confirmation(self._text(message), last_context):
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
            and draft.status != IncidentDraft.Status.READY
            and self._is_confirmation(self._text(message), draft)
        ):
            # Confirmar un borrador parcial o en corrección no puede pedir a la
            # IA que confirme por nosotros. Primero se entrega un resumen nuevo.
            missing = [name for name in self.REQUIRED_FIELDS if not draft.extracted_data.get(name)]
            draft.missing_fields = missing
            draft.status = IncidentDraft.Status.COLLECTING if missing else IncidentDraft.Status.READY
            reply = self.FALLBACK_QUESTIONS[missing[0]] if missing else self._build_confirmation_reply(draft.extracted_data)
            if missing and missing[0] in draft.extracted_data.get("_unknown_fields", []):
                reply = "Conservo el borrador sin registrarlo. El área sigue pendiente; consulta al responsable de la operación cuando puedas."
            draft.last_question = reply
            draft.save(update_fields=["missing_fields", "status", "last_question", "updated_at"])
            message.draft = draft
            message.status = ChannelMessage.Status.PROCESSED
            message.processed_at = timezone.now()
            message.save(update_fields=["draft", "status", "processed_at"])
            return {
                "action": "ask_clarification", "incident_created": False,
                "draft_id": draft.pk, "missing_fields": missing, "reply": reply,
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
            self._text(message)
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
            and self._is_clarification_request(self._text(message))
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
        if draft is not None and self._looks_like_natural_correction_statement(self._text(message)):
            correction_mode = True
        current_evidence = None
        unchanged_fields = []
        requires_update_clarification = False

        direct_draft_result = self._try_process_direct_draft_reply(
            draft=draft,
            text=self._text(message),
            previous_data=previous_data,
            pending_messages=pending_messages,
        )

        if direct_draft_result is not None:
            extracted_data, clarification_question = (
                direct_draft_result
            )
            if correction_mode and extracted_data == previous_data and clarification_question is None:
                unchanged_fields = [self._infer_correction_target_field(draft=draft, text=self._text(message), previous_data=previous_data)]

        elif (
            self._is_waiting_only_for_priority(draft)
            and not correction_mode
            and not self._looks_like_compound_reply(self._text(message))
            and not self._has_operational_information(self._text(message), previous_data)
        ):
            priority = self._extract_explicit_priority(
                self._text(message)
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
                current_message=self._text(message),
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
                declared = {self._priority_for_message(message), *(self._extract_explicit_priority(text) for text in pending_messages)}
                if extraction.priority not in declared:
                    # El proveedor no puede decidir una prioridad sin una
                    # declaración explícita del reportante.
                    extraction.priority = None

            if (
                extraction.intent == "conversation"
                and not correction_mode
                and not fact_updates(extraction, previous_data, self._text(message))
                and not (draft and (time_fact(self._text(message)) or backup_transition(self._text(message)) or rewrite_requested(self._text(message))))
            ):
                reply = self._conversation_reply(draft)

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
            if draft and rewrite_requested(self._text(message)) and not any(name != 'description' and quote and quote in self._text(message) for name,quote in (current_evidence or {}).items()):
                # Un proveedor antiguo/sin acto no puede sobrescribir hechos al
                # pedir redacción. La normalización extractiva es verificable.
                extraction.dialogue_action = 'rewrite'
                extraction.action_evidence = self._text(message)
                extraction.rewrite_clauses = sentences(previous_data.get('description'))
                return self._semantic_dialogue(message, draft, extraction)
            if draft and backup_transition(self._text(message)) and not actions(extraction) and update_kind not in {'ambiguous', 'new_incident'}:
                current_evidence = dict(current_evidence or {})
                quote = current_evidence.get('operational_impact') or current_evidence.get('description') or self._text(message)
                current_evidence['operational_impact'] = quote
                extraction.field_evidence = current_evidence
                extraction.operational_impact = quote
                update_kind = 'evolution'
            if draft and current_evidence and update_kind == 'additional' and any(
                name != 'description' and getattr(extraction, name, None) and getattr(extraction, name) != previous_data.get(name)
                for name in current_evidence if name in self.CHANGE_LABELS):
                # El contenido respaldado prevalece sobre una etiqueta vaga.
                update_kind = 'evolution'
            for name in ("description", "operational_impact", "failure_type"):
                setattr(extraction, name, preserve_modality(getattr(extraction, name), (current_evidence or {}).get(name)))
            if draft and update_kind == "new_incident":
                normalized = self._normalize_quick_text(self._text(message))
                normalized = re.sub(r"\bno (?:es|se trata de) (?:una? )?otra (?:incidencia|incidente|problema|falla|reporte)\b", "", normalized)
                explicit_new = re.search(r"\b(?:otr[oa]|nuev[oa])\s+(?:incidencia|incidente|problema|falla|reporte)\b", normalized)
                explicit_new = explicit_new or re.search(r"\b(?:incidencia|incidente|problema|falla|reporte)\s+(?:distint[oa]|diferente|independiente)\b", normalized)
                continuity = re.search(r"\b(?:mism[oa]|sobre est[ae]|ademas|tambien|sigue|continua)\b", normalized)
                same_subject = all(not getattr(extraction, field) or self._normalize_quick_text(getattr(extraction, field)) == self._normalize_quick_text(previous_data.get(field) or "")
                                   for field in ("equipment", "location_context"))
                contextual_details = bool(current_evidence) and not set(current_evidence).intersection({"area", "equipment", "location_context"})
                if not explicit_new and (continuity or (same_subject and contextual_details)):
                    update_kind = "evolution" if set(current_evidence or {}).intersection({"priority", "failure_type", "operational_impact"}) else "additional"
            if draft and (update_kind in {"ambiguous", "new_incident"} or self._is_uncertain_update(self._text(message))):
                correction_mode = True
                requires_update_clarification = True
                extracted_data = dict(previous_data)
                diagnostic_detail = (current_evidence or {}).get("description")
                if update_kind != "new_incident" and diagnostic_detail and is_diagnostic_uncertainty(diagnostic_detail):
                    extracted_data["description"] = join_description(previous_data.get("description"), diagnostic_detail)
                clarification_question = (
                    "¿Se trata de otra incidencia? Mantengo el borrador actual. Si deseas descartarlo para comenzar otro, utiliza /cancelar."
                    if update_kind == "new_incident" else
                    "¿Cuál es el dato correcto y su nuevo valor? Mantengo los datos anteriores hasta que lo aclares."
                )
            elif (correction_mode and update_kind != "additional") or (draft and update_kind in {"correction", "evolution"}):
                correction_mode = True
                correction_target_field = (
                    self._infer_correction_target_field(
                        draft=draft,
                        text=self._text(message),
                        previous_data=previous_data,
                    )
                )
                if self._contains_multiple_fields(self._text(message)):
                    correction_target_field = None
                if current_evidence and (len(current_evidence) > 1 or correction_target_field not in current_evidence):
                    correction_target_field = None

                (
                    extracted_data,
                    correction_applied,
                ) = self._merge_ai_correction(
                    previous_data=previous_data,
                    extraction=extraction,
                    message_text=self._text(message),
                    target_field=correction_target_field,
                    declared_priority=self._priority_for_message(message),
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

                # Las preguntas visibles se construyen con el estado validado;
                # no retransmitir prosa libre que pueda afirmar acciones falsas.
                clarification_question = None
                if draft and update_kind == "additional":
                    # Un dato complementario no reemplaza otro ya válido.
                    for name, old_value in previous_data.items():
                        if old_value:
                            extracted_data[name] = old_value
                    detail = (current_evidence or {}).get("description")
                    if detail and detail.casefold() not in str(previous_data.get("description", "")).casefold():
                        extracted_data["description"] = join_description(previous_data.get("description"), detail)

        intermittent = observable_intermittence(self._text(message))
        if intermittent and not requires_update_clarification:
            extracted_data['failure_type'] = intermittent

        extracted_data["asset_category"] = (
            self._refine_asset_category_from_equipment(
                equipment=extracted_data.get("equipment"),
                current_category=extracted_data.get(
                    "asset_category"
                ),
            )
        )

        self._validate_equipment_change(previous_data, extracted_data, self._text(message), current_evidence)

        contextual = dict(previous_data)
        for field in ("equipment", "location_context"):
            if contextual.get(field) != extracted_data.get(field):
                self._synchronize_description_after_direct_correction(contextual, contextual.get(field), extracted_data.get(field))
        same_family = not previous_data.get("asset_category") or previous_data.get("asset_category") == extracted_data.get("asset_category")
        if draft and same_family and backup_transition(self._text(message)) and not requires_update_clarification and not (outcome and outcome[2] and actions(outcome[2])):
            evidence = (current_evidence or {}).get('operational_impact') or self._text(message)
            extracted_data['operational_impact'] = evidence
            extracted_data['description'] = operational_description(contextual.get('description'), evidence)
        if draft and same_family and extracted_data.get("description") != previous_data.get("description"):
            extracted_data["description"] = preserve_description_context(contextual.get("description"), extracted_data.get("description"), self._text(message))
        explicit_time = time_fact(self._text(message))
        if explicit_time and draft and extracted_data.get("description") and not self._is_uncertain_update(self._text(message)):
            description = extracted_data["description"]
            extracted_data["description"] = (replace_time(description, explicit_time) if time_fact(description)
                else join_description(description, f"Duración reportada: {TIME.search(explicit_time)['duration']}"))
        interpretation = outcome[2] if outcome else None
        operational = merge_operational_facts(previous_data, interpretation, self._text(message)) if interpretation else None
        if operational and not requires_update_clarification:
            extracted_data['_operational_facts'], extracted_data['operational_impact'] = operational
        elif previous_data.get('_operational_facts'):
            extracted_data['_operational_facts'] = (previous_data['_operational_facts'] if extracted_data.get('operational_impact') == previous_data.get('operational_impact')
                else {'consequence':extracted_data.get('operational_impact')})
        if draft and not requires_update_clarification and clarification_question == self.CORRECTION_PROMPT and any(
                previous_data.get(name) != extracted_data.get(name) for name in self.REQUIRED_FIELDS):
            clarification_question = None

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
        reported_unknown_area = unknown_area_statement(self._text(message))
        if reported_unknown_area:
            extracted_data["area"] = None
        if "area" in previous_data.get("_unknown_fields", []) and not previous_data.get("area"):
            candidate_area = extracted_data.get("area")
            if candidate_area and self._normalize_quick_text(candidate_area) not in self._normalize_quick_text(self._text(message)):
                extracted_data["area"] = None
        missing_fields = [name for name in self.REQUIRED_FIELDS if not extracted_data.get(name)]
        deferred = [name for name in set(previous_data.get("_unknown_fields", []) + (["area"] if reported_unknown_area else [])) if name in missing_fields]
        if deferred:
            extracted_data["_unknown_fields"] = deferred
            missing_fields.sort(key=lambda name: name in deferred)

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

        is_new_draft = draft is None
        if missing_fields and missing_fields[0] in deferred and not requires_update_clarification:
            clarification_question = (
                "Conservo el borrador. El área sigue pendiente: puedes consultarla con el responsable "
                "de la operación y volver cuando la conozcas, o consultar/corregir los datos guardados."
            )
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
            if is_new_draft and extracted_data.get("equipment") and extracted_data.get("location_context"):
                equipment, location = extracted_data["equipment"], extracted_data["location_context"]
                if max(len(equipment), len(location)) <= 80:
                    clarification_question = f"Entiendo. Para el borrador tengo «{equipment}» en «{location}». {clarification_question}"
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
        *, semantic_authorized=False,
    ):
        normalized_message = self._normalize_quick_text(
            self._text(message)
        )

        if semantic_authorized or self._is_confirmation(self._text(message), draft):
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
                # La consulta explícita de TODO el resumen también puede
                # entregar los datos vigentes sin mutar last_question.
                # Comparar el texto completo actual, mismo borrador, SENT y
                # anterior a la autorización; una consulta parcial no sirve.
                content__in=[draft.last_question, self._answer_draft_query(draft, {'summary'})],
                processed_at__lte=message.created_at,
            ).exists():
                # READY significa datos válidos. En Telegram, la confirmación
                # exige haber entregado ese resumen antes de recibirla. Una
                # confirmación en cola no autoriza un resumen enviado al recuperar.
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
                self._text(message)
            )
        )

        if (
            requested_field is not None
            and self._is_correction_field_selection(
                text=self._text(message),
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

        if rewrite_requested(self._text(message)):
            return None

        if self._looks_like_natural_correction_statement(
            self._text(message)
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
            or (normalized_message.startswith("no ") and not self._is_uncertain_update(self._text(message)))
            or self._confirmation_text(self._text(message)) in self.CONFIRMATION_HOLD
            or (any(mark in self._text(message) for mark in ("?", "¿")) and self._confirmation_text(self._text(message)) in self.CONFIRMATION_POSITIVE)
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
        declared_priority=None,
    ) -> tuple[dict, bool]:
        extracted_data = dict(
            previous_data
        )

        candidate_values = {
            "area": extraction.area,
            "asset_category": extraction.asset_category,
            "location_context": extraction.location_context,
            "equipment": extraction.equipment,
            "failure_type": extraction.failure_type,
            "operational_impact": extraction.operational_impact,
            "priority": extraction.priority,
        }
        declared_priority = declared_priority or self._extract_explicit_priority(message_text)
        if declared_priority:
            candidate_values["priority"] = declared_priority

        structural_fields = (
            "area",
            "asset_category",
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
                explicit_priority = declared_priority

                if explicit_priority is None:
                    continue

                candidate_value = explicit_priority

            elif field_name == "asset_category":
                if candidate_value not in Incident.AssetCategory.values:
                    continue

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
            or bool(evidence and evidence.get("description"))
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
                f"La incidencia {incident.code} fue registrada correctamente."
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
            self._text(message)
        ):
            saved_pending_messages.append(
                self._text(message).strip()
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
        if self._contains_multiple_fields(text) or time_fact(text) or query_fields(text) or rewrite_requested(text) or backup_transition(text) or text.startswith("[Secuencia ordenada"):
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
                    "Agregué ese síntoma al reporte. "
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
        parts = [part for part in re.split(r"[.;\n]+", text) if not is_diagnostic_uncertainty(part)]
        return bool(re.search(r"\b(?:quizas?|tal vez|no se si|no estoy segur[oa]|podria ser)\b",
                              self._normalize_quick_text(" ".join(parts))))

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
        # Una petición cortés explícita de modificar el borrador no es una
        # consulta informativa. Reutilizar las validaciones de prioridad única,
        # negada y declarada; no aceptar una prioridad decidida solo por IA.
        request = priority_update_statement(text)
        if request:
            text = request + ". " + text
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

        # Una asignación propia explícita prevalece sobre una prioridad
        # atribuida a terceros. Una negación nunca cuenta como confirmación.
        declaration = r"(?:asigno|selecciono|confirmo|establezco)\s+(?:la\s+)?prioridad\s+(?:es\s+)?(baja|media|alta|critica|low|medium|high|critical)\b"
        normalized_text = re.sub(r"\bno\s+" + declaration, "", normalized_text)
        assigned = {priority_words[word] for word in re.findall(r"\b" + declaration, normalized_text)}
        if assigned:
            return assigned.pop() if len(assigned) == 1 else None

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
                "Tómate el tiempo para revisar. Sigo aquí cuando quieras continuar."
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
