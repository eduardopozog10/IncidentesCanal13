"""Operaciones extractivas sobre texto: no diagnostican ni completan hechos."""
import re
import unicodedata


def normalized(text):
    return " ".join("".join(c for c in unicodedata.normalize("NFD", str(text).lower())
                            if unicodedata.category(c) != "Mn").split())


def join_description(*parts):
    sentences, seen = [], set()
    for part in parts:
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", str(part or "").strip()):
            sentence = sentence.strip()
            key = re.sub(r"[^\w\s]", "", normalized(sentence))
            if not key or key in seen:
                continue
            sentence = sentence[0].upper() + sentence[1:]
            sentences.append(sentence if sentence.endswith((".", "!", "?")) else sentence + ".")
            seen.add(key)
    return " ".join(sentences)


MODALITY = re.compile(r"\b(?:sospech\w*|podria\w*|quizas?|tal vez|hipotesis|creemos|parece|"
                      r"posible|puede|podemos|podriamos|disponible|permitiria|permitira|sin confirmar|no (?:esta|hemos|sabemos)|"
                      r"falta revisar|todavia no|aun no)\b")
TIME = re.compile(r"\b(?P<lead>desde hace|hace|llev(?:a|amos|an)(?: ahora)?|han (?:pasado|transcurrido)|van|"
                  r"(?:duraci[oó]n|antig[uü]edad)(?: (?:informada|reportada|actual))?\s*(?::|es de|es|a|de)?)\s+"
                  r"(?P<duration>(?:(?:aproximadamente|como|alrededor de|cerca de|unos?)\s+)?"
                  r"(?:\d+(?:[.,]\d+)?|una?|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|media)"
                  r"\s+(?:minutos?|horas?|d[ií]as?|semanas?|meses?))\b", re.IGNORECASE)


def _duration_matches(text):
    """Duración afirmada de la falla; no edad de una conexión/reinicio/espera."""
    for match in TIME.finditer(text):
        before = re.split(r'[.!?;,\n]|\s+(?:y|pero)\s+', text[:match.start()], flags=re.IGNORECASE)[-1]
        after = re.split(r'[.!?;,\n]|\s+(?:y|pero)\s+', text[match.end():], flags=re.IGNORECASE)[0]
        prefix, clause = normalized(before), normalized(before + ' ' + after)
        if (re.search(r'\b(?:no|ni)(?:\s+comenzo)?\s*$', prefix)
                or re.search(r'\b(?:podriamos|quizas|tal vez)\b', prefix)
                or re.search(r'\bsi\b', before, re.IGNORECASE)):
            continue
        if re.search(r'\b(?:conect\w*|reinici\w*|enchuf\w*|esperando|llego el tecnico)\b', clause) and not re.search(r'\b(?:falla|problema|fallando|perdiendo)\b', clause):
            continue
        yield match


def time_fact(text):
    matches = [m.group() for m in _duration_matches(text)]
    return matches[-1] if matches else None


def replace_time(description, fact):
    update = TIME.search(fact)
    if update is None:
        return description
    result = description
    for match in reversed(list(_duration_matches(description))):
        result = result[:match.start()] + match['lead'] + ' ' + update['duration'] + result[match.end():]
    # Un apéndice que SOLO vuelve a decir la duración no añade un hecho.
    # No borrar cláusulas con equipos, síntomas, fechas u otros antecedentes.
    kept, seen, removed = [], False, False
    temporal_words = set('ya ahora con el la los las este esta del de un una problema falla tiempo transcurrido es'.split())
    for part in sentences(result):
        matches = list(_duration_matches(part))
        residue = part
        for match in reversed(matches): residue = residue[:match.start()] + residue[match.end():]
        if seen and matches and set(re.findall(r'\w+', normalized(residue))) <= temporal_words:
            removed = True
            continue
        kept.append(part)
        seen = seen or bool(matches)
    return join_description(*kept) if removed else result


def preserve_modality(candidate, evidence):
    """Si una paráfrasis pierde modalidad, conservar su cita, sin inferirla."""
    if candidate and evidence:
        source, result = normalized(evidence), normalized(candidate)
        if is_diagnostic_uncertainty(evidence) and not is_diagnostic_uncertainty(candidate):
            return evidence
        if MODALITY.search(source) and not MODALITY.search(result):
            return evidence
        for condition in (r"\bparcial\w*\b", r"\bdisponible\b", r"\b(?:sin|no (?:hay|tenemos|existe|disponemos))\s+(?:un[oa]?\s+)?(?:respaldo|alternativa|otra camara|otro equipo)\b"):
            if re.search(condition, source) and not re.search(condition, result): return evidence
        in_use = r"\b(?:(?:estamos|seguimos|ya)\s+(?:usando|utilizando|trabajando)|en uso)\b"
        resumed = (backup_transition(evidence)
            and re.search(r'\b(?:retomamos|reanudamos|pudimos (?:retomar|continuar)|se (?:retoman|reanudan))\b', source)
            and not re.search(r'\bno (?:pudimos |logramos )?(?:retomar|continuar|reanudar)\b', source))
        if re.search(in_use, result) and not (re.search(in_use, source) or resumed): return evidence
    return candidate


