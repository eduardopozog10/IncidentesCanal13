"""Inbox durable → interpretación → autorización → transacción + outbox."""
import re
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from incidents.ai.base import AITemporaryError, AIConfigurationError
from incidents.services.conversation_lock import conversation_lock
from .ai import GeminiContentInterpreter, validate_plan
from .models import Conversation, Message, Outbox, Participant, Task, ProgressQuery
from . import services as domain
from .resolution import resolve, ChooseTask
from .reporting import query as read_query
from .interpretation import adapt_updates, versions_in, version_answer, focused_reference
from .access import telegram_allowed, message_binding_valid
from .dialogue import response_act
from incidents.messaging.presentation import message_blocks

CANCEL = {'cancelar', 'cancela', 'cancelalo', 'no sigamos', 'dejemoslo hasta aqui',
          'cancela el borrador', 'cancelar borrador', 'no confirmo'}
QUESTIONS = {
    'title': '¿Qué contenido necesitas?',
    'description': '¿Qué debe incluir el contenido?',
    'requesting_area': '¿Qué área lo solicita?',
    'responsible_area': '¿Qué área debe prepararlo?',
    'due_at': '¿Para cuándo lo necesitas? También puedes indicar «sin plazo».',
}
LABELS = {'assign': 'asignar el diseño de', 'assign_review': 'asignar la revisión de', 'assign_edit': 'asignar Edición de', 'approve': 'aprobar', 'corrections': 'abrir correcciones',
          'cancel': 'cancelar', 'emit': 'confirmar emisión'}
REVIEW_DRAFT = {'revisar borrador', 'mostrar resumen', 'retomar borrador'}
SMALL_TALK = {'hola': 'Hola. ¿Qué trabajo necesitas gestionar?',
              'buenos dias': 'Buenos días. ¿Qué necesitas coordinar?',
              'buenas tardes': 'Buenas tardes. ¿Qué necesitas coordinar?',
              'buenas noches': 'Buenas noches. ¿Qué necesitas coordinar?',
              'gracias': 'De nada. Aquí sigo si necesitas revisar otro avance.',
              'muchas gracias': 'De nada. Aquí sigo si necesitas revisar otro avance.',
              'un momento': 'Tómate el tiempo para revisar. Sigo aquí cuando quieras continuar.',
              'hasta luego': 'Hasta luego. Los cambios guardados quedan disponibles en el panel.'}


def draft_controls(draft, text):
    """Vetos explícitos prevalecen sobre una propuesta de la IA y su contexto viejo."""
    value = domain.clean(text)
    if re.search(r'\bno (?:la |lo )?(?:ingreses|registres)\b|\bno quiero (?:ingresar|registrar)\b', value):
        draft['_on_hold'] = True
    if re.search(r'(?:(?:no|tampoco) (?:he |hemos )?definido|sin definir|no definida|no tengo definida).*\bprioridad\b|\bprioridad\b.*(?:sin definir|no definida)', value):
        draft.pop('priority', None)
        draft['_priority_unspecified'] = True
    if re.search(r'\b(?:no hay una persona asignada|sin una persona responsable definida|sin (?:disenador|persona) asignad[oa]|pendiente de asignacion)\b', value):
        draft['_assignment_confirmed_unassigned'] = True


def draft_summary(draft):
    priority = draft.get('priority')
    if priority:
        priority_label = {'normal': 'Normal', 'high': 'Alta'}.get(priority, priority)
        priority_label += ' (indicada por ti)'
    else:
        priority_label = 'No indicada por ti; Normal es el valor predeterminado al registrar'
    return (f"{draft['title']}\n{draft['description']}\nÁrea solicitante: {draft['requesting_area']}\n"
            f"Área encargada: {draft['responsible_area']}\nDiseñador: sin asignar\n"
            f"Estado inicial: pendiente de asignación\nPlazo: {draft['due_at']}\nPrioridad: {priority_label}")


def normalized(text):
    return re.sub(r'[^\w\s]', '', domain.clean(text)).strip()


