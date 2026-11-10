"""Lectura de etapas y versiones reales compartida por chat y dashboard."""
import re
from datetime import timedelta
from django.conf import settings
from django.utils import timezone
from django.core.exceptions import ValidationError
from .models import ContentRequest, Task
from .services import clean, person, require, visible
from .resolution import eligible_tasks
from .interpretation import versions_in
from .activity import last_activity
from incidents.messaging.presentation import message_blocks
from .access import telegram_allowed


def work_info(r, *, version=None):
    tasks = {t.kind: t for t in r.tasks.all()}
    versions = sorted(r.versions.all(), key=lambda v: v.pk)
    current = next((v for v in versions if v.number == version), None) if version else versions[-1] if versions else None
    approved = [v for v in versions if v.approved_at]
    received = [v for v in versions if v.received_at]
    review = tasks.get('review')
    design = tasks.get('design')
    editing = tasks.get('editing')
    editor = editing.assignee.name if editing and editing.assignee else 'Sin asignar'
    reviewer = review.assignee.name if review and review.assignee else 'Sin asignar'
    if r.state == 'cancelled':
        next_step = 'La solicitud está cancelada.'
    elif current and current.emitted_at:
        next_step = 'Emisión confirmada por ' + current.emitted_by.name + '.'
    elif current and current.received_at:
        next_step = 'Switch recibió el material. Falta confirmar la emisión por separado.'
    elif current and current.edited_at:
        next_step = 'Falta confirmar la recepción de esta versión en Switch.'
    elif current and current.approved_at:
        if editing is None:
            next_step = 'Hay material aprobado, pero falta la tarea de Edición; requiere revisión técnica.'
        elif editor == 'Sin asignar':
            next_step = 'Edición todavía no tiene responsable. Coordinación puede asignar un editor autorizado; un editor autorizado o administrador puede asumir la etapa al registrar la incorporación.'
        else:
            next_step = f'Falta que {editor} incorpore esta versión aprobada a la nota en Edición.'
    elif current and current.state == 'review':
        next_step = ('Falta la aprobación del revisor autorizado ' + reviewer + '.' if reviewer != 'Sin asignar'
                     else 'Primero falta asignar un revisor autorizado; luego debe aprobar esta versión.')
    elif current and current.state == 'working':
        next_step = f'Hay correcciones en la versión {current.number}. Falta entregarla para revisión.'
    elif design and design.state == 'working':
        next_step = 'Falta terminar el diseño y entregar una versión para revisión.'
    elif design and design.assignee_id:
        next_step = 'Falta comenzar el diseño.'
    else:
        next_step = 'Falta asignar un diseñador.'
    return {'tasks': tasks, 'current': current, 'approved': approved[-1] if approved else None,
            'received': received[-1] if received else None, 'reviewer': reviewer, 'editor': editor, 'next': next_step,
            'designer': design.assignee.name if design and design.assignee else 'Sin asignar'}


def describe(r, *, list_approved=False, version=None, editing_only=False, compact=False):
    info = work_info(r, version=version)
    if version and info['current'] is None:
        return f'{r.code} · {r.title}\nNo tiene una versión {version} registrada.'
    fields = [('Estado', r.get_state_display())]
    if editing_only:
        fields.append(('Edición',info['editor']))
    elif not compact:
        fields.extend([('Diseño',info['designer']),('Revisión',info['reviewer']),('Edición',info['editor'])])
    current = info['current']
    if current and not list_approved:
        fields.append((f'Versión {current.number}',current.get_state_display().lower() + '.'))
    if info['approved']:
        if list_approved:
            approved = [v.number for v in r.versions.all() if v.approved_at]
            fields.append(('Versiones aprobadas', ', '.join(approved) + '.'))
        elif not current or info['approved'].pk != current.pk:
            fields.append(('Última aprobada', info['approved'].number + '.'))
    task = info['tasks'].get('design')
    activity = last_activity(r, version=version)
    if activity:
        fields.append(('Último evento',activity['text'] + ' · ' + activity['time']))
    if task and not task.last_progress and not current:
        fields.append(('Avance de diseño','Sin avance informado.'))
    elif task and task.last_progress and (not activity or activity['text'] != task.last_progress):
        fields.append(('Avance de diseño',task.last_progress))
    if task and task.eta_minutes:
        fields.append(('Estimación informada',f'{task.eta_minutes} minutos (informada por el responsable).'))
    next_step = info['next']
    if current and current.emitted_at:
        next_step = None  # No repetir un cierre como si aún fuese un siguiente paso.
    if editing_only and current and current.approved_at and not current.edited_at:
        next_step += ' Después de Edición sigue Switch; la emisión se confirma por separado.'
    return message_blocks(f'{r.code} · {r.title}', fields, next_step,kind='followup')


