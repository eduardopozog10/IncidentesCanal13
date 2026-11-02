"""Validación del plan semántico. Sin red, SQL ni autoridad de escritura."""
import re
from .conversation_content import normalized, join_description, preserve_modality, direct_action_text, time_fact, MODALITY

ACTIONS = {'update', 'query', 'help', 'rewrite', 'confirm', 'cancel', 'conversation'}
FACETS = {'consequence', 'continuity', 'backup', 'original_fault'}


def render_operational_facts(facts):
    """Omitir solo una conexión simple ya expresada; conservar hechos fuente."""
    texts = {name:facts.get(name) for name in ('consequence','continuity','backup','original_fault')}
    backup = normalized(texts.get('backup') or '').rstrip('.')
    # No aplicar similitud difusa: números, tiempo, negaciones, modalidad y
    # calificadores adicionales deben permanecer visibles.
    connection = re.fullmatch(r'(?:(?:ya|recien)\s+)?(?:conectamos|hemos conectado|se conecto)\s+(.+)', backup)
    if connection and not MODALITY.search(backup) and not time_fact(backup):
        for other in (texts.get('consequence'), texts.get('continuity')):
            value = normalized(other or '').rstrip('.')
            if not MODALITY.search(value) and re.search(r'\b(?:conectar|conectamos|conectado)\s+' + re.escape(connection[1]) + r'(?:[.,;]|$)', value):
                texts['backup'] = None
                break
    # La reanudación explícita CON respaldo también cubre una conexión
    # simple del mismo respaldo genérico. No omitir IDs ni calificadores.
    bare_backup = r'(?:la |el )?(?:camara|equipo|monitor|consola) de respaldo'
    simple_connection = bool(re.fullmatch(bare_backup + r' conectad[oa]', backup)
        or re.fullmatch(r'(?:' + bare_backup + r'|(?:el )?respaldo)(?: esta| está)? en uso', backup)
        or (connection and re.fullmatch(bare_backup, connection[1])))
    if simple_connection and not MODALITY.search(backup) and not time_fact(backup):
        for facet in ('consequence','continuity'):
            value = normalized(texts.get(facet) or '')
            if (facts.get('_states',{}).get(facet) in {'resumed','partial'}
                    and not MODALITY.search(value)
                    and re.search(r'\b(?:con|usando|utilizando|mediante) (?:el |la )?(?:respaldo|(?:camara|equipo|monitor|consola) de respaldo)\b',value)):
                texts['backup'] = None
                break
    return join_description(*texts.values())


def actions(extraction):
    return set(getattr(extraction, 'dialogue_actions', []) or []) & ACTIONS


def fact_updates(extraction, previous, text):
    evidence = getattr(extraction, 'field_evidence', None) or {}
    return any(quote and quote in text and getattr(extraction, name, None)
               and getattr(extraction, name) != previous.get(name)
               for name, quote in evidence.items()) or bool(valid_operational_updates(extraction, text))


def valid_operational_updates(extraction, text):
    updates = getattr(extraction, 'operational_updates', []) or []
    return [item for item in updates if isinstance(item, dict) and item.get('facet') in FACETS
            and isinstance(item.get('text'), str) and item['text'].strip()
            and isinstance(item.get('evidence'), str) and item['evidence'].strip()
            and item['evidence'] in text
            and not (item['facet']=='original_fault' and time_fact(item['text'])
                and not re.search(r'\b(?:sigue|continua|reparad\w*|resuelt\w*|fallando|averiad\w*|funciona|persiste)\b', normalized(item['text'])))]


def merge_operational_facts(previous, extraction, text):
    updates = valid_operational_updates(extraction, text)
    if not updates:
        return None
    facts = dict(previous.get('_operational_facts') or {})
    if not facts and previous.get('operational_impact'):
        facts['consequence'] = previous['operational_impact']
    states = dict(facts.get('_states') or {})
    history = list(facts.get('_history') or [])
    explicit_facets = {item['facet'] for item in updates}
    for item in updates:
        old = facts.get(item['facet'])
        if old and old != item['text']:
            history.append({'facet':item['facet'],'text':old})
        facts[item['facet']] = preserve_modality(item['text'].strip(), item['evidence'])
        state = item.get('state')
        # Una posibilidad no autoriza sustituir el estado operacional actual.
        asserted = not re.search(r'\b(?:podemos|podriamos|podria|quizas|tal vez|si conectamos|cuando conectemos|todavia no|aun no)\b', normalized(item['evidence']))
        if state and state != 'none' and asserted:
            states[item['facet']] = state
            if item['facet']=='continuity' and state in {'resumed','partial','stopped'}:
                if 'consequence' not in explicit_facets:
                    prior = facts.get('consequence')
                    if prior and prior != item['text']: history.append({'facet':'consequence','text':prior})
                    facts['consequence'] = facts['continuity']
                    states['consequence'] = state
                related = item.get('backup_state')
                if related in {'in_use','unavailable','available'} and 'backup' not in explicit_facets:
                    prior = facts.get('backup')
                    if prior and prior != item['text']: history.append({'facet':'backup','text':prior})
                    facts['backup'] = facts['continuity']
                    states['backup'] = related
    if states: facts['_states'] = states
    if history: facts['_history'] = history[-8:]
    return facts, render_operational_facts(facts)


def authorized(extraction, text, draft, kind):
    data = getattr(extraction, 'authorization', None)
    if not isinstance(data, dict) or data.get('kind') != kind or kind not in actions(extraction):
        return False
    if data.get('direct') is not True or any(data.get(name) is not False for name in ('conditional','delayed','third_party','requires_changes')):
        return False
    quote = data.get('evidence')
    if not isinstance(quote, str) or not quote.strip() or quote not in text or direct_action_text(text) is None:
        return False
    value = normalized(text)
    if kind == 'confirm':
        if not re.search(r'\b(?:confirmo|autorizo|ingresa(?:lo)?|registra(?:lo)?|crea(?:lo)?|guarda(?:lo)?|procede)\b', normalized(quote)):
            return False
        if not re.search(r'\b(?:reporte|incidencia|incidente|borrador|datos|informacion|registr\w*|ingres\w*|crea\w*|guarda\w*)\b', value):
            return False
        if re.search(r'\b(?:no|nunca|todavia|aun|cambia\w*|corrig\w*|corrij\w*|actualiza\w*|excepto|falta\w*)\b', value):
            return False
        if re.search(r'\b(?:pregunt\w*|si confirmo|si autorizo|que pasa|podria)\b', value):
            return False
        if actions(extraction) & {'update','rewrite','cancel'} or fact_updates(extraction, draft.extracted_data, text):
            return False
    elif re.search(r'\bno\s+(?:\w+\s+){0,3}(?:cancel\w*|descart\w*|olvid\w*)\b', value):
        return False
    for field in ('equipment','location_context'):
        reference = data.get(field)
        if reference and (normalized(reference) != normalized(draft.extracted_data.get(field) or '') or normalized(reference) not in value):
            return False
    # El modelo no puede omitir una referencia a otro equipo/estudio para
    # autorizar el actual. Comparar expresiones completas, sin permutar IDs.
    context = ' '.join(normalized(draft.extracted_data.get(name) or '') for name in ('equipment','location_context'))
    for reference in re.findall(r'\b(?:camara|monitor|microfono|consola|router|equipo|estudio|movil|sala)\s+(?:\d+|[a-z])\b', value):
        if reference not in context:
            return False
    return True