def enqueue(actor, external_id, text, *, channel='telegram', payload=None):
    if not actor.active or not isinstance(text, str) or not 0 < len(text.strip()) <= 4000:
        raise PermissionDenied('Mensaje o participante no admitido.')
    msg, created = Message.objects.get_or_create(
        external_id=external_id,
        defaults={'participant': actor, 'text': text, 'channel': channel, 'payload': payload or {}},
    )
    if not created and (msg.participant_id != actor.pk or msg.text != text or msg.channel != channel):
        raise PermissionDenied('El identificador ya pertenece a otro mensaje.')
    return msg


def context_for(actor, conv):
    requests = list(domain.visible(actor).exclude(state__in=['cancelled', 'emitted']).order_by('-pk')[:60])
    return {
        'now': timezone.localtime().isoformat(), 'role': actor.role,
        'draft': conv.draft, 'pending': conv.pending, 'context': conv.context,
        'followups': [
            {'id': q.pk, 'question': q.notification.text,
             'tasks': [{'code': t.request.code, 'title': t.request.title} for t in q.tasks.filter(assignee=actor).select_related('request')]}
            for q in ProgressQuery.objects.filter(worker=actor, answered_at__isnull=True,
                      notification__status='sent').select_related('notification').prefetch_related('tasks__request')],
        'requests': [{'code': r.code, 'title': r.title, 'state': r.state,
                      'tasks': [{'kind':t.kind,'assignee':t.assignee.name if t.assignee else None,'state':t.state,
                                 'last_progress':t.last_progress,'own':t.assignee_id==actor.pk,
                                 'can_start_design':t.kind=='design' and actor.role in {'designer','admin'}
                                     and (t.assignee_id==actor.pk or actor.role=='admin') and t.state=='pending' and not t.started_at}
                                for t in r.tasks.select_related('assignee')],
                      'versions': list(r.versions.values('number', 'state'))} for r in requests],
        'participants': list(Participant.objects.filter(active=True).values('name', 'role'))
        if actor.role in {'coordinator', 'admin'} else [],
    }


def contextual_response(text, conv):
    references = ['operacion', 'solicitud', 'asignacion', 'envio', 'consulta']
    pending = conv.pending
    if pending.get('request_id'):
        r = domain.visible(conv.participant).filter(pk=pending['request_id']).first()
        if r: references.extend([r.code, r.title])
    return response_act(text, pending, references)


