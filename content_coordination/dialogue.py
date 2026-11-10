"""Actos contextuales; la interpretación nunca sustituye la autorización ORM.

Reutiliza los vetos a citas, condiciones y terceros del agente de incidencias.
La tolerancia tipográfica se limita a una palabra de autorización larga, completa.
"""
import re

from incidents.services.conversation_content import direct_action_text, natural_confirmation
from .services import clean


def one_edit(a, b):
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        different = [i for i in range(len(a)) if a[i] != b[i]]
        return len(different) == 1 or (len(different) == 2 and different[1] == different[0] + 1
            and a[different[0]] == b[different[1]] and a[different[1]] == b[different[0]])
    short, long = sorted((a, b), key=len)
    return any(long[:i] + long[i + 1:] == short for i in range(len(long)))


def response_act(text, pending=None, references=()):
    """Un sí contextual no autoriza una operación diferente ni una petición nueva."""
    direct = direct_action_text(text)
    if direct is None:
        return None
    value = re.sub(r'[^\w\s]', '', direct).strip()
    if value in {'no', 'no gracias', 'mejor no', 'no confirmo'}:
        return 'decline' if pending else None
    if value in {'todavia no', 'aun no', 'un momento', 'espera', 'no lo envies todavia'}:
        return 'hold' if pending else None
    if value in {'si', 'si por favor', 'claro', 'dale', 'de acuerdo', 'adelante'}:
        return 'confirm' if pending else None
    if value in {'si preguntale', 'preguntale', 'si consultale'}:
        return 'confirm' if pending and pending.get('action') == 'ask' else None
    if pending and pending.get('action') == 'ask' and natural_confirmation(
            re.sub(r'\b(?:preguntale|consultale)\b', 'confirmo', clean(text)), references):
        return 'confirm'
    if value in {'si ingresala', 'ingresala'}:
        return 'confirm' if pending and pending.get('action') == 'create' else None
    if natural_confirmation(text, references):
        return 'confirm'
    # Nunca aproximar sí/no, frases largas, números, preguntas ni negaciones.
    if pending and re.fullmatch(r'[a-z]{6,10}', value) and any(one_edit(value, word) for word in ('confirmo', 'autorizo')):
        return 'confirm'
    return None


def contact_intent(text):
    """Respaldo gramatical al contrato ask/query, sin ejecutar el contacto."""
    value = clean(text)
    verb = r'(?:pregunt\w*|consult\w*|contact\w*|envi\w*|mand\w*)'
    if re.search(r'\bno\s+(?:\w+\s+){0,3}' + verb + r'\b', value):
        return False
    if re.search(r'\b(?:dijo|dice|dijeron|cuando|quizas|tal vez|si yo)\b', value) or any(c in text for c in ('«', '»', '“', '”', '"')):
        return None
    return True if re.search(r'\b(?:pregunta(?:le|les|rle|rles)|preguntes|consultale|contacta(?:r)?|(?:envia|manda)(?:le)?\s+(?:un\s+)?mensaje)\b', value) else None
