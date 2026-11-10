"""Reglas de negocio. La IA propone acciones; este módulo decide y persiste."""
import re
import unicodedata
from datetime import timedelta
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .models import Participant, ContentRequest, Task, ContentVersion, History, Outbox, ProgressQuery
from incidents.messaging.presentation import message_blocks


class Clarification(ValidationError):
    pass


def clean(value):
    return ''.join(c for c in unicodedata.normalize('NFD', str(value).lower()) if unicodedata.category(c) != 'Mn').strip()


def require(actor, *roles):
    if not actor.active or (actor.role != 'admin' and actor.role not in roles):
        raise PermissionDenied('Tu rol no permite realizar esta operación.')


def visible(actor):
    qs = ContentRequest.objects.all()
    if not actor.active:
        return qs.none()
    if actor.role in {'admin','coordinator'}:
        return qs
    access = Q(requester=actor) | Q(tasks__assignee=actor)
    stage = {'reviewer':'review','editor':'editing','switch':'switch'}.get(actor.role)
    if stage:
        access |= Q(tasks__kind=stage, tasks__assignee__isnull=True)
    return qs.filter(access).distinct()


def person(text):
    matches = [p for p in Participant.objects.filter(active=True) if re.search(r'(?<!\w)'+re.escape(clean(p.name))+r'(?!\w)', clean(text))]
    if len(matches) != 1:
        raise Clarification('¿A qué persona te refieres? Indica su nombre registrado completo.')
    return matches[0]


def resolve_request(actor, text):
    qs = visible(actor).exclude(state__in=['cancelled','emitted'])
    codes = {int(n) for n in re.findall(r'\bCT-0*(\d+)\b', text, re.I)}
    if codes:
        choices = list(qs.filter(pk__in=codes))
        if len(choices) != len(codes):
            raise PermissionDenied('No tienes acceso a esa solicitud activa.')
    else:
        words = set(re.findall(r'\w{4,}', clean(text))) - {'termine','terminado','lista','listo','trabajo','version','grafica','para','revision','esta','estoy','quiero','contenido'}
        choices = [r for r in qs if words & set(re.findall(r'\w{4,}', clean(r.title)))]
        generic = re.fullmatch(r'(?:si[, ]*)?(?:ya )?(?:termine|estoy trabajando|estoy empezando|me faltan \d+ minutos)[.! ]*', clean(text))
        if not choices and generic:
            choices = list(qs.filter(tasks__assignee=actor).distinct())
    if len(choices) != 1:
        options = '; '.join(f'{r.code} {r.title}' for r in choices[:8])
        raise Clarification('¿Qué solicitud quieres actualizar?' + (' '+options if options else ' Indica su código CT.'))
    return choices[0]


def notify(recipient, text, key, request=None, source=None, *, suppress=False):
    # No afirmar entrega: esta fila es solo la intención durable.
    return Outbox.objects.get_or_create(key=key, defaults={'recipient':recipient,'text':text,'request':request,'source':source,
        'status':'suppressed' if suppress else 'pending', 'error_type':'SelfAssignment' if suppress else ''})[0]


def notify_role(role, request, text, key):
    for p in Participant.objects.filter(active=True, role=role):
        notify(p, text, f'{key}:{p.pk}', request)


def deadline(value):
    if not value or value == 'sin plazo':
        return None
    dt = parse_datetime(str(value))
    if dt is None or timezone.is_naive(dt):
        raise Clarification('Indica fecha y hora del plazo, con zona horaria; por ejemplo 2026-11-10T18:00:00-03:00.')
    return dt


def snapshot(request):
    return {'state':request.state,'revision':request.revision,
            'title':request.title,'description':request.description,
            'requesting_area':request.requesting_area,'responsible_area':request.responsible_area,
            'priority':request.priority,'due_at':request.due_at.isoformat() if request.due_at else None,
            'tasks':list(request.tasks.order_by('pk').values('id','kind','assignee_id','state','last_progress','eta_minutes')),
            'versions':list(request.versions.order_by('pk').values('number','state','approved_by_id','edited_by_id','received_by_id','emitted_by_id'))}