def preserve_description_context(previous, current, message):
    if not current:
        return current
    facts = []
    prior_time = time_fact(previous or "")
    if prior_time and not time_fact(current) and not time_fact(message):
        facts.append(f"Antigüedad informada: {prior_time}")
    for sentence in sentences(previous):
        if not is_diagnostic_uncertainty(sentence):
            continue
        # Confirmar prioridad u otro dato no confirma la causa de una falla.
        resolved = re.search(r'\b(?:confirmamos|descartamos|diagnostico confirmado|causa confirmada|ya no sospechamos)\b', normalized(message))
        subjects = set(re.findall(r'\w{4,}', normalized(sentence))) - set('sospechamos creemos podria posible hipotesis confirmar falta revisar esta este desde hace todavia'.split())
        if not (resolved and subjects.intersection(re.findall(r'\w{4,}', normalized(message)))):
            facts.append(sentence)
    # Una corrección de otro hecho no puede eliminar el respaldo descrito.
    if not backup_transition(message):
        facts.extend(sentence for sentence in fact_clauses(previous)
                     if backup_reference(sentence) and not any(
                         normalized(sentence).rstrip('.') in normalized(item)
                         for item in sentences(current)))
    return join_description(current, *facts) if facts else current


def is_diagnostic_uncertainty(text):
    text = normalized(text)
    return bool(re.search(r"\b(?:sospech\w*|hipotesis|creemos|sin confirmar|falta revisar)\b", text)
                or re.search(r"\b(?:quizas?|tal vez|podria ser)\b.*\b(?:causa|cable|senal|monitor|conector|alimentacion)\b", text))


def unknown_area_statement(text):
    text = normalized(text)
    return "area" in text and unknown_information(text)


def unknown_information(text):
    return bool(re.search(r"\b(?:no (?:lo )?se(?=\s*(?:$|[.,!?]|(?:a|el|la|si|cual|que|como|donde|nada|muy|exactamente)\b))|no (?:lo )?(?:cacho|conozco)|desconozco|sin saber|no tengo (?:idea|claro)|no estoy segur[oa])\b", normalized(text)))


def sentences(text):
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", str(text or '')) if part.strip()]


def fact_clauses(text):
    # Separar solo límites explícitos de cláusulas. No resumir ni diagnosticar.
    return [part.strip() for sentence in sentences(text)
            for part in re.split(r';\s*|\s+(?:y|pero|mientras que)\s+(?=(?:tenemos|hay|el |la |otro |otra |estamos|el original|la original))', sentence, flags=re.IGNORECASE)
            if part.strip()]


def backup_reference(text):
    return bool(re.search(r"\b(?:respaldo|alternativ[oa]|otr[oa] (?:camara|monitor|equipo|consola))\b", normalized(text)))


def backup_transition(text):
    """Acción reportada, nunca una posibilidad o instrucción a otro usuario."""
    value = normalized(text)
    if not backup_reference(value) or '?' in value:
        return False
    if re.search(r"\b(?:podemos|podriamos|podria|quizas|si conectamos|cuando conectemos|sin conectar|no (?:hemos|conectamos|estamos))\b", value):
        return False
    return bool(re.search(r"\b(?:conectamos|conectado|enchufamos|activamos|estamos (?:usando|utilizando)|empezamos a (?:usar|utilizar)|dejamos de usar|ya no (?:funciona|sirve)|fallo tambien)\b", value)
                or re.search(r'\bperdimos (?:el |la |un |una )?(?:respaldo|(?:monitor|camara|equipo) (?:de respaldo|alternativ[oa]))\b', value))


def observable_intermittence(text):
    value = normalized(text)
    if '?' in value or unknown_information(value) or is_diagnostic_uncertainty(value):
        return None
    if not (re.search(r'\b(?:corta|cortes|negro|pierde|perdida)\b', value)
            and re.search(r'\b(?:vuelve|regresa|retorna)\b', value)):
        return None
    for subject in ('imagen', 'sonido', 'audio', 'senal'):
        if re.search(rf'\b{subject}\b', value):
            return 'Pérdida intermitente de ' + {'senal':'señal'}.get(subject, subject)
    return None


