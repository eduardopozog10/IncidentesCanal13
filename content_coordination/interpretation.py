"""Referencias literales y límites entre consulta, Edición y otras etapas."""
import re
from .services import clean
from .dialogue import contact_intent
from incidents.services.conversation_content import direct_action_text
from .services import Clarification

NUMBER_WORDS = dict(zip('cero uno una dos tres cuatro cinco seis siete ocho nueve diez'.split(),
                        ['0','1','1','2','3','4','5','6','7','8','9','10']))
START_DECLARATION = re.compile(r'\b(?:voy a (?:comenzar|empezar|iniciar)|comence|empece|inicie|'
                               r'estoy (?:comenzando|empezando|iniciando)|comenzare|empezare|iniciare)\b')


def versions_in(text):
    value = clean(text)
    found = []
    # El punto final es puntuación; un segundo componente decimal no lo es.
    pattern = r'(?<![\w.])(?:v(?:ersion)?\s*)?(\d{1,3})[.,](\d{1,3})(?!\d|[.,]\d)'
    for match in re.finditer(pattern, value):
        found.append((match.start(), match[1] + '.' + match[2]))
    words = '|'.join(NUMBER_WORDS)
    for match in re.finditer(rf'\b({words})\s+punto\s+({words})\b', value):
        found.append((match.start(), NUMBER_WORDS[match[1]] + '.' + NUMBER_WORDS[match[2]]))
    return list(dict.fromkeys(number for _, number in sorted(found)))


def is_question(text):
    value = clean(text).lstrip(' \"«“')
    return value.startswith('¿') or bool(re.match(r'(que|quien|quienes|como|cual|hay alguien)\b', value))


def explicit_action(text, proposed):
    """La semántica inequívoca prevalece sobre una etiqueta de IA incorrecta.

    No autoriza operaciones: todas pasan luego por permisos, estados y confirmación.
    """
    value = clean(text)
    contact = contact_intent(text)
    if proposed == 'ask':
        return 'unknown' if contact is False else 'ask'
    if proposed in {'query', 'unknown'} and contact is True:
        return 'ask'
    if is_question(text):
        return 'query'
    if re.search(r'\bno (?:la |lo )?(?:apruebo|apruebes|aprobar|incorpore|he incorporado|asignes)\b', value):
        return 'unknown'
    if re.search(r'\b(?:no|nunca|si|cuando|podria|podriamos|deberia)\b', value):
        return proposed
    if re.search(r'\b(?:asigna|reasigna|asignar|reasignar)\b', value):
        if re.search(r'\b(?:edicion|editor|editora)\b', value):
            return 'assign_edit'
        if re.search(r'\brevis(?:ion|or|ora)\b', value):
            return 'assign_review'
    if re.search(r'\b(?:incorpore|he incorporado)\b', value) and re.search(r'\b(?:nota|edicion)\b', value):
        return 'edit'
    if re.search(r'\b(?:apruebo|aprueba|aprobar)\b|\baprobada\s+la\s+(?:version|v?\d)|\bla\s+v\d.*\besta aprobada\b', value):
        return 'approve'
    return proposed


