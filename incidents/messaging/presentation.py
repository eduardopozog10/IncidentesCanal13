"""Presentación común. El texto canónico y las confirmaciones no se reescriben."""
import html
import re

LABELS = ('Estado', 'Versión', 'Edición', 'Diseño', 'Revisión', 'Responsable', 'Etapa',
          'Siguiente paso', 'Último evento', 'Último avance', 'Última aprobada',
          'Versiones aprobadas', 'Área encargada', 'Área solicitante', 'Plazo', 'Prioridad')
LABELS += ('Área','Ubicación/contexto','Familia','Equipo/servicio','Síntoma','Descripción','Impacto operacional')
LABELS += ('Solicitud','Responsable de diseño','Responsable de revisión','Responsable de Edición',
           'Avance de diseño','Estimación informada','Tiempo estimado restante','Pregunta','Diseñador')
LABELS += ('Nueva versión','Detalle')
KINDS = {'success':'✅','request':'📋','assignment':'📩','progress':'⏱️',
         'confirmation':'❓','warning':'⚠️','emission':'🎬','followup':'🔄'}
CODE = re.compile(r'^\s*(?:CT|SC|TVT)-\d+\b')
EMOJI = re.compile(r'(?:[\U0001F1E6-\U0001F1FF]{2}|[0-9#*]\ufe0f?\u20e3|[\U0001F300-\U0001FAFF\u2600-\u27bf](?:[\ufe0f\U0001F3FB-\U0001F3FF])*(?:\u200d[\U0001F300-\U0001FAFF\u2600-\u27bf](?:[\ufe0f\U0001F3FB-\U0001F3FF])*)*)')


def display_text(text):
    # Un solo emoji decorativo al inicio de línea. Los símbolos dentro de datos
    # literales se conservan, para no cambiar nombres ni hechos del reportante.
    seen = False
    lines = []
    for line in text.split('\n'):
        match = EMOJI.match(line.lstrip())
        if match:
            if seen:
                line = line[:len(line)-len(line.lstrip())] + line.lstrip()[match.end():].lstrip()
            seen = True
        lines.append(line)
    result = '\n'.join(lines)
    # Compatibilidad visual con los mensajes canónicos de incidencias. Solo
    # plantillas inequívocas del backend; nunca buscar éxitos dentro de los datos.
    first = result.split('\n',1)[0]
    kind = None
    if not EMOJI.match(first.lstrip()):
        if re.fullmatch(r'La incidencia (?:SC|TVT)-\d+ fue registrada correctamente\.', first):kind='success'
        elif first == 'Antes de crear el incidente, revisa los datos:':kind='confirmation'
        elif CODE.match(first):kind='request'
        elif first.startswith(('Tu cuenta de Telegram no está autorizada','No fue posible procesar el reporte','El registro de la incidencia todavía no está confirmado')):kind='warning'
    if kind and utf16_length(result) + utf16_length(KINDS[kind]+' ') <= 4096:
        result = KINDS[kind] + ' ' + result
    return result


def utf16_length(text):
    return len(text.encode('utf-16-le')) // 2


def telegram_html(text):
    """Solo genera <b>; todos los datos, incluido HTML del usuario/IA, se escapan."""
    lines = []
    plain = display_text(text)
    for number, line in enumerate(plain.split('\n')):
        stripped = line.strip()
        prefix_emoji = EMOJI.match(stripped)
        semantic = stripped[prefix_emoji.end():].lstrip() if prefix_emoji else stripped
        semantic = semantic.removeprefix('• ').strip()
        label = next((name for name in LABELS if semantic.startswith(name + ':')), None)
        if not label and re.match(r'^Versión \d+\.\d+:',semantic):
            label = semantic.split(':',1)[0]
        if CODE.match(semantic) or (prefix_emoji and number == 0):
            rendered = '<b>' + html.escape(line, quote=False) + '</b>'
        elif label:
            prefix, rest = line.split(':', 1)
            rendered = '<b>' + html.escape(prefix + ':', quote=False) + '</b>' + html.escape(rest, quote=False)
            if label in {'Solicitud','Versión','Nueva versión','Estado','Responsable','Responsable de diseño',
                         'Responsable de revisión','Responsable de Edición','Diseño','Edición','Revisión'} and rest.strip():
                rendered = '<b>' + html.escape(prefix + ':',quote=False) + '</b> <b>' + html.escape(rest.strip(),quote=False) + '</b>'
        else:
            rendered = html.escape(line, quote=False)
        lines.append(rendered)
    return '\n'.join(lines)


def split_text(text, limit=4000):
    parts = []
    remaining = text
    while remaining:
        units = end = 0
        for char in remaining:
            size = 2 if ord(char)>0xFFFF else 1
            if units + size > limit:break
            units += size; end += 1
        if end < len(remaining):
            boundary = remaining.rfind('\n',0,end)
            if boundary < end//2:boundary = remaining.rfind(' ',0,end)
            if boundary >= end//2:end = boundary
        part = remaining[:end].strip()
        if part:parts.append(part)
        remaining = remaining[end:].lstrip()
    return parts


def message_blocks(title, fields=(), next_step=None, *, kind=None, body=None):
    """Composición breve, transportable a texto plano, web y Telegram."""
    blocks = [(KINDS[kind]+' ' if kind else '') + title]
    if fields:
        blocks.append('\n'.join(f'{label}: {value}' for label, value in fields if value))
    if body:
        blocks.append(body)
    if next_step:
        blocks.append('Siguiente paso: ' + next_step)
    return '\n\n'.join(blocks)