class Processor:
    def __init__(self, interpreter=None):
        self._interpreter = interpreter

    @property
    def interpreter(self):
        if self._interpreter is None:
            self._interpreter = GeminiContentInterpreter()
        return self._interpreter

    def process(self, message):
        with conversation_lock(f'content:{message.participant_id}'):
            message.refresh_from_db()
            if message.status in {'done', 'exhausted'} or message.retry_at > timezone.now():
                return message.result.get('reply', '')
            # Conserva el orden: un fallo previo no permite que una confirmación lo adelante.
            if Message.objects.filter(participant=message.participant, pk__lt=message.pk,
                                      status__in=['pending', 'retry', 'processing']).exists():
                return ''
            actor = Participant.objects.get(pk=message.participant_id)
            conv, _ = Conversation.objects.get_or_create(participant=actor)
            message.attempts += 1
            message.status = 'processing'
            message.save(update_fields=['attempts', 'status'])
            try:
                if message.channel == 'telegram' and (not telegram_allowed(actor) or not message_binding_valid(actor,message)):
                    raise PermissionDenied('Tu cuenta ya no está autorizada para esta conversación.')
                command = normalized(message.text)
                response = contextual_response(message.text, conv)
                plan = None
                previous_draft = conv.draft.copy()
                # Una pausa/corrección explícita del borrador invalida el resumen,
                # incluso si falla la IA. Una entrada desconocida NO lo descarta.
                if not response and not message.text.startswith('/') and conv.draft:
                    draft_controls(conv.draft, message.text)
                    if conv.draft != previous_draft:
                        conv.pending = {}
                        Conversation.objects.filter(pk=conv.pk).update(draft=conv.draft, pending={})
                if not response and command not in CANCEL | REVIEW_DRAFT | SMALL_TALK.keys() and not message.text.startswith('/'):
                    if conv.module != 'content' and message.channel == 'telegram':
                        raise domain.Clarification('Elige /contenido o /incidencia antes de continuar.')
                    choice = conv.context.get('choice')
                    operation = conv.context.get('operation')
                    if operation and version_answer(message.text):
                        if operation['channel'] != message.channel:
                            raise domain.Clarification('Aclara la versión desde el mismo canal donde solicitaste la operación.')
                        codes = {int(n) for n in re.findall(r'\bCT-0*(\d+)\b', message.text, re.I)}
                        if codes and codes != {operation['request_id']}:
                            raise domain.Clarification('Esa versión menciona otra solicitud. Indica nuevamente la operación y su código CT.')
                        data = {**operation['data'], 'version': versions_in(message.text)[0]}
                        plan = {'updates': [{'action':operation['action'], 'data':data,
                                'evidence': f"CT-{operation['request_id']:06d} " + operation['evidence'] + ' ' + message.text,
                                '_context_revision': operation['revision']}]}
                    elif version_answer(message.text):
                        raise domain.Clarification('¿A qué solicitud corresponde esa versión y qué operación quieres realizar?')
                    elif choice and (re.match(r'^con\b', domain.clean(message.text)) or
                                   len(message.text.split()) <= 3):
                        if choice.get('channel') != message.channel:
                            raise domain.Clarification('Retomemos la aclaración desde el mismo canal donde elegiste la tarea.')
                        r = resolve(actor, message.text, choice['action'], choices=choice['ids'])
                        plan = {'updates': [{'action': choice['action'], 'data': choice['data'],
                                             'evidence': r.code + ' ' + choice.get('evidence', '') + ' ' + message.text}]}
                    else:
                        plan = validate_plan(self.interpreter.interpret(message.text, context_for(actor, conv)), message.text)
                    message.result = {'interpretation': plan}
                    message.save(update_fields=['result'])
                with transaction.atomic():
                    actor = Participant.objects.select_for_update().get(pk=actor.pk)
                    if not actor.active or (message.channel == 'telegram' and (not telegram_allowed(actor) or not message_binding_valid(actor,message))):
                        raise PermissionDenied('Tu acceso está deshabilitado.')
                    conv = Conversation.objects.select_for_update().get(pk=conv.pk)
                    reply = self.apply(actor, conv, message, plan, previous_draft=previous_draft)
                    conv.save()
                    self.finish(message, reply)
                return reply
            except ChooseTask as exc:
                reply = message_blocks('Aclaremos el trabajo',body='; '.join(exc.messages),kind='confirmation')
                with transaction.atomic():
                    conv.refresh_from_db()
                    conv.pending = {}
                    conv.context['choice'] = {'action': exc.action, 'ids': exc.choices, 'data': exc.data,
                                              'evidence': message.text, 'channel': message.channel}
                    conv.save(update_fields=['pending', 'context'])
                    self.finish(message, reply)
                return reply
            except (PermissionDenied, ValidationError) as exc:
                reply = '; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
                reply = message_blocks('Acción no autorizada' if isinstance(exc,PermissionDenied) else 'Necesito una aclaración',
                        body=reply or 'No tienes permiso para esa operación.',kind='warning' if isinstance(exc,PermissionDenied) else 'confirmation')
                with transaction.atomic():
                    # Conservar la operación para aclararla, sin permitir ejecutar
                    # un resumen que ahora podría ser obsoleto.
                    conv.refresh_from_db()
                    if conv.pending:conv.pending['needs_review'] = True
                    conv.context.pop('operation', None)
                    conv.save(update_fields=['pending','context'])
                    self.finish(message, reply or 'No tienes permiso para esa operación.')
                return reply
            except Exception as exc:
                conv.refresh_from_db()
                if conv.pending:
                    conv.pending['needs_review'] = True
                    conv.save(update_fields=['pending'])
                message.error_type = type(exc).__name__
                terminal = isinstance(exc, AIConfigurationError) or message.attempts >= settings.CONTENT_MAX_ATTEMPTS
                message.status = 'exhausted' if terminal else 'retry'
                delay = max(settings.CONTENT_RETRY_SECONDS, getattr(exc, 'retry_after', 0) or 0)
                message.retry_at = timezone.now() + timedelta(seconds=delay)
                message.save(update_fields=['error_type', 'status', 'retry_at'])
                text = ('No pude procesar ese mensaje. No se aplicó la operación. Revisa el estado antes de enviar uno nuevo.'
                        if terminal else 'El procesamiento está tardando. Conservaré tu mensaje para recuperarlo; no necesitas repetirlo.')
                text = message_blocks('Procesamiento por revisar' if terminal else 'Procesamiento pendiente',body=text,kind='warning')
                domain.notify(actor, text, f'processing-error:{message.pk}:{terminal}', source=message)
                return text

    @staticmethod
    def finish(message, reply):
        message.status = 'done'
        message.result = {**message.result, 'reply': reply}
        message.error_type = ''
        message.save(update_fields=['status', 'result', 'error_type'])
        out = domain.notify(message.participant, reply, f'reply:{message.pk}', source=message)
        if message.channel == 'dashboard':
            out.status = 'local'
            out.save(update_fields=['status'])

    def apply(self, actor, conv, message, plan, previous_draft=None):
        text = message.text
        command = normalized(text)
        previous_pending = conv.pending.copy()
        response = contextual_response(text, conv)
        if text.strip() in {'/contenido', '/incidencia', '/start'}:
            # Compatibilidad para llamadas internas antiguas. El receptor real
            # consume estos comandos en telegram_routing, antes del agente.
            if text.strip() == '/incidencia' and message.channel == 'telegram':
                from telegram_routing.authorization import incident_account
                if not incident_account(actor.telegram_id):
                    return 'Tu cuenta no está autorizada para Incidencias. No se modificó tu selección ni tus permisos.'
            if text.strip() != '/start':
                conv.module = {'/contenido': 'content', '/incidencia': 'incident'}[text.strip()]
            return {'content': 'Estás en coordinación de contenidos. ¿Qué necesitas gestionar?',
                    'incident': 'Estás en incidencias. Cuéntame qué ocurrió.',
                    '': 'Elige /contenido para coordinar trabajos o /incidencia para reportar una falla.'}[conv.module]
        if text.startswith('/'):
            return 'Usa /contenido o /incidencia para elegir el módulo.'
        if command in SMALL_TALK:
            reply = SMALL_TALK[command]
            if conv.pending:reply += ' La operación sigue pendiente; confirma o indica qué quieres corregir.'
            if conv.draft:
                reply += ' Tu borrador se conserva; escribe «revisar borrador» cuando quieras retomarlo.'
            return reply
        if response == 'decline':
            conv.pending = {}
            return 'No realicé la operación ni envié mensajes. El borrador y las solicitudes se conservan.'
        if response == 'hold':
            return 'Dejé la operación pendiente, sin ejecutarla ni enviar mensajes. Confirma cuando estés listo o indica qué quieres corregir.'
        if command in CANCEL:
            conv.pending = {}; conv.draft = {}
            conv.context = {}
            return 'Dejé sin efecto el borrador y la operación pendiente. Las solicitudes registradas se conservan.'
        if response == 'confirm':
            if conv.draft.get('_on_hold'):
                conv.pending = {}
                return 'El borrador sigue en pausa y no lo ingresé. Escribe «revisar borrador» antes de volver a confirmar.'
            pending = conv.pending
            if not pending:
                return 'No hay una operación pendiente de confirmación. Indica qué necesitas hacer.'
            if pending.get('needs_review'):
                return 'Conservé la operación, pero necesita revisión. Indica de nuevo la operación con su código CT; si es un borrador, escribe «revisar borrador».'
            if message.channel != pending.get('channel'):
                return 'Confirma desde el mismo canal donde revisaste el resumen. La operación sigue pendiente.'
            if message.channel == 'telegram':
                if not Outbox.objects.filter(key=f"reply:{pending['prompt_id']}", status='sent',
                                             sent_at__lte=message.created_at).exists():
                    return 'El resumen aún no tiene entrega confirmada. La operación sigue pendiente; espera el resumen antes de confirmar.'
            conv.pending = {}
            if pending['action'] == 'ask':
                return domain.ask_updates(actor, Participant.objects.get(pk=pending['worker']), task_ids=pending.get('task_ids'),
                                          question=pending.get('question', ''), message=message)
            if pending['action'] == 'create':
                r = domain.create_request(actor, pending['data'], message)
                conv.draft = {}
                return message_blocks('Solicitud registrada',[
                    ('Solicitud',f'{r.code} · {r.title}'),('Área solicitante',r.requesting_area),
                    ('Área encargada',r.responsible_area),('Prioridad',r.get_priority_display()),
                    ('Estado','Pendiente de asignación')],body='La solicitud quedó registrada correctamente.',kind='success')
            r = domain.execute(actor, pending['request_id'], pending['action'], pending['data'],
                               revision=pending['revision'], confirmed=True, message=message)
            return operation_reply(r, pending['action'], pending['data'])
        if command in REVIEW_DRAFT:
            domain.require(actor, 'journalist', 'coordinator')
            if not conv.draft:
                return 'No hay un borrador de contenidos para revisar.'
            conv.draft.pop('_on_hold', None)
            conv.pending = {}
            missing = next((k for k in QUESTIONS if not conv.draft.get(k)), None)
            if missing:
                return QUESTIONS[missing]
            conv.pending = {'action': 'create', 'data': conv.draft.copy(), 'prompt_id': message.pk, 'channel': message.channel}
            return message_blocks('Revisa la solicitud',body=draft_summary(conv.draft)+'\n\n¿La ingreso así? Responde «confirmo» o corrige los datos.',kind='confirmation')
        updates = adapt_updates(plan['updates'], text)
        message.result['resolved_updates'] = updates
        # Una nueva acción sustituye la anterior solo después de interpretarla.
        actions = {u['action'] for u in updates}
        if actions - {'query', 'unknown', 'acknowledge'}:
            conv.pending = {}
        if len(updates) > 1 and all(u['action'] == 'query' for u in updates):
            updates = [{'action': 'query', 'data': {}, 'evidence': text}]
        if conv.draft.get('_on_hold') and not any(u['action'] == 'create' for u in updates):
            return 'No la ingresé. El borrador sigue en pausa; escribe «revisar borrador» cuando quieras retomarlo.'
        if len(updates) > 1 and all(u['action'] == 'create' for u in updates):
            # El proveedor puede separar campos del mismo borrador. Solo unir
            # valores compatibles; dos títulos/descripciones distintos son ambiguos.
            merged = {}
            clear_fields = set()
            for update in updates:
                clear_fields.update(update.get('clear_fields', []))
                for key, value in update['data'].items():
                    if key not in QUESTIONS and key != 'priority':
                        continue
                    if key in merged and merged[key] != value:
                        raise domain.Clarification('Hay datos de más de una solicitud. Creemos una a la vez.')
                    merged[key] = value
            updates = [{'action': 'create', 'data': merged, 'evidence': text, 'clear_fields': sorted(clear_fields)}]
        if len(updates) > 1 and any(u['action'] in domain.SENSITIVE | {'create', 'query', 'ask', 'unknown'} for u in updates):
            raise domain.Clarification('Revisemos una operación sensible a la vez. Indica primero su código CT.')
        replies = []
        for update in updates:
            action, data, evidence = update['action'], update['data'], update['evidence']
            if action == 'unknown':
                replies.append('Conservé la operación pendiente, sin ejecutarla. ¿Quieres confirmarla, corregirla o dejarla sin efecto?'
                               if conv.pending else 'Puedo ayudarte a coordinar solicitudes, versiones y avances de contenidos. ¿Qué necesitas gestionar?')
            elif action == 'create':
                domain.require(actor, 'journalist', 'coordinator')
                before = previous_draft if previous_draft is not None else conv.draft.copy()
                explicit_priority = re.search(r'\bprioridad\s*(?:es\s+|sera\s+|:\s*)?(normal|alta)\b', domain.clean(text))
                conv.draft.update({k: v for k, v in data.items() if k in QUESTIONS})
                if explicit_priority:
                    conv.draft['priority'] = {'normal': 'normal', 'alta': 'high'}[explicit_priority[1]]
                    conv.draft.pop('_priority_unspecified', None)
                for key in update.get('clear_fields', []):
                    conv.draft.pop(key, None)
                draft_controls(conv.draft, text)
                if conv.draft.get('_priority_unspecified'):
                    # No restaurar desde la IA un valor que el usuario acaba de retirar.
                    if not explicit_priority:
                        conv.draft.pop('priority', None)
                    else:
                        conv.draft.pop('_priority_unspecified', None)
                if conv.draft.get('_on_hold'):
                    replies.append('No la ingresé. Conservé el borrador en pausa, con el área encargada '
                                   f"{conv.draft.get('responsible_area', 'por definir')} y sin diseñador asignado. "
                                   'Escribe «revisar borrador» cuando quieras retomarlo.')
                    continue
                missing = next((k for k in QUESTIONS if not conv.draft.get(k)), None)
                if missing:
                    replies.append(QUESTIONS[missing])
                else:
                    domain.deadline(conv.draft['due_at'])
                    conv.pending = {'action': 'create', 'data': conv.draft.copy()}
                    if before == conv.draft:
                        conv.pending = {}
                        replies.append('El área encargada está registrada y el diseñador sigue sin asignar. '
                                       'No ingresé la solicitud. Escribe «revisar borrador» cuando quieras confirmar.')
                    else:
                        prefix = 'Actualicé el borrador.\n\n' if before else ''
                        replies.append(message_blocks('Revisa la solicitud',body=prefix+draft_summary(conv.draft)+'\n\n¿La ingreso así? Responde «confirmo» o corrige los datos.',kind='confirmation'))
            elif action == 'query':
                replies.append(self.query(actor, conv, text))
            elif action == 'ask':
                worker, tasks = domain.prepare_followup(actor, text, focus=conv.context.get('focus'))
                question = data.get('question') or text
                conv.pending = {'action': 'ask', 'worker': worker.pk, 'task_ids': [t.pk for t in tasks], 'question': question}
                codes = '; '.join(f'{t.request.code} · {t.request.title}' for t in tasks)
                replies.append(message_blocks(f'¿Confirmas que consulte a {worker.name}?',
                    [('Solicitud',codes),('Responsable',worker.name),('Pregunta',question)],
                    body='No envié nada todavía.',
                    next_step='Responde «confirmo» o indica la corrección.',kind='confirmation'))
            else:
                reference = focused_reference(evidence, action, conv.context)
                if action == 'progress' and not re.search(r'\bCT-\d+\b', evidence, re.I) and (data.get('reply_to_followup') or re.fullmatch(r'me faltan \d+ minutos[.! ]*',domain.clean(evidence))):
                    queries = ProgressQuery.objects.filter(worker=actor,answered_at__isnull=True,notification__status='sent', notification__sent_at__lte=message.created_at)
                    tasks = list(Task.objects.filter(progressquery__in=queries,assignee=actor).select_related('request').distinct())
                    if len(tasks) == 1:reference = tasks[0].request.code + ' ' + evidence
                    elif tasks:raise domain.Clarification('Hay más de una tarea consultada. Indica el código CT de tu avance; no cambié ninguna.')
                    elif queries.exists() or data.get('reply_to_followup'):
                        raise domain.Clarification('No hay una consulta entregada con una tarea que siga asignada a ti. Indica el CT; no apliqué el avance a otro trabajo.')
                try:
                    r = resolve(actor, reference, action)
                except ChooseTask as exc:
                    if len(updates) != 1:
                        raise domain.Clarification('Hay varias tareas posibles. Revisemos una actualización a la vez.')
                    exc.data = data.copy()
                    raise
                conv.context = {'focus': r.pk}
                if action == 'acknowledge':
                    task = domain.own_task(actor, r, 'design')
                    pending_refs = ({conv.pending['request_id']} if conv.pending.get('request_id') else
                                    set(Task.objects.filter(pk__in=conv.pending.get('task_ids',[])).values_list('request_id',flat=True)))
                    if pending_refs and pending_refs != {r.pk}:conv.pending['needs_review'] = True
                    replies.append(message_blocks('Asignación recibida', [('Solicitud',f'{r.code} · {r.title}'),
                        ('Responsable de diseño',actor.name),('Estado',r.get_state_display())],
                        'Avísame cuando comiences el diseño.' if task.state=='pending' else 'Puedes informar el avance de tu diseño.',kind='assignment'))
                    continue
                refs = versions_in(evidence)
                for key in ('version', 'new_version'):
                    if data.get(key):
                        parsed = versions_in(data[key])
                        data[key] = parsed[0] if len(parsed) == 1 else data[key]
                        if data[key] not in refs:
                            raise domain.Clarification('La versión interpretada no coincide con tu mensaje. Indica cuál quieres utilizar.')
                version_actions = {'approve','edit','receive','emit','corrections','submit'}
                if action in version_actions:
                    if action in {'approve','corrections','edit','receive','emit'}:
                        stage, role = {'approve':('review','reviewer'),'corrections':('review','reviewer'),
                                       'edit':('editing','editor'),'receive':('switch','switch'),'emit':('switch','switch')}[action]
                        domain.stage_task(actor, r, stage, role)  # Validación sin guardar ni asumir la etapa.
                    else:
                        domain.own_task(actor, r, 'design')
                    if not data.get('version') and len(refs) == 1:
                        data['version'] = refs[0]
                    if not data.get('version'):
                        if len(updates) != 1:
                            raise domain.Clarification('Falta identificar una versión. Revisemos un trabajo a la vez.')
                        conv.context['operation'] = {'action':action, 'request_id':r.pk, 'revision':r.revision,
                                                     'data':data.copy(), 'evidence':text, 'channel':message.channel}
                        return f'{r.code} · ¿Qué versión quieres ' + {'approve':'aprobar','edit':'incorporar a la nota','submit':'entregar para revisión',
                                  'corrections':'corregir','receive':'recibir en Switch','emit':'confirmar como emitida'}[action] + '?'
                if action in {'assign','assign_review','assign_edit'}:
                    data['assignee_id'] = domain.person(evidence).pk
                revision = update.get('_context_revision', r.revision)
                if action in domain.SENSITIVE:
                    # Verificar permisos y precondiciones ahora, sin guardar el ensayo.
                    with transaction.atomic():
                        domain.execute(actor, r.pk, action, data, revision=revision, confirmed=True, message=message)
                        transaction.set_rollback(True)
                    conv.pending = {'action': action, 'request_id': r.pk, 'revision': r.revision, 'data': data}
                    details = ', '.join(f'{dict(version="Versión",new_version="Nueva versión",progress="Corrección").get(k,k)}: {v}' for k, v in data.items() if k != 'assignee_id')
                    if action in {'assign','assign_review','assign_edit'}:
                        details = Participant.objects.get(pk=data['assignee_id']).name
                    replies.append(message_blocks('¿Confirmas esta asignación?' if action.startswith('assign') else 'Confirmación pendiente',
                        [('Solicitud',f'{r.code} · {r.title}'),('Responsable' if action.startswith('assign') else 'Detalle',details)],
                        body=f'¿Confirmas {LABELS[action]} {r.code}?',
                        next_step='Responde «confirmo» o indica la corrección.',kind='confirmation'))
                else:
                    r = domain.execute(actor, r.pk, action, data, revision=revision, message=message)
                    replies.append(operation_reply(r, action, data))
        if conv.pending and conv.pending != previous_pending:
            conv.pending.update(prompt_id=message.pk, channel=message.channel)
        return '\n'.join(replies)

    @staticmethod
    def query(actor, conv, text):
        if not re.search(r'\bCT-\d+\b', text, re.I) and conv.context.get('focus') and re.search(r'\b(esta tarea|este trabajo|esta solicitud)\b', domain.clean(text)):
            text += f" CT-{conv.context['focus']:06d}"
        answer, worker = read_query(actor, text)
        codes = {int(n) for n in re.findall(r'\bCT-0*(\d+)\b', text, re.I)}
        pending_ids = ({conv.pending['request_id']} if conv.pending.get('request_id') else
                       set(Task.objects.filter(pk__in=conv.pending.get('task_ids', [])).values_list('request_id', flat=True)))
        changed_scope = bool(codes and pending_ids and codes != pending_ids)
        replace_offer = (conv.pending.get('action') == 'ask' and worker and
                         (set(conv.pending.get('task_ids', [])) != set(worker.query_task_ids) or conv.pending.get('worker') != worker.pk))
        if changed_scope and conv.pending and not replace_offer:
            conv.pending['needs_review'] = True
            answer += '\n\nLa operación anterior se conserva, pero cambió el trabajo consultado. Revisa su CT antes de confirmarla.'
        if worker and (not conv.pending or replace_offer):
            conv.pending = {'action': 'ask', 'worker': worker.pk, 'task_ids':worker.query_task_ids,
                            'question': '¿Cómo vas con el trabajo y cuánto estimas que te falta?'}
            codes = ', '.join(
                t.request.code for t in Task.objects.filter(pk__in=worker.query_task_ids).select_related('request'))
            answer += f'\n\nNo tengo un avance detallado reciente. ¿Quieres que le consulte a {worker.name} por {codes} cómo va y cuánto estima que le falta?'
        codes = re.findall(r'\bCT-0*(\d+)\b', text, re.I)
        if len(codes) == 1 and domain.visible(actor).filter(pk=int(codes[0])).exists():
            if conv.context.get('operation', {}).get('request_id') != int(codes[0]):
                conv.context.pop('operation', None)
            conv.context['focus'] = int(codes[0])
        return answer