def adapt_updates(updates, text):
    """Desambiguar el objeto y la fase antes de resolver permisos o modificar estado.

    La IA aporta actos/evidencia; estos límites verifican la coherencia literal.
    No sustituyen resolve/own_task/stage_task ni sus bloqueos transaccionales.
    """
    result = []
    same_work = len({int(n) for n in re.findall(r'\bCT-0*(\d+)\b', text, re.I)}) <= 1
    combined_start = same_work and START_DECLARATION.search(clean(text)) and not re.search(r'\b(?:switch|version|material)\b',clean(text))
    for original in updates:
        update = {**original, 'data': original['data'].copy()}
        evidence = update['evidence']
        value = clean(evidence)
        proposed = explicit_action(evidence, update['action'])
        receipt = bool(re.search(r'\b(?:recibi|recibido|recibida|llego|llegado)\b', value))
        assignment = receipt and (bool(re.search(r'\b(?:asignaci\w*|tarea|trabajo|encargo)\b', value))
                                 or update['data'].get('receipt_scope') == 'assignment' or combined_start)
        technical = bool(re.search(r'\b(?:switch|recepcion tecnica|material|version)\b', value))
        start = bool(START_DECLARATION.search(value))
        partial = bool(re.search(r'\b(?:llevo la mitad|tengo avanzada|tengo avanzado|me faltan|me queda[n]?)\b', value))
        direct = direct_action_text(evidence) is not None and not re.search(r'\b(?:no|nunca|si me|si la|si el|si todo)\b', value)
        if partial and not start and not receipt:
            # Una negación de finalización o un contraste no niega el avance:
            # conservar el hecho completo, sin autorizar cierres ni versiones.
            probe = re.sub(r'\b(?:pero|aunque)\b', ',', value)
            direct = direct_action_text(probe) is not None and not re.search(
                r'\b(?:si me|si la|si el|si todo|no (?:me )?(?:faltan|queda[n]?|tengo|llevo))\b',value)
        if (start or partial or receipt) and not direct and proposed not in {'query','ask'}:
            raise Clarification('¿Quieres registrar un cambio ahora o estás comentando una posibilidad? No cambié ninguna etapa.')
        if start and receipt and technical:
            raise Clarification('Mencionas inicio de diseño y recepción técnica. Indica qué operación quieres registrar y su CT; revisemos una etapa a la vez.')
        if direct and start and proposed in {'receive','acknowledge','start','unknown','progress'}:
            proposed = 'start'
        elif direct and assignment and not technical and proposed in {'receive','acknowledge','start','unknown'}:
            proposed = 'acknowledge'
        elif direct and partial and proposed in {'submit','receive','start','unknown','progress'} and not technical:
            if proposed != 'progress':update['data']['progress'] = evidence
            proposed = 'progress'
            update['data'].setdefault('progress', evidence)
        elif proposed == 'receive' and not technical:
            raise Clarification('¿Recibiste una asignación para diseñar o una versión en Switch? Indica la etapa; no registré una recepción técnica.')
        if proposed == 'acknowledge' and not (assignment or (receipt and update['data'].get('receipt_scope') == 'assignment')):
            raise Clarification('¿Qué asignación recibiste? Indica su CT; no cambié el estado del diseño.')
        update['action'] = proposed
        if proposed in {'acknowledge','start','progress'}:
            # Una versión inventada por la extracción no convierte el inicio en Switch.
            if not versions_in(evidence):
                update['data'].pop('version', None)
                update['data'].pop('new_version', None)
        result.append(update)
    # Dos cláusulas de acuse/inicio del mismo trabajo describen un solo comienzo.
    codes = {int(n) for n in re.findall(r'\bCT-0*(\d+)\b', text, re.I)}
    if len(result) > 1 and len(codes) <= 1 and {u['action'] for u in result} <= {'acknowledge','start'}:
        action = 'start' if any(u['action']=='start' for u in result) else 'acknowledge'
        return [{'action':action, 'evidence':text, 'data':{}}]
    if len(result) > 1 and any(u['action'] in {'receive','edit','submit'} for u in result):
        raise Clarification('Hay varias operaciones de etapas o versiones. Revisemos una a la vez, con su CT y versión.')
    return result


def version_answer(text):
    """Una aclaración corta aporta datos, no una autorización."""
    value = clean(text)
    value = re.sub(r'\bct-\d+\b', '', value)
    value = re.sub(r'(?<!\w)v?\d{1,3}[.,]\d{1,3}(?!\d)', '', value)
    value = re.sub(r'\b(?:' + '|'.join(NUMBER_WORDS) + r')\s+punto\s+(?:' + '|'.join(NUMBER_WORDS) + r')\b', '', value)
    words = set(re.findall(r'\w+', value))
    return len(versions_in(text)) == 1 and words <= {'la','version','es','de','esa','quiero','aprobar'}


def focused_reference(text, action, context):
    if action not in {'approve','edit','receive','emit','corrections','submit'} or not context.get('focus') or re.search(r'\bCT-\d+\b', text, re.I):
        return text
    value = clean(text)
    value = re.sub(r'v?\d{1,3}[.,]\d{1,3}', '', value)
    generic = set('revise revisé grafica version apruebo aprueba aprobada aprobar esta estoy quiero solicitud solicito registrar aprobacion incorpore incorporado nota edicion lista revision recibi recibida switch emision confirma confirmo punto uno cero dos tres'.split())
    words = set(re.findall(r'\w{4,}', value))
    if words <= generic:
        return f"CT-{context['focus']:06d} " + text
    return text