def log(request, actor, operation, before, message=None):
    request.revision += 1
    request.save(update_fields=['state','revision','updated_at'])
    kind = {'assign':'design','assign_review':'review','assign_edit':'editing','start':'design','progress':'design','submit':'design','approve':'review','corrections':'review','edit':'editing','receive':'switch','emit':'switch'}.get(operation)
    History.objects.create(request=request,task=request.tasks.filter(kind=kind).first() if kind else None,actor=actor,operation=operation,before=before,after=snapshot(request),
                           message=message,channel=message.channel if message else 'dashboard')


@transaction.atomic
def create_request(actor, data, message=None):
    actor=Participant.objects.select_for_update().get(pk=actor.pk)
    require(actor, 'journalist','coordinator')
    for key in ('title','description','requesting_area','responsible_area'):
        if not isinstance(data.get(key),str) or not data[key].strip():
            raise Clarification('Falta '+key+'.')
    r=ContentRequest(requester=actor,title=data['title'],description=data['description'],requesting_area=data['requesting_area'],
                     responsible_area=data['responsible_area'],due_at=deadline(data.get('due_at')),priority=data.get('priority','normal'))
    r.full_clean();r.save()
    Task.objects.create(request=r,kind='design',due_at=r.due_at)
    log(r,actor,'create',{},message)
    notify_role('coordinator',r,message_blocks('Nueva solicitud pendiente',[
        ('Solicitud',f'{r.code} · {r.title}'),('Área solicitante',r.requesting_area),('Prioridad',r.get_priority_display())],
        '¿A quién quieres asignar el diseño?',kind='request'),f'new:{r.pk}')
    return r


def own_task(actor, r, kind):
    require(actor, 'designer')
    task=r.tasks.get(kind=kind)
    if actor.role != 'admin' and task.assignee_id != actor.pk:
        raise PermissionDenied('Solo puedes actualizar tus propias tareas.')
    return task


def stage_task(actor,r,kind,role):
    if not actor.active or actor.role not in {role, 'admin'}:
        label = {'editor':'editor','reviewer':'revisor','switch':'responsable de Switch'}[role]
        raise PermissionDenied(f'Esta etapa requiere un {label} autorizado o un administrador.')
    task=r.tasks.filter(kind=kind).first()
    if task is None:
        if kind == 'editing' and r.versions.filter(approved_at__isnull=False).exists():
            raise Clarification(f'{r.code} tiene material aprobado, pero falta la tarea de Edición. Requiere revisión técnica; no se modificó el trabajo.')
        raise Clarification('La solicitud todavía no está en esa etapa. Completa primero la etapa anterior.')
    if task.assignee_id and actor.role!='admin' and task.assignee_id != actor.pk:
        raise PermissionDenied(f'No puedes registrar esta operación: {task.get_kind_display()} está asignada a otro trabajador.')
    task.assignee=actor;task.assigned_at=task.assigned_at or timezone.now()
    return task


SENSITIVE={'assign','assign_review','assign_edit','approve','corrections','cancel','emit'}


