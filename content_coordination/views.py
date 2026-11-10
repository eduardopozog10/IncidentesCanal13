import uuid
from functools import wraps
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse, HttpResponseBadRequest
from django.db.models import Q
from django.shortcuts import get_object_or_404, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST
from django.utils import timezone

from .models import Participant, ContentRequest, Message, Outbox
from .processor import Processor, enqueue
from .services import visible
from .models import Conversation
from .presentation import history_cards, progress_steps, operational_panels
from .reporting import work_info
from .activity import last_activity


def member(view):
    @wraps(view)
    @login_required(login_url='contents:login')
    @never_cache
    def wrapped(request, *args, **kwargs):
        actor = Participant.objects.filter(user=request.user, active=True).first()
        if actor is None or not request.user.is_active:
            raise PermissionDenied('No tienes acceso a coordinación de contenidos.')
        request.content_actor = actor
        return view(request, *args, **kwargs)
    return wrapped


def page_context(request, pk=None):
    actor = request.content_actor
    qs = visible(actor)
    active = qs.exclude(state__in=['emitted', 'cancelled'])
    stats = [('Activas', active.count()), ('Pendientes', qs.filter(state__in=['pending', 'assigned']).count()),
             ('En producción', qs.filter(state='production').count()), ('En revisión', qs.filter(state='review').count()),
             ('Por vencer', active.filter(due_at__lte=timezone.now() + timedelta(minutes=20)).count()),
             ('Completadas', qs.filter(state='emitted').count())]
    state = request.GET.get('state', '')
    search = request.GET.get('q', '').strip()
    area = request.GET.get('area', '').strip()
    if state in dict(ContentRequest.State.choices):
        qs = qs.filter(state=state)
    if search:
        if search.upper().startswith('CT-') and search[3:].isdigit():
            qs = qs.filter(pk=int(search[3:]))
        else:
            qs = qs.filter(title__icontains=search)
    if area:
        qs = qs.filter(responsible_area=area)
    detail = None
    if pk is not None:
        detail = get_object_or_404(visible(actor).prefetch_related('tasks__assignee', 'versions__author', 'versions__approved_by', 'versions__edited_by', 'versions__received_by', 'versions__emitted_by', 'history__actor', 'history__message'), pk=pk)
    requests = list(qs.prefetch_related('tasks__assignee', 'versions','history').order_by('-updated_at')[:100])
    for row in requests:
        row.design = next((t for t in row.tasks.all() if t.kind == 'design'), None)
        row.last_activity = last_activity(row)
    notices = Outbox.objects.filter(recipient=actor)
    if actor.role in {'admin', 'coordinator'}:
        notices = Outbox.objects.filter(Q(recipient=actor) | Q(request__in=visible(actor)))
    notices = list(notices.filter(status__in=['blocked','failed','uncertain']).select_related('recipient','request').order_by('-pk')[:12])
    action_rows = list(visible(actor).exclude(state__in=['cancelled','emitted']).prefetch_related('tasks__assignee','versions').order_by('due_at','pk')[:100])
    actions, alerts = operational_panels(actor, action_rows, notices)
    info = work_info(detail) if detail else None
    if detail:
        actions, alerts = operational_panels(actor, [detail], [n for n in notices if n.request_id == detail.pk])
    conversation = Conversation.objects.filter(participant=actor).first()
    return {'actor': actor, 'stats': stats, 'rows': requests, 'detail': detail,
            'states': ContentRequest.State.choices, 'selected_state': state, 'search': search,
            'areas': visible(actor).order_by('responsible_area').values_list('responsible_area', flat=True).distinct(),
            'selected_area': area, 'nonce': str(uuid.uuid4()),
            'actions': actions, 'alerts': alerts, 'info': info,
            'last_activity':last_activity(detail) if detail else None,
            'progress_steps': progress_steps(detail, info) if detail else [],
            'history_cards': history_cards(detail.history.select_related('actor','message').order_by('-created_at','-pk')) if detail else [],
            'awaiting_confirmation': bool(conversation and conversation.pending and conversation.pending.get('action') != 'clarify'),
            'refreshed': timezone.localtime()}


@member
@require_GET
def dashboard(request, pk=None):
    return render(request, 'content_coordination/dashboard.html', page_context(request, pk))


@member
@require_GET
def fragment(request, pk=None):
    return render(request, 'content_coordination/data.html', page_context(request, pk))


@member
@require_POST
def send(request):
    try:
        nonce = str(uuid.UUID(request.POST.get('nonce', '')))
    except ValueError:
        return HttpResponseBadRequest('Identificador de mensaje inválido.')
    msg = enqueue(request.content_actor, f'web:{request.content_actor.pk}:{nonce}',
                  request.POST.get('text', ''), channel='dashboard')
    reply = Processor().process(msg)
    msg.refresh_from_db()
    return JsonResponse({'reply': reply, 'status': msg.status, 'nonce': str(uuid.uuid4())})