def query(actor, text):
    value = clean(text)
    scope = visible(actor).prefetch_related('tasks__assignee', 'versions__emitted_by','history').order_by('pk')
    codes = {int(n) for n in re.findall(r'\bCT-0*(\d+)\b', text, re.I)}
    worker = None
    if not codes and re.search(r'\b(como va|como van|que tiene|trabajos de|tareas de|asignad[oa]s? a)\b', value):
        try:
            worker = person(text)
        except ValidationError:
            pass
    own = bool(re.search(r'\b(tengo|mis|mi|debo)\b', value))
    if not codes and worker and worker.pk != actor.pk:
        require(actor, 'coordinator')
        scope = scope.filter(tasks__assignee=worker).distinct()
    elif not codes and (own or worker):
        scope = scope.filter(tasks__assignee=actor).distinct()
    if codes:
        scope = scope.filter(pk__in=codes)
    all_rows = list(scope)
    rows = all_rows
    if worker:
        reference = re.search(r'\b(?:con|sobre)\s+(.+)', value)
        if reference and not codes:
            from .resolution import STOP
            words = set(re.findall(r'\w{4,}', reference[1])) - STOP
            if words:
                rows = [r for r in rows if words & (set(re.findall(r'\w{4,}',clean(r.title))) - STOP)]
    start = 'comenz' in value or 'empez' in value
    if start:
        ids = set(eligible_tasks(worker or actor, 'start').values_list('request_id', flat=True))
        rows = [r for r in rows if r.pk in ids]
    elif 'produccion' in value:
        rows = [r for r in rows if r.state == 'production']
    elif ('revision' in value or 'aprobacion' in value) and not codes:
        rows = [r for r in rows if any(v.state == 'review' for v in r.versions.all())]
    elif 'version' in value and 'aprobada' in value and not codes:
        rows = [r for r in rows if any(v.approved_at for v in r.versions.all())]
    elif 'pendiente' in value and not codes:
        rows = [r for r in rows if r.state not in {'emitted', 'cancelled', 'received'}]
    if not rows:
        if start:
            answer = ('No tienes tareas asignadas.' if not all_rows else 'No tienes tareas pendientes de comenzar.')
            if all_rows:
                answer += '\n\n' + '\n'.join(f'{r.code}: {r.get_state_display()}.' for r in all_rows[:8])
            return answer, None
        return ('No tienes tareas asignadas.' if own and not all_rows else 'No hay trabajos que coincidan con esa consulta.'), None
    refs = versions_in(text)
    version = refs[0] if len(refs) == 1 else None
    answer = '\n\n'.join(describe(r, list_approved='version' in value and 'aprobad' in value and not ('edicion' in value or 'incorporar' in value), version=version,
                                 editing_only=bool(re.search(r'\b(edicion|editor|incorporar)\b',value)), compact=len(rows)>1) for r in rows[:20])
    if len(rows) > 20:
        answer += '\n\nHay más resultados; acota por código o estado.'
    ask = None
    if actor.role in {'coordinator','admin'}:
        stale = timezone.now() - timedelta(minutes=settings.CONTENT_REMINDER_MINUTES)
        tasks = list(Task.objects.filter(request__in=rows,kind='design',assignee__isnull=False,
                     state__in=['pending','working']).exclude(request__state__in=['cancelled','emitted'])
                     .select_related('assignee'))
        tasks = [t for t in tasks if (not worker or t.assignee_id == worker.pk)
                 and (not t.last_progress or not t.progress_at or t.progress_at <= stale)]
        workers = {t.assignee_id for t in tasks}
        if len(workers) == 1 and tasks[0].assignee_id != actor.pk and telegram_allowed(tasks[0].assignee):
            ask = tasks[0].assignee
            ask.query_task_ids = [t.pk for t in tasks]
    return answer[:3700], ask
