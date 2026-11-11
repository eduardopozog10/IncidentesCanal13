"""Router determinista. Recepción atómica antes del offset; dominios independientes."""
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from incidents.models import ChannelMessage, IncidentDraft
from incidents.messaging.telegram_update_handler import TelegramUpdateHandler
from incidents.services.conversation_lock import conversation_lock
from incidents.services.telegram_message_groups import TelegramMessageGroups, TelegramGroupRunner
from content_coordination.models import Conversation, Message, Outbox, Participant, TelegramConsent, ParticipantAccessEvent
from content_coordination.processor import Processor, contextual_response
from content_coordination.routing import queue_update

from .authorization import command, capabilities, private_message, still_authorized, content_actor
from .models import RoutingSession, TelegramReceipt


SELECTORS = {'incidencias': 'incident', 'incidencia': 'incident', 'contenidos': 'content', 'contenido': 'content'}
LABELS = {'incident': 'Incidencias', 'content': 'Coordinación de Contenidos', '': 'Sin seleccionar'}


class RoutingBlocked(RuntimeError):
    """Revisión explícita requerida; nunca descartar o adivinar un destino."""


def check_legacy_pending():
    """No asignar retrospectivamente un módulo a mensajes históricos."""
    from incidents.services.incoming_message_service import IncomingMessageService
    content = list(Message.objects.filter(channel='telegram', status__in=['pending', 'retry', 'processing'],
                                          routing_receipt__isnull=True).values_list('pk', flat=True)[:20])
    incidents = list(IncomingMessageService.recoverable_messages().filter(provider='telegram',
                       routing_receipt__isnull=True).values_list('pk', flat=True)[:20])
    if content or incidents:
        raise RoutingBlocked(f'Pendientes históricos sin destino verificable: contenidos={content}, incidencias={incidents}. '
                             'Revisar su contexto; no se modificaron ni reprocesaron.')


class RoutedGroups(TelegramMessageGroups):
    def __init__(self, handler, bot_id):
        super().__init__(handler)
        self.bot_id = bot_id

    def _pending(self, reporter=None):
        return super()._pending(reporter).filter(routing_receipt__bot_id=self.bot_id,
                                                routing_receipt__destination='incident', routing_receipt__status='queued')

    def message_text(self, row):
        return '/cancelar' if command(row.content, settings.CONTENT_BOT_USERNAME) == 'cancelar' else row.content