def operational_description(previous, evidence):
    """Sustituir estado actual, conservar síntoma, tiempo e hipótesis originales."""
    kept, history = [], []
    for sentence in fact_clauses(previous):
        if backup_reference(sentence):
            # La disponibilidad anterior está superada por el nuevo estado.
            # Una hipótesis o antigüedad en la misma frase no se descarta.
            if is_diagnostic_uncertainty(sentence) or time_fact(sentence):
                history.append('Antecedente: ' + sentence[0].lower() + sentence[1:])
        elif re.search(r"\b(?:interrumpid[oa]|detenid[oa]|no (?:podemos|se puede) (?:seguir|continuar))\b", normalized(sentence)):
            history.append('Antes de la actualización: ' + sentence[0].lower() + sentence[1:])
        else:
            kept.append(sentence)
    return join_description(*kept, *history, evidence)


def rewrite_requested(text):
    if re.search(r'\bno (?:cambies|modifiques|toques|reescribas) (?:la )?descripcion\b', normalized(text)):
        return False
    return bool(re.search(r"\b(?:reescrib\w*|reformula\w*|ordena\w*|redacta\w*|resum\w*|elimina\w* repeticiones|quita\w* repeticiones)\b", normalized(text))
                and re.search(r"\b(?:descripcion|redaccion|texto|repeticiones|hechos)\b", normalized(text)))


def faithful_rewrite(previous, clauses):
    """Reordenar citas completas: no aceptar paráfrasis ni omitir hechos únicos."""
    original = sentences(previous)
    key = lambda value: normalized(value).strip(' .!?')
    originals = {key(value) for value in original}
    if not isinstance(clauses, list) or not clauses or any(not isinstance(value, str) or key(value) not in originals for value in clauses):
        return None
    if {key(value) for value in clauses} != originals:
        return None
    return join_description(*clauses)


def direct_action_text(text):
    """Veto común a órdenes hipotéticas, citadas, diferidas o de terceros."""
    value = normalized(text)
    if any(mark in value for mark in ('?', '¿', '«', '»', '"', "'", '“', '”')):
        return None
    if re.search(r"\b(?:dijo|dice|dijeron|companer[oa]|si digo|si todo|si esta|si puedes|cuando|despues|luego|manana|tal vez|quizas|creo|excepto|aunque|pero|primero|no se si)\b", value):
        return None
    return value


def natural_cancel(text):
    value = direct_action_text(text)
    if not value or re.search(r"\bno\s+(?:lo\s+)?(?:cancel\w*|descart\w*|olvid\w*)\b", value):
        return False
    if re.search(r'\b(?:cambia\w*|corrig\w*|actualiza\w*)\b', value):
        return False
    object_ = r"(?:borrador|reporte|incidente|incidencia)"
    directive = re.search(rf"\b(?:cancela|descarta|olvida)(?:r|lo)?\b.*\b{object_}\b", value)
    if directive:
        prefix = value[:directive.start()]
        # No confundir una explicación/consulta sobre cancelar con la orden.
        words = set(re.findall(r'\w+', prefix))
        if words <= set('oye ya por favor puedes podrias quiero necesito solicito mejor ahora entonces'.split()):
            return True
    return bool(re.search(rf"\bya no quiero (?:seguir|continuar)(?: con (?:este|el|mi) {object_})?\s*[.!]*$", value)
                or re.fullmatch(r"mejor no (?:lo )?(?:registremos|guardemos|creemos)[.!]*", value))


def natural_confirmation(text, references=()):
    value = direct_action_text(text)
    if not value or re.search(r"\b(?:no|todavia|aun|cambia\w*|corrig\w*|corrij\w*|actualiza\w*|falta\w*|errone\w*|mal|incorrect\w*)\b", value):
        return False
    # Verbos de autorización + objeto. El resto solo puede ser cortesía,
    # afirmación o una referencia al equipo/ubicación del borrador actual.
    action = r"\b(?:(?:confirmo|autorizo)(?:\s+(?:la\s+)?(?:creacion|registrar|crear|guardar))?|(?:registra|crea|guarda|confirma|registrar|crear|guardar)(?:lo|la)?|crearlo|registrarlo|guardarlo)\b"
    if not re.search(action, value):
        return False
    value = re.sub(action, ' ', value)
    allowed = set('si ya ahora po jajaja jaja ja dale nomas gracias por favor perfecto correcto correcta correctos correcto todo todos esta estan bien puedes puede podes el la los las un una este esta estos estas del de al a lo le reporte incidente incidencia borrador datos informacion para proceda proceder'.split())
    for reference in references:
        # Quitar referencias completas; una bolsa de palabras permitiría
        # intercambiar números de equipo y estudio de otro reporte.
        reference = normalized(reference or '')
        if reference:
            value = re.sub(r'(?<!\w)' + re.escape(reference) + r'(?!\w)', ' ', value)
    words = re.findall(r'\w+', value)
    return all(word in allowed or re.fullmatch(r'(?:ja){2,}', word) for word in words)


