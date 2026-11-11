"""Distinciones locales de lectura, ayuda y petición; exclusivas de Incidencias.

Estos detectores solo eligen un camino. No extraen valores, autorizan acciones
ni sustituyen el plan semántico y las validaciones del procesador Django.
"""
import re

from .conversation_content import is_query_only, normalized


def polite_update_requested(text):
    value = normalized(text)
    if any(mark in value for mark in ('«', '»', '"', "'", '“', '”')):
        return False
    if re.search(r"\b(?:si|cuando|quizas|tal vez|supongamos|manana|despues|luego|mas tarde|todavia no|aun no|mejor no|no[, ]+espera)\b", value):
        return False
    if re.search(r"\bno\s+(?:(?:lo|la)\s+)?(?:hagas|cambies|corrijas|actualices|modifiques)\b", value):
        return False
    # «¿Puedes decirme cómo cambiar ...?» pide información. El infinitivo
    # de modificación debe seguir directamente a la petición, sin intermediarios.
    return bool(re.match(
        r"^¿?\s*(?:por favor[, ]+)?(?:me )?(?:puedes|podrias)\s+"
        r"(?:por favor\s+)?(?:cambiar|corregir|actualizar|agregar|incorporar|anotar|modificar)\b",
        value))


def saved_data_query(text):
    return is_query_only(text) and not polite_update_requested(text)


def priority_update_statement(text):
    """Reconocer una asignación cortés explícita, no una mención de prioridad.

    Solo el primer pedido «cambiar/corregir ... prioridad a/por ...» se evalúa
    como declaración. Las preguntas informativas posteriores siguen excluidas
    por el extractor existente, que también resuelve negaciones/alternativas.
    """
    if not polite_update_requested(text):
        return None
    first = text.split("?", 1)[0].lstrip("¿ ")
    value = normalized(first)
    if re.match(
        r"^(?:por favor[, ]+)?(?:me )?(?:puedes|podrias)\s+(?:por favor\s+)?"
        r"(?:cambiar|corregir|actualizar|agregar|incorporar|anotar|modificar)\s+"
        r"(?:la )?prioridad\s+(?:a|por)\s+(?:baja|media|alta|critica|low|medium|high|critical)\b", value):
        return first
    return None


def explanation_requested(text, *, allow_following_context=False):
    """Definición aislada o seguida de una continuación sin hechos ni órdenes.

    allow_following_context solo sirve para redactar ayuda después de que el
    procesador interprete el mensaje completo; nunca para saltarse ese proceso.
    """
    value = normalized(text).strip(" ¿?!.\t\n")
    parts = re.split(r"[?!.]\s*", value, maxsplit=1)
    if len(parts) == 2:
        head, tail = parts
        continuation = re.fullmatch(
            r"(?:despues|luego|mas tarde) (?:seguimos|continuamos|retomamos)"
            r"(?: (?:con )?(?:el|la|este|esta|mi) (?:reporte|borrador|incidente|incidencia))?",
            tail)
        if not allow_following_context and not continuation:
            return None
        value = head
    match = re.fullmatch(
        r"(?:por favor[, ]+)?(?:que significa|que es|a que te refieres con|"
        r"(?:me )?(?:puedes|podrias) explicar(?:me)?|explicame|me explicas)\s+"
        r"(?:(?:el|la)\s+)?(?P<field>prioridad(?: (?P<priority>baja|media|alta|critica))?|impacto operacional)"
        r"(?:[, ]+por favor)?",
        value)
    if not match:
        return None
    if match["field"].startswith("prioridad"):
        priority = {"baja": "low", "media": "medium", "alta": "high", "critica": "critical"}.get(match["priority"])
        return "priority", priority
    return "operational_impact", None
