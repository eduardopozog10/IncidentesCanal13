"""Último evento confirmado, global o de una versión; los avisos no son avances."""
from django.utils import timezone

LABELS = {'create':'Solicitud registrada','assign':'Diseñador asignado','assign_review':'Revisor asignado',
          'assign_edit':'Editor asignado','start':'Diseño iniciado','progress':'Avance de diseño informado',
          'submit':'enviada a revisión','approve':'aprobada','corrections':'Correcciones abiertas',
          'edit':'incorporada a Edición','receive':'recibida por Switch','emit':'Emisión confirmada', 'cancel':'Solicitud cancelada'}
VERSION_ACTIONS = {'submit','approve','corrections','edit','receive','emit'}


def last_activity(request, *, version=None):
    history = sorted(request.history.all(), key=lambda h:(h.created_at,h.pk), reverse=True)
    for event in history:
        if event.operation not in LABELS:
            continue
        before = {v['number']:v for v in event.before.get('versions', [])}
        after = {v['number']:v for v in event.after.get('versions', [])}
        changed = [number for number, value in after.items() if before.get(number) != value]
        if version:
            if version not in changed:
                # Un avance de diseño se vincula a la versión en trabajo más reciente
                # de ese momento, nunca a otra versión antigua.
                if event.operation != 'progress' or not after or list(after)[-1] != version:
                    continue
            number = version
        else:
            number = changed[-1] if changed else None
        text = LABELS[event.operation]
        if event.operation in VERSION_ACTIONS and number:
            text = (f'Emisión de la versión {number} confirmada' if event.operation == 'emit'
                    else f'Correcciones abiertas en la versión {number}' if event.operation == 'corrections'
                    else f'Versión {number} {text}')
        if event.operation == 'progress':
            old = {t['kind']:t for t in event.before.get('tasks', [])}
            task = next((t for t in event.after.get('tasks', []) if t['kind']=='design'), {})
            if task.get('last_progress') != old.get('design', {}).get('last_progress'):
                text = task.get('last_progress') or text
        date = timezone.localtime(event.created_at)
        return {'text':text, 'at':event.created_at, 'time':date.strftime('%d/%m %H:%M'), 'operation':event.operation}
    return None