@transaction.atomic
def execute(actor, request_id, action, data, *, revision=None, confirmed=False, message=None):
    actor=Participant.objects.select_for_update().get(pk=actor.pk)
    if not actor.active:raise PermissionDenied()
    r=ContentRequest.objects.select_for_update().get(pk=request_id)
    if not visible(actor).filter(pk=r.pk).exists():raise PermissionDenied('No tienes acceso a esa solicitud.')
    if revision is not None and r.revision != revision:
        raise Clarification('La solicitud cambió. Revisa su estado y vuelve a confirmar la operación.')
    if action in SENSITIVE and not confirmed:
        raise Clarification('Esta operación requiere confirmación explícita.')
    if r.state in {'emitted','cancelled'}:raise Clarification('La solicitud ya está cerrada.')
    before=snapshot(r);now=timezone.now();v=None
    if action in {'assign','assign_review','assign_edit'}:
        require(actor,'coordinator')
        assignee=Participant.objects.filter(pk=data['assignee_id'],active=True).first()
        if assignee is None:raise Clarification('Esa persona no está activa o ya no está disponible.')
        role = {'assign_review':'reviewer', 'assign_edit':'editor'}.get(action, 'designer')
        if assignee.role not in {role,'admin'}:raise Clarification('Selecciona una persona autorizada para esa etapa.')
        task=r.tasks.filter(kind={'assign_review':'review', 'assign_edit':'editing'}.get(action, 'design')).first()
        if task is None:
            if action == 'assign_edit':
                if r.versions.filter(approved_at__isnull=False).exists():
                    raise Clarification(f'{r.code} tiene material aprobado, pero falta la tarea de Edición. Requiere revisión técnica.')
                raise Clarification('Antes de asignar Edición debe aprobarse una versión del trabajo.')
            raise Clarification('Esta etapa todavía no está disponible.')
        if task.state in {'review','done'}:
            if action == 'assign_edit':raise Clarification('Edición ya está completada; no se reasigna una etapa finalizada.')
            raise Clarification('La tarea ya avanzó a revisión; solicita correcciones antes de reasignar.')
        if action == 'assign_review' and not r.versions.filter(state='review').exists():
            raise Clarification('No hay una versión pendiente de revisión.')
        if action == 'assign_edit':
            versions = r.versions.filter(approved_at__isnull=False, edited_at__isnull=True)
            if data.get('version'):
                versions = versions.filter(number=data['version'])
            if not versions.exists():
                raise Clarification('Edición requiere una versión aprobada y aún no incorporada a la nota.')
        task.assignee=assignee;task.assigned_at=now;task.save()
        if action == 'assign':r.state='production' if task.state == 'working' else 'assigned'
        stage = {'assign_review':'la revisión de', 'assign_edit':'la Edición de'}.get(action, 'el diseño de')
        from .access import telegram_allowed
        same_account = (actor.pk == assignee.pk or (actor.telegram_id and actor.telegram_id == assignee.telegram_id)) and telegram_allowed(actor) and telegram_allowed(assignee)
        task_label = task.get_kind_display()
        due = timezone.localtime(task.due_at).strftime('%d/%m %H:%M') if task.due_at else 'Sin plazo definido'
        text = message_blocks('Nueva tarea asignada',[
            ('Solicitud',f'{r.code} · {r.title}'),('Etapa',task_label),('Plazo',due)],
            'Entra con /contenido y avísame cuando comiences.' if action=='assign' else 'Entra con /contenido para continuar con esta etapa.',
            body=f'{actor.name} te asignó {task_label}.',kind='assignment')
        notify(assignee,text,f'{action}:{r.pk}:{r.revision}',r,source=message,suppress=bool(same_account and message))
    elif action in {'start','progress','submit'}:
        task=own_task(actor,r,'design')
        if task.state in {'done','cancelled'}:raise Clarification('La tarea de diseño ya terminó; las correcciones deben abrir una nueva versión.')
        if action=='start':
            if task.state != 'pending' or task.started_at:
                raise Clarification('El diseño ya comenzó o está en revisión; no se vuelve a iniciar.')
            task.state='working';task.started_at=task.started_at or now;r.state='production'
        if action=='progress':
            text=str(data.get('progress','')).strip()
            if not text:raise Clarification('¿Qué avance quieres registrar?')
            task.last_progress=text[:3000];task.progress_at=now
            eta=data.get('eta_minutes')
            if eta is not None:
                if type(eta) is not int or not 1<=eta<=10080:raise Clarification('Indica una estimación válida en minutos.')
                task.eta_minutes=eta
        if action=='submit':
            number=str(data.get('version','')).strip()
            if not re.fullmatch(r'\d{1,3}\.\d{1,3}',number):raise Clarification('¿Qué versión entregas para revisión? Por ejemplo, 1.0.')
            if r.versions.filter(state='review').exists():
                raise Clarification('Ya hay una versión en revisión. Espera su resolución antes de entregar otra.')
            v,created=ContentVersion.objects.get_or_create(request=r,number=number,defaults={'author':actor,'state':'review'})
            if not created and (v.state!='working' or v.approved_at):raise Clarification('Esa versión ya fue entregada. No se sobrescribe; indica una nueva versión.')
            v.state='review';v.save();task.state='review';task.last_progress=f'Versión {number} lista para revisión';task.progress_at=now;r.state='review'
            review,_=Task.objects.get_or_create(request=r,kind='review',defaults={'due_at':r.due_at})
            review.state='pending';review.save()
            notify_role('reviewer',r,message_blocks('Versión lista para revisión',[
                ('Solicitud',f'{r.code} · {r.title}'),('Versión',number)],'Revisa el material antes de aprobarlo o pedir correcciones.',kind='assignment'),f'review:{v.pk}')
        task.save()
    elif action in {'approve','corrections','edit','receive','emit'}:
        number=str(data.get('version',''))
        v=r.versions.filter(number=number).first()
        if v is None:raise Clarification(f'{r.code} no tiene la versión {number}. Indica una versión existente de este trabajo.')
        if action in {'approve','corrections'}:
            task=stage_task(actor,r,'review','reviewer')
            if action=='approve':
                if v.state!='review' or v.approved_at:raise Clarification('Solo se aprueba una versión pendiente de revisión.')
                v.state='approved';v.approved_by=actor;v.approved_at=now;r.state='approved';task.state='done';task.completed_at=now
                r.tasks.filter(kind='design').update(state='done',completed_at=now)
                editing,_=Task.objects.get_or_create(request=r,kind='editing',defaults={'due_at':r.due_at})
                editing.state='pending';editing.save()
                notify_role('editor',r,message_blocks('Versión aprobada disponible',[
                    ('Solicitud',f'{r.code} · {r.title}'),('Versión',number)],'Confirma cuando la incorpores a la nota.',kind='assignment'),f'approved:{v.pk}')
            else:
                if r.versions.exclude(pk=v.pk).filter(state__in=['working','review']).exists():
                    raise Clarification('Ya hay otra versión en trabajo o revisión. Resuélvela antes de abrir nuevas correcciones.')
                new=str(data.get('new_version',''))
                if not re.fullmatch(r'\d{1,3}\.\d{1,3}',new) or tuple(map(int,new.split('.'))) <= tuple(map(int,number.split('.'))):
                    raise Clarification('Indica un número de versión nuevo y posterior para las correcciones.')
                if r.versions.filter(number=new).exists():raise Clarification('Ese número de versión ya existe.')
                designer=r.tasks.get(kind='design')
                if not designer.assignee:raise Clarification('Asigna primero un diseñador.')
                ContentVersion.objects.create(request=r,number=new,author=designer.assignee,state='working')
                if not v.approved_at:v.state='changes'
                designer.state='working';designer.completed_at=None;designer.last_progress=str(data.get('progress','Correcciones solicitadas'));designer.save()
                r.state='production';task.state='pending'
                notify(designer.assignee,message_blocks('Correcciones solicitadas',[
                    ('Solicitud',f'{r.code} · {r.title}'),('Versión',new)],
                    body=f'Trabaja las correcciones en la nueva versión. La versión {number} conserva su historial.',kind='assignment'),f'changes:{r.pk}:{new}',r)
        elif action=='edit':
            if not v.approved_at or v.edited_at:raise Clarification('La versión debe estar aprobada y pendiente de Edición.')
            task=stage_task(actor,r,'editing','editor')
            v.edited_by=actor;v.edited_at=now;r.state='editing';task.state='done';task.completed_at=now
            stage,_=Task.objects.get_or_create(request=r,kind='switch',defaults={'due_at':r.due_at});stage.state='pending';stage.save()
            notify_role('switch',r,message_blocks('Material disponible en Switch',[
                ('Solicitud',f'{r.code} · {r.title}'),('Versión',number)],'Confirma la recepción del material; la emisión se registra por separado.',kind='assignment'),f'edited:{v.pk}')
        elif action=='receive':
            task=stage_task(actor,r,'switch','switch')
            if not v.approved_at or not v.edited_at or v.received_at:raise Clarification('La versión debe estar aprobada, incorporada y pendiente de recepción.')
            v.received_at=now;v.received_by=actor;r.state='received';task.state='done';task.completed_at=now
        else:
            task=stage_task(actor,r,'switch','switch')
            if not v.received_at or v.emitted_at:raise Clarification('Confirma primero la recepción de esa versión.')
            v.emitted_by=actor;v.emitted_at=now;r.state='emitted'
        task.save();v.save()
    elif action=='cancel':
        require(actor,'coordinator');r.state='cancelled';r.tasks.exclude(state='done').update(state='cancelled')
    else:raise Clarification('No reconozco esa operación de contenidos.')
    # La entrega de una versión antigua no completa la nueva que sigue en trabajo.
    latest = r.versions.order_by('-pk').first()
    if latest and action != 'cancel':
        if latest.state == 'working':r.state = 'production'
        elif latest.state == 'review':r.state = 'review'
        elif latest.approved_at:
            r.state = 'emitted' if latest.emitted_at else 'received' if latest.received_at else 'editing' if latest.edited_at else 'approved'
    log(r,actor,action,before,message)
    # Responder a consultas abiertas solo sobre las tareas afectadas y una vez por cambio.
    affected = [task.pk] if action != 'cancel' else []
    queries = ProgressQuery.objects.filter(worker=actor,answered_at__isnull=True,tasks__in=affected,
                                            notification__status='sent').distinct()
    if message is not None:
        queries = queries.filter(notification__sent_at__lte=message.created_at)
    for query in queries:
        from .activity import last_activity
        activity = last_activity(r)
        fields = [('Solicitud',f'{r.code} · {r.title}'),('Responsable',actor.name),
                  ('Último avance',activity['text'] if activity else r.get_state_display()),('Estado',r.get_state_display())]
        if task.eta_minutes and action == 'progress':
            fields.append(('Estimación informada',f'{task.eta_minutes} minutos.'))
        text = message_blocks(f'Nuevo avance de {r.code}' if action=='progress' else f'Actualización de {r.code}',
                              fields,kind='progress' if action=='progress' else 'followup')
        notify(query.requester,text,f'query-answer:{query.pk}:{r.pk}:{r.revision}',r)
        query.tasks.remove(task)
        if not query.tasks.exists():query.answered_at=now;query.save()
    return r


