"""Presentación de hechos persistidos, sin modificar auditoría ni estados."""
from datetime import timedelta
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .models import ContentRequest, Task, ContentVersion, Participant
from .reporting import work_info

OPERATIONS = {'create': 'Solicitud registrada', 'assign': 'Asignación de diseño',
              'assign_review': 'Asignación de revisión', 'assign_edit': 'Asignación de Edición', 'start': 'Inicio del diseño',
              'progress': 'Actualización de avance', 'submit': 'Entrega para revisión',
              'approve': 'Aprobación de versión', 'corrections': 'Solicitud de correcciones',
              'edit': 'Incorporación en Edición', 'receive': 'Recepción en Switch',
              'emit': 'Confirmación de emisión', 'cancel': 'Cancelación de solicitud'}


def progress_steps(r, info):
    design = info['tasks'].get('design')
    v = info['current']
    complete = [True, bool(design and design.assignee_id and design.assigned_at),
                bool(v and v.state in {'review', 'approved'}), bool(v and v.approved_at),
                bool(v and v.approved_at), bool(v and v.edited_at),
                bool(v and v.received_at), bool(v and v.emitted_at)]
    labels = ['Solicitud', 'Asignación', 'Diseño', 'Revisión', 'Aprobación', 'Edición', 'Switch', 'Emisión']
    owners = [r.requester.name, info['designer'], info['designer'], info['reviewer'],
              v.approved_by.name if v and v.approved_by else 'Por confirmar',
              v.edited_by.name if v and v.edited_by else info['editor'],
              v.received_by.name if v and v.received_by else 'Por confirmar',
              v.emitted_by.name if v and v.emitted_by else 'No confirmada']
    current = next((i for i, done in enumerate(complete) if not done), None)
    return [{'label': label, 'owner': owner,
             'status': 'done' if done else 'blocked' if r.state == 'cancelled' else 'current' if i == current else 'pending',
             'caption': 'Completada' if done else 'Cancelada' if r.state == 'cancelled' else 'Pendiente' if i != current else 'Próximo paso'}
            for i, (label, owner, done) in enumerate(zip(labels, owners, complete))]


def history_cards(events):
    events = list(events)
    names = dict(Participant.objects.values_list('pk', 'name'))
    state = dict(ContentRequest.State.choices)
    task_states = dict(Task._meta.get_field('state').choices)
    kinds = dict(Task._meta.get_field('kind').choices)
    version_states = dict(ContentVersion._meta.get_field('state').choices)
    cards = []
    for event in events:
        before, after = event.before or {}, event.after or {}
        changes = []
        def add(label, a, b):
            if a != b:
                changes.append({'label': label, 'before': a or 'Sin definir', 'after': b or 'Sin definir'})
        for key, label in [('state','Estado general'), ('title','Título'), ('description','Descripción'),
                           ('requesting_area','Área solicitante'), ('responsible_area','Área encargada'),
                           ('priority','Prioridad'), ('due_at','Plazo')]:
            a, b = before.get(key), after.get(key)
            if key == 'state': a, b = state.get(a, a), state.get(b, b)
            if key == 'priority': a, b = {'normal':'Normal','high':'Alta'}.get(a,a), {'normal':'Normal','high':'Alta'}.get(b,b)
            if key == 'due_at':
                def human_date(value):
                    date = parse_datetime(value) if isinstance(value, str) else None
                    if date is not None:
                        return timezone.localtime(date).strftime('%d/%m/%Y %H:%M') if timezone.is_aware(date) else date.strftime('%d/%m/%Y %H:%M')
                    return value
                a, b = human_date(a), human_date(b)
            add(label, a, b)
        old_tasks = {t['kind']: t for t in before.get('tasks', [])}
        for task in after.get('tasks', []):
            old = old_tasks.get(task['kind'], {})
            prefix = kinds.get(task['kind'], 'Tarea')
            add(prefix + ' · Responsable', names.get(old.get('assignee_id'), 'Sin asignar'), names.get(task.get('assignee_id'), 'Sin asignar'))
            add(prefix + ' · Estado', task_states.get(old.get('state')), task_states.get(task.get('state')))
            add(prefix + ' · Avance', old.get('last_progress'), task.get('last_progress'))
            add(prefix + ' · Estimación (min)', old.get('eta_minutes'), task.get('eta_minutes'))
        old_versions = {v['number']: v for v in before.get('versions', [])}
        for version in after.get('versions', []):
            old = old_versions.get(version['number'], {})
            prefix = 'Versión ' + version['number']
            add(prefix + ' · Estado', version_states.get(old.get('state')), version_states.get(version.get('state')))
            for key, label in [('approved_by_id','Aprobó'), ('edited_by_id','Incorporó'), ('received_by_id','Recibió'), ('emitted_by_id','Confirmó emisión')]:
                add(prefix + ' · ' + label, names.get(old.get(key)), names.get(version.get(key)))
        cards.append({'event': event, 'label': OPERATIONS.get(event.operation, 'Actualización del trabajo'), 'changes': changes,
                      'channel': {'telegram':'Telegram','dashboard':'Panel'}.get(event.channel, 'Panel')})
    return cards


def operational_panels(actor, requests, notices):
    actions, alerts = [], []
    now = timezone.now()
    for r in requests:
        info = work_info(r)
        if r.state in {'cancelled','emitted'}:
            continue
        if r.due_at and r.due_at <= now + timedelta(minutes=20):
            alerts.append({'request':r, 'title':'Plazo vencido' if r.due_at < now else 'Plazo próximo', 'text':info['next']})
        design = info['tasks'].get('design')
        if design and not design.assignee_id and actor.role in {'coordinator','admin'}:
            actions.append({'request':r, 'label':'Asignar responsable', 'text':'El diseño aún no tiene responsable.'})
        elif info['current'] and info['current'].state == 'review':
            actions.append({'request':r, 'label':'Revisar solicitud', 'text':info['next']})
        elif info['current'] and info['current'].approved_at and not info['current'].received_at:
            actions.append({'request':r, 'label':'Ver entrega pendiente', 'text':info['next']})
        elif info['current'] and info['current'].received_at and not info['current'].emitted_at:
            actions.append({'request':r, 'label':'Ver confirmación de emisión', 'text':info['next']})
        elif design and design.assignee_id == actor.pk and design.state in {'pending','working'}:
            actions.append({'request':r, 'label':'Ver tarea asignada', 'text':info['next']})
    for out in notices:
        if out.status in {'blocked','failed','uncertain'}:
            alerts.append({'request':out.request, 'title':'Notificación por revisar',
                           'text':f'{out.recipient.name}: {out.status_label}. No se asegura una entrega sin acuse.'})
    return actions[:12], alerts[:12]