class TelegramRouter:
    def __init__(self, bot_id, *, incident_handler=None, content_processor=None, transport=None,
                 commands_only=False, threaded=False):
        if not str(bot_id).isascii() or not str(bot_id).isdecimal() or int(bot_id) <= 0:
            raise ValueError('Se requiere el ID del bot verificado, no el token.')
        self.bot_id = str(bot_id)
        self._incident_handler = incident_handler
        self.processor = content_processor or Processor()
        self.transport = transport
        self.commands_only = commands_only
        self.threaded = threaded
        self.runner = None

    @property
    def incident_handler(self):
        if self._incident_handler is None:
            self._incident_handler = TelegramUpdateHandler()
        return self._incident_handler

    def receive(self, update):
        msg = private_message(update)
        cmd = command(msg['text'], settings.CONTENT_BOT_USERNAME)
        # Nunca admitir una identidad por pertenecer únicamente al otro módulo.
        with conversation_lock(f'telegram-router:{self.bot_id}'), transaction.atomic():
            auth = capabilities(update, inbound_start=cmd == 'start')
            if not auth:
                raise PermissionDenied('Cuenta no autorizada para ningún módulo; actualización conservada en Telegram.')
            existing = TelegramReceipt.objects.filter(bot_id=self.bot_id, update_id=update['update_id']).first()
            if existing:
                if existing.payload != update:
                    raise PermissionDenied('El update_id ya pertenece a otro contenido o identidad.')
                return existing
            session, _ = RoutingSession.objects.get_or_create(bot_id=self.bot_id,
                    sender_id=str(msg['from']['id']), chat_id=str(msg['chat']['id']))
            session = RoutingSession.objects.select_for_update().get(pk=session.pk)
            self.sync()
            reason, reply, destination = '', '', 'control'
            if session.last_update_id is not None and update['update_id'] <= session.last_update_id:
                destination, reason = 'blocked', 'OutOfOrderUpdate'
            elif cmd == 'start':
                actor = content_actor(update, inbound_start=True)
                if actor:
                    Participant.objects.select_for_update().get(pk=actor.pk)
                    actor = content_actor(update, inbound_start=True)
                    if actor is None:
                        raise PermissionDenied('El consentimiento cambió durante la recepción.')
                    consent = TelegramConsent.objects.select_for_update().filter(participant=actor, revoked_at__isnull=True).first()
                    if consent and consent.verified_at is None:
                        consent.verified_at = timezone.now()
                        consent.verified_update_id = update['update_id']
                        consent.save(update_fields=['verified_at', 'verified_update_id'])
                        ParticipantAccessEvent.objects.create(participant=actor, action='verified', details={'update_id': update['update_id']})
                auth = capabilities(update)
                reply = self.options(session, auth)
            elif cmd in SELECTORS:
                target = SELECTORS[cmd]
                if target not in auth:
                    reply = f'Tu cuenta no está autorizada para {LABELS[target]}. Se conserva el módulo: {LABELS[session.module]}.'
                elif target != session.module and session.receipts.filter(status__in=['queued', 'blocked'],
                                                    destination__in=['incident', 'content', 'blocked']).exists():
                    destination, reason = 'blocked', 'EarlierMessagePending'
                else:
                    if target != session.module:
                        boundary = timezone.now().isoformat()
                        if session.module:
                            session.resume_after = {**session.resume_after, session.module: boundary}
                        # También al seleccionar por primera vez tras instalar el
                        # router: puede existir un resumen histórico ya entregado.
                        session.resume_after = {**session.resume_after, target: boundary}
                        session.module = target
                    reply = f'Estás en {LABELS[target]}. Los borradores y operaciones de ambos módulos se conservan.'
                    if self.has_pending_context(target, auth):
                        reply += ' Revisa nuevamente el resumen antes de confirmar una operación anterior.'
            elif cmd == 'modulo':
                reply = self.options(session, auth)
            elif cmd is not None and not (cmd == 'cancelar' and session.module == 'incident'):
                reply = 'Comando no reconocido en este módulo. Usa /incidencias, /contenidos o /modulo.'
            elif not session.module:
                reply = self.options(session, auth)
            elif session.module not in auth:
                destination, reason = 'blocked', 'ModuleAuthorizationRevoked'
            elif self.commands_only:
                destination, reason = 'blocked', 'CommandsOnlyBusinessInput'
            elif self.confirmation_needs_review(session, update):
                reply = 'Conservé la operación anterior, sin ejecutarla. Revisa otra vez el resumen en este módulo antes de confirmar.'
            else:
                destination = session.module
            receipt = TelegramReceipt.objects.create(session=session, bot_id=self.bot_id,
                update_id=update['update_id'], payload=update, authorization=auth,
                destination=destination, status='blocked' if reason else 'queued', reason=reason)
            if destination == 'control':
                receipt.control_message = ChannelMessage.objects.create(provider='telegram',
                    external_message_id=f'router:{self.bot_id}:{update["update_id"]}', external_sender_id=session.sender_id,
                    external_chat_id=session.chat_id, direction='inbound', content=msg['text'], raw_payload=update,
                    received_via_polling=False, status='processed', processing_result={'action': 'routing', 'reply': reply})
                receipt.save(update_fields=['control_message'])
            elif destination == 'content':
                # El contexto de Contenidos nunca guarda la selección de transporte.
                actor = content_actor(update)
                Conversation.objects.get_or_create(participant=actor)
                receipt.content_message = queue_update(update)
                receipt.save(update_fields=['content_message'])
            elif destination == 'incident':
                # Entrada receive_only: persistencia y autorización originales, sin IA ni envío.
                result = self.incident_handler.incoming_service.process_text(provider='telegram',
                    external_user_id=session.sender_id, external_chat_id=session.chat_id,
                    external_message_id=f'{session.chat_id}:{msg["message_id"]}', text=msg['text'],
                    raw_payload=update, trusted_telegram=True, receive_only=True)
                receipt.incident_message = result['message']
                receipt.save(update_fields=['incident_message'])
            session.last_update_id = max(session.last_update_id or 0, update['update_id'])
            session.save(update_fields=['module', 'resume_after', 'last_update_id', 'updated_at'])
            return receipt

    @staticmethod
    def options(session, auth):
        available = []
        if 'incident' in auth:
            available.append('/incidencias')
        if 'content' in auth:
            available.append('/contenidos')
        return f'Módulo seleccionado: {LABELS[session.module]}. Opciones disponibles: {", ".join(available)}. Consulta con /modulo.'

    @staticmethod
    def has_pending_context(module, auth):
        if module == 'incident':
            return IncidentDraft.objects.filter(technician_id=auth[module]['technician_id'], status__in=['ready', 'collecting']).exists()
        conv = Conversation.objects.filter(participant_id=auth[module]['participant_id']).first()
        return bool(conv and (conv.draft or conv.pending or conv.context))

    def confirmation_needs_review(self, session, update):
        boundary = session.resume_after.get(session.module)
        if not boundary:
            return False
        since = parse_datetime(boundary)
        text = update['message']['text']
        if session.module == 'content':
            actor = content_actor(update)
            conv = Conversation.objects.filter(participant=actor).first()
            if not conv or contextual_response(text, conv) != 'confirm' or not conv.pending:
                return False
            prompt_id = conv.pending.get('prompt_id')
            return not Outbox.objects.filter(key=f'reply:{prompt_id}', status='sent',
                                            source__created_at__gte=since, sent_at__lte=timezone.now()).exists()
        account_id = capabilities(update)['incident']['channel_id']
        draft = IncidentDraft.objects.filter(technician__channels__pk=account_id, status='ready').first()
        processor = self.incident_handler.incoming_service.incident_processor
        if not draft or not processor._is_confirmation(text, draft):
            return False
        summaries = [draft.last_question, processor._answer_draft_query(draft, {'summary'})]
        return not ChannelMessage.objects.filter(draft=draft, provider='telegram', direction='outbound',
                status='sent', content__in=summaries, created_at__gte=since, processed_at__lte=timezone.now()).exists()

    def sync(self):
        from incidents.services.incoming_message_service import IncomingMessageService
        for receipt in TelegramReceipt.objects.filter(bot_id=self.bot_id, status='queued').exclude(destination='control'):
            if receipt.content_message_id:
                message = Message.objects.get(pk=receipt.content_message_id)
                if message.status in {'done', 'exhausted'}:
                    receipt.status = 'done'
                    receipt.reason = 'DomainExhausted' if message.status == 'exhausted' else ''
                    receipt.save(update_fields=['status', 'reason'])
            elif receipt.incident_message_id and not IncomingMessageService.recoverable_messages().filter(pk=receipt.incident_message_id).exists():
                receipt.status = 'done'
                receipt.save(update_fields=['status'])

    def deliver_controls(self):
        for receipt in TelegramReceipt.objects.filter(bot_id=self.bot_id, destination='control', status='queued').select_related('control_message', 'session').order_by('pk')[:20]:
            with conversation_lock(f'router-control:{receipt.pk}'):
                receipt.refresh_from_db()
                if receipt.status != 'queued':
                    continue
                if not any(still_authorized(receipt, module) for module in receipt.authorization):
                    receipt.status, receipt.reason = 'blocked', 'ControlAuthorizationRevoked'
                else:
                    # Reutiliza acuses, multipartes y tratamiento de envíos inciertos.
                    handler = TelegramUpdateHandler(messaging_provider=self.transport, incoming_service=object())
                    outgoing, delivered = handler._deliver_reply(receipt.control_message,
                                receipt.control_message.processing_result['reply'], purpose='routing')
                    if delivered or outgoing.status == 'uncertain' or outgoing.processing_attempts >= 3:
                        receipt.status = 'done'
                        receipt.reason = '' if delivered else 'ControlDeliveryUncertainOrFailed'
                receipt.save(update_fields=['status', 'reason'])

    def tick(self):
        blocked = TelegramReceipt.objects.filter(bot_id=self.bot_id, status='blocked').first()
        if blocked:
            raise RoutingBlocked(f'Actualización {blocked.update_id} detenida: {blocked.reason}. No se descartó ni cambió de destino.')
        self.deliver_controls()
        if self.commands_only:
            return
        for receipt in TelegramReceipt.objects.filter(bot_id=self.bot_id, destination__in=['content', 'incident'], status='queued').select_related('content_message').order_by('pk')[:20]:
            if not still_authorized(receipt, receipt.destination):
                receipt.status, receipt.reason = 'blocked', 'AuthorizationChangedBeforeDispatch'
                receipt.save(update_fields=['status', 'reason'])
                raise RoutingBlocked(f'Actualización {receipt.update_id}: autorización cambió; requiere revisión.')
            if receipt.destination == 'content':
                Conversation.objects.filter(participant=receipt.content_message.participant).update(module='content')
                self.processor.process(receipt.content_message)
        if TelegramReceipt.objects.filter(bot_id=self.bot_id, destination='incident', status='queued').exists():
            groups = RoutedGroups(self.incident_handler, self.bot_id)
            if self.threaded:
                if self.runner is None:
                    self.runner = TelegramGroupRunner(self.incident_handler, getattr(settings, 'TELEGRAM_PROCESSING_WORKERS', 2))
                    self.runner.groups = groups
                self.runner.tick()
            else:
                for reporter in groups.ready_reporters():
                    groups.process_reporter(reporter)
        self.sync()

    def close(self):
        if self.runner:
            self.runner.close()