def operation_reply(r, action, data):
    fields = [('Solicitud',f'{r.code} · {r.title}')]
    if action in {'assign', 'assign_review', 'assign_edit'}:
        kind = {'assign_review':'review', 'assign_edit':'editing'}.get(action, 'design')
        task = r.tasks.select_related('assignee').get(kind=kind)
        return message_blocks(task.get_kind_display()+(' asignado' if kind=='design' else ' asignada'),
            fields+[('Responsable',task.assignee.name),('Estado',r.get_state_display())],body='La asignación quedó guardada.',kind='success')
    if action == 'start':
        return message_blocks('Diseño iniciado',fields+[('Estado',r.get_state_display())],
            body='Quedó registrado el inicio del diseño.',kind='success')
    if action == 'submit':
        return message_blocks('Versión entregada para revisión',fields+[('Versión',data['version']),('Estado',r.get_state_display())],
            body='La versión está lista para revisión. Falta la aprobación autorizada.',kind='success')
    if action == 'progress':
        fields.extend([('Avance de diseño',data['progress']),('Estado',r.get_state_display())])
        if data.get('eta_minutes'):fields.append(('Estimación informada',f'{data["eta_minutes"]} minutos.'))
        return message_blocks('Avance registrado',fields,kind='progress')
    title, body = {
        'approve':('Versión aprobada','Sigue Edición.'),
        'corrections':('Correcciones abiertas','Abrí las correcciones en la versión indicada; conservé la anterior.'),
        'edit':('Versión incorporada a la nota','Falta la recepción de Switch.'),
        'receive':('Recepción por Switch registrada','Switch recibió la versión indicada. La emisión sigue sin confirmar.'),
        'emit':('Emisión confirmada','La versión quedó registrada como emitida.'),
        'cancel':('Solicitud cancelada','La solicitud quedó cancelada; su historial se conserva.')
    }.get(action,('Estado de la solicitud',''))
    if data.get('version'):fields.append(('Versión',data['version']))
    if data.get('new_version'):fields.append(('Nueva versión',data['new_version']))
    fields.append(('Estado',r.get_state_display()))
    return message_blocks(title,fields,body=body,kind='emission' if action=='emit' else 'success')