def query_fields(text):
    if text.startswith("[Secuencia ordenada"):
        return None
    text = normalized(text)
    questions = re.findall(r"([^.!?¿]+)\?", text)
    if questions:
        text = " ".join(questions)
    else:
        request = re.search(r"\b(?:recuerdame|muestrame|dame|dime|quiero consultar|puedes (?:mostrarme|darme|decirme))\b", text)
        if request:
            # El alcance de salida no incluye instrucciones previas como
            # «no cambies el impacto». También puede pedirse sin interrogación.
            text = text[request.start():]
        elif not re.search(r"\b(?:resumen|recuerdas|recordar|que informacion|que datos|que teniamos|solicito)\b", text):
            return None
    # Una restricción de presentación no solicita el campo que menciona:
    # «solo el impacto, sin repetir el respaldo» pide únicamente impacto.
    text = re.sub(r'\b(?:sin (?:volver a )?(?:repetir|mostrar|incluir|detallar)|no (?:me )?(?:repitas|muestres|incluyas|detalles))\b[^,.;!?]*', '', text)
    if re.search(r"\b(?:resumen|toda la informacion|todos los datos|que teniamos anotado)\b", text) and not re.search(r"\bno\b.*\b(?:resumen|reporte|todos)\b", text):
        return {"summary"}
    fields = {'missing'} if re.search(r"\b(?:falta|faltan|pendiente|pendientes)\b", text) else set()
    for field, pattern in {
        "equipment": r"\b(?:equipo|camara|monitor|consola|servicio|microfono|router)\b",
        "location_context": r"\b(?:ubicacion|estudio|lugar|donde)\b",
        "failure_type": r"\b(?:sintoma|problema|imagen|falla)\b",
        "description": r"\bdescripcion\b",
        "operational_impact": r"\b(?:impacto|afecta|operaciones)\b",
        "priority": r"\bprioridad\b", "area": r"\barea\b",
        "asset_category": r"\b(?:familia|clasificacion|categoria)\b",
        "time": r"\b(?:tiempo|antiguedad|inicio|empezo|desde cuando|cuanto lleva)\b",
        "backup": r"\b(?:respaldo|alternativ[oa]|(?:otra|otro) (?:camara|monitor|equipo)|estamos usando)\b",
    }.items():
        if re.search(pattern, text): fields.add(field)
    # «Cuánto empezó la falla» pregunta por tiempo, no por el síntoma.
    if 'time' in fields and not re.search(r"\b(?:que (?:falla|problema)|cual.*sintoma)\b", text): fields.discard('failure_type')
    if 'backup' in fields and not re.search(r"\b(?:que (?:equipo|camara|monitor)|cual.*(?:equipo|camara|monitor))\b", text): fields.discard('equipment')
    return fields or None


def report_preparation_requested(text):
    """Ayuda para preparar el reporte, distinta de consultar un campo guardado."""
    value = normalized(text)
    requests = re.findall(r'[^.!?¿]+(?:\?|$)', value)
    return any(re.search(r'\b(?:ayud\w*|apoy\w*|asist\w*|gui\w*)\b', part)
        and re.search(r'\b(?:prepar\w*|arm\w*|complet\w*|redact\w*|dejar)\b', part)
        and re.search(r'\b(?:reporte|incidencia|borrador)\b', part)
        for part in requests)


def is_query_only(text):
    if report_preparation_requested(text): return False
    if rewrite_requested(text): return False
    if not query_fields(text): return False
    if "?" in text:
        remainder = re.sub(r"[¿]?[^.!?¿]*\?", "", normalized(text))
        remainder = re.sub(r"\bno (?:quiero|deseo|necesito|debes)\b[^.!?]*", "", remainder)
        remainder = re.sub(r"\b(?:por favor|solo consultar|sin modificar nada)\b", "", remainder)
        return not re.search(r"\w", remainder)
    value = re.sub(r'\b(?:sin\s+(?:cambiar|modificar|actualizar)|no\s+(?:(?:quiero|debes|necesito)\s+)?(?:cambies|modifiques|actualices|cambiar|modificar|actualizar))\b[^,.;!?]*', '', normalized(text))
    return not re.search(r"\b(?:cambia\w*|corrijo|corrige\w*|actualiza\w*|agrega\w*|incorpora\w*|anota\w*|tenemos|contamos|ademas|comenzo|prioridad es|ubicacion es|equipo es)\b", value) and not time_fact(text)
