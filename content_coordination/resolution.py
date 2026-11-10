"""Resolución autorizada por intención, etapa y referencia literal."""
import re
from django.core.exceptions import PermissionDenied
from .models import Task
from .services import Clarification, clean, visible

STAGES = {'acknowledge':'design', 'start': 'design', 'progress': 'design', 'submit': 'design',
          'assign': 'design', 'assign_review': 'review', 'assign_edit': 'editing', 'approve': 'review',
          'corrections': 'review', 'edit': 'editing', 'receive': 'switch', 'emit': 'switch'}
TASK_STATES = {'acknowledge':['pending','working','review','done'], 'start': ['pending'], 'progress': ['pending', 'working', 'review'],
               'submit': ['pending', 'working'], 'assign': ['pending', 'working'],
               'assign_review': ['pending', 'working'], 'assign_edit': ['pending', 'working'], 'approve': ['pending', 'working'],
               'corrections': ['pending', 'working', 'done'], 'edit': ['pending', 'working'],
               'receive': ['pending', 'working'], 'emit': ['done']}
NO_TASK = {'start': 'Por ahora no tienes tareas pendientes de comenzar.',
           'submit': 'No tienes tareas de diseño pendientes de entregar a revisión.',
           'progress': 'No tienes tareas de diseño activas para actualizar.'}
STOP = set('ya voy vamos puedo comenzar comenzare comenzaré empezar empezare iniciar trabajando trabajar trabajo trabajos tarea tareas con para esta estoy grafica gráfica quiero version lista listo termine terminado revision diseno produccion minutos faltan favor actualiza actualizare actualizar registra empezaré'.split())
STOP.update('recibi recibido recibida asignaron asignada asignado asignacion llego llegado encargo comence empece inicie comenzando empezando iniciando llevo mitad tengo avanzada avanzado aproximadamente revisarlo'.split())


class ChooseTask(Clarification):
    def __init__(self, choices, action, data=None):
        self.choices = [r.pk for r in choices]
        self.action = action
        self.data = data or {}
        verb = 'comenzar' if action == 'start' else 'actualizar'
        options = ' o '.join(f'{r.code} {r.title}' for r in choices[:8])
        super().__init__(f'¿Qué trabajo quieres {verb}: {options}?')


def eligible_tasks(actor, action, *, own=True):
    qs = Task.objects.filter(request__in=visible(actor)).exclude(request__state__in=['cancelled', 'emitted'])
    if action in STAGES:
        qs = qs.filter(kind=STAGES[action], state__in=TASK_STATES[action])
    if own:
        qs = qs.filter(assignee=actor)
    if action == 'start':
        qs = qs.filter(started_at__isnull=True)
    return qs


def resolve(actor, text, action, *, choices=None):
    scope = visible(actor).exclude(state__in=['cancelled', 'emitted'])
    codes = {int(n) for n in re.findall(r'\bCT-0*(\d+)\b', text, re.I)}
    words = set(re.findall(r'\w{4,}', clean(text))) - STOP
    if codes:
        rows = list(scope.filter(pk__in=codes))
        if len(rows) != len(codes):
            raise PermissionDenied('No tienes acceso a ese trabajo activo.')
        if len(rows) == 1 and action in {'edit','assign_edit','approve','corrections','receive','emit'}:
            # Identificar no equivale a autorizar. El servicio de escritura explica
            # permisos/versiones/etapas con precisión, sin ocultar una solicitud válida.
            return rows[0]
    else:
        rows = [r for r in scope if words & (set(re.findall(r'\w{4,}', clean(r.title))) - STOP)]
        if not rows:
            # Una referencia desconocida nunca se reemplaza por otra tarea propia.
            if words and not choices:
                raise Clarification('No identifico ese trabajo entre tus tareas. Indica su título o código.')
            ids = eligible_tasks(actor, action).values_list('request_id', flat=True)
            rows = list(scope.filter(pk__in=ids))
    if choices is not None:
        rows = [r for r in rows if r.pk in choices]
    # Para actualizaciones propias, ni un título ajeno visible por otro motivo otorga permiso.
    if action in {'acknowledge', 'start', 'progress', 'submit'} and actor.role != 'admin':
        if any(not r.tasks.filter(kind='design', assignee=actor).exists() for r in rows):
            raise PermissionDenied('Solo puedes actualizar tus propias tareas.')
    ids = set(eligible_tasks(actor, action, own=False).values_list('request_id', flat=True))
    valid = [r for r in rows if r.pk in ids] if action in STAGES else rows
    if not valid:
        raise Clarification(NO_TASK.get(action, 'No hay tareas disponibles para esa acción en su etapa actual.'))
    if len(valid) != 1:
        raise ChooseTask(valid, action)
    return valid[0]