def followup_tasks(actor, worker, task_ids=None):
    require(actor,'coordinator')
    from .access import telegram_allowed
    if worker.pk == actor.pk or not telegram_allowed(worker):
        raise Clarification('El responsable debe ser otra persona con Telegram autorizado y verificado. No envié ningún mensaje.')
    qs = Task.objects.filter(assignee=worker,state__in=['pending','working','review'],request__in=visible(actor)).exclude(request__state__in=['cancelled','emitted'])
    if task_ids is not None:qs = qs.filter(pk__in=task_ids)
    tasks=list(qs.select_related('request'))
    if task_ids is not None and {t.pk for t in tasks} != set(task_ids):
        raise Clarification('Las asignaciones cambiaron. Consulta de nuevo antes de enviar el aviso.')
    if not tasks:raise Clarification('No hay tareas activas que consultar a esa persona.')
    return tasks


def prepare_followup(actor, text, *, focus=None):
    """Resolver destinatario desde asignaciones reales, nunca desde un ID de la IA."""
    require(actor, 'coordinator')
    codes = {int(n) for n in re.findall(r'\bCT-0*(\d+)\b', text, re.I)}
    named = [p for p in Participant.objects.filter(active=True)
             if re.search(r'(?<!\w)' + re.escape(clean(p.name)) + r'(?!\w)', clean(text))]
    if len(named) > 1:raise Clarification('Revisemos una persona a la vez. Indica el CT y su responsable.')
    if not codes and focus:codes = {focus}
    if codes:
        rows = list(visible(actor).filter(pk__in=codes))
        if len(rows) != len(codes):raise PermissionDenied('No tienes acceso a esa solicitud.')
        tasks = list(Task.objects.filter(request__in=rows,kind='design').select_related('assignee','request'))
        workers = {t.assignee_id for t in tasks}
        if len(workers) != 1 or None in workers:
            raise Clarification('Indica un responsable asignado y sus trabajos; revisemos una persona a la vez.')
        worker = tasks[0].assignee
        if named and named[0].pk != worker.pk:
            raise Clarification('La persona indicada no es la responsable asignada. Revisa el CT; no envié nada.')
        return worker, followup_tasks(actor, worker, [t.pk for t in tasks])
    if not named:raise Clarification('¿A quién y por qué solicitud quieres consultar? Indica el código CT.')
    worker = named[0]
    return worker, followup_tasks(actor, worker)


@transaction.atomic
def ask_updates(actor, worker, task_ids=None, *, question='', message=None):
    actor=Participant.objects.select_for_update().get(pk=actor.pk)
    worker=Participant.objects.select_for_update().get(pk=worker.pk)
    tasks = followup_tasks(actor, worker, task_ids)
    codes='; '.join(f'{t.request.code} {t.request.title}' for t in tasks)
    # Una consulta todavía abierta no genera avisos duplicados.
    ids = {t.pk for t in tasks}
    for query in ProgressQuery.objects.filter(worker=worker,answered_at__isnull=True).prefetch_related('tasks'):
        previous = {t.pk for t in query.tasks.all()}
        if query.requester_id == actor.pk and previous == ids:
            return message_blocks('Seguimiento pendiente',body='Ya hay una consulta pendiente para esas tareas. No dupliqué el aviso.',kind='followup')
        if previous & ids:raise Clarification('Ya hay un seguimiento abierto para esa tarea. Espera su respuesta antes de abrir otro.')
    question = question.strip() or '¿Cómo vas con cada trabajo y cuánto estimas que te falta?'
    if len(question) > 3000:raise Clarification('Acota la pregunta de seguimiento a 3000 caracteres.')
    key = f'query:message:{message.pk}' if message else f'query:{actor.pk}:{worker.pk}:{timezone.now().isoformat()}'
    if ProgressQuery.objects.filter(notification__key=key).exists():
        return message_blocks('Seguimiento registrado',body='Esta consulta ya quedó registrada; no se duplicó el aviso.',kind='followup')
    text = message_blocks('Consulta de seguimiento',[('Solicitud',codes),('Pregunta',question)],
        body=f'{actor.name} solicita una actualización.',next_step='Entra con /contenido para responder; si hay varios trabajos, indica su CT.',kind='followup')
    out=notify(worker,text,key,source=message,
               request=tasks[0].request if len(tasks)==1 else None)
    q=ProgressQuery.objects.create(requester=actor,worker=worker,notification=out);q.tasks.set(tasks)
    return message_blocks('Seguimiento pendiente de envío', [('Responsable',worker.name),('Solicitud',codes)],
        body='La consulta quedó pendiente de envío. Te avisaré cuando responda.',kind='followup')


def schedule_reminders(minutes=20):
    now=timezone.now();count=0
    for task in Task.objects.filter(due_at__lte=now+timedelta(minutes=minutes),state__in=['pending','working'],assignee__active=True).exclude(request__state__in=['cancelled','emitted']).select_related('request','assignee'):
        _,created=Outbox.objects.get_or_create(key=f'deadline:{task.pk}:{task.due_at.isoformat()}',defaults={'recipient':task.assignee,'request':task.request,
            'text':message_blocks('Plazo por revisar',[('Solicitud',f'{task.request.code} · {task.request.title}')],
                body=f'{task.assignee.name}, el trabajo vence pronto o ya venció. ¿Cómo vas? Responde en /contenido.',kind='progress')})
        count+=created
    return count
