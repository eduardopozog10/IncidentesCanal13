"""Transporte e IA simulados; exclusivamente PostgreSQL temporal de tests."""
from copy import deepcopy
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from django.db import close_old_connections
import httpx

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, SimpleTestCase, override_settings
from django.utils import timezone

from incidents.ai.schemas import IncidentExtractionResult
from incidents.models import ChannelMessage, Incident, IncidentDraft, TechnicianChannel
from incidents.test_qa_extreme import handler_fixture
from incidents.test_natural_confirmation import DATA, INITIAL
from content_coordination.models import (Conversation, Message, Participant, TelegramConsent,
    TelegramLinkTransfer, ContentRequest, Task, ContentVersion, History, Outbox, ParticipantAccessEvent)
from content_coordination.processor import Processor
from content_coordination.tests import Fixture
from .models import RoutingSession, TelegramReceipt
from .router import TelegramRouter, RoutingBlocked, check_legacy_pending
from .receiver_lock import receiver_lock


@override_settings(DEBUG=False, TELEGRAM_AUTO_REGISTER=False, TELEGRAM_GROUP_WINDOW_SECONDS=0,
                   PILOT_MESSAGE_RETRY_SECONDS=0, CONTENT_LIVE_TELEGRAM=True)
class RoutingTests(Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.identity = 6690350935
        self.reporter, self.ai, self.transport, self.handler = handler_fixture(self.identity)
        self.ai.extract_incident.return_value = IncidentExtractionResult(**deepcopy(DATA))
        self.coordinator.telegram_id = str(self.identity)
        self.coordinator.telegram_authorized = True
        self.coordinator.save()
        TelegramConsent.objects.create(participant=self.coordinator, telegram_id=str(self.identity),
                                      consent_at=timezone.now(), verified_at=timezone.now())
        self.interpreter = Mock()
        self.interpreter.interpret.side_effect = lambda text, context: {'updates': [{'action': 'query', 'data': {}, 'evidence': text}]}
        self.processor = Processor(self.interpreter)
        self.number = 0
        self.router = self.new_router()

    def new_router(self, **kwargs):
        return TelegramRouter(777, incident_handler=self.handler, content_processor=self.processor,
                              transport=self.transport, **kwargs)

    def update(self, text, number=None):
        self.number = number if number is not None else self.number + 1
        return {'update_id': self.number, 'message': {'message_id': self.number,
            'from': {'id': self.identity}, 'chat': {'id': self.identity, 'type': 'private'},
            'date': int(timezone.now().timestamp()), 'text': text}}

    def send(self, text, *, tick=True):
        receipt = self.router.receive(self.update(text))
        if tick:
            self.router.tick()
        receipt.refresh_from_db()
        return receipt

    def selected(self):
        return RoutingSession.objects.get(bot_id='777').module

    def test_both_authorizations_select_independently_without_business_or_ai(self):
        self.send('/incidencias'); self.assertEqual(self.selected(), 'incident')
        self.send('/contenidos'); self.assertEqual(self.selected(), 'content')
        self.assertFalse(Incident.objects.exists()); self.assertFalse(ContentRequest.objects.exists())
        self.ai.extract_incident.assert_not_called(); self.interpreter.interpret.assert_not_called()
        self.assertFalse(Message.objects.exists())

    def test_incident_only_does_not_require_content_participant_or_consent(self):
        self.coordinator.telegram_id = None; self.coordinator.save()
        self.send('/incidencias'); self.send(INITIAL)
        self.assertEqual(IncidentDraft.objects.get().technician, self.reporter)
        self.assertFalse(Message.objects.exists())
        self.send('/contenidos'); self.assertEqual(self.selected(), 'incident')

    def test_content_only_cannot_gain_incident_authorization(self):
        TechnicianChannel.objects.all().update(is_active=False)
        self.send('/contenidos'); self.send('/incidencias')
        self.assertEqual(self.selected(), 'content')
        self.send('¿Qué solicitudes están pendientes?')
        self.assertEqual(Message.objects.count(), 1)
        self.ai.extract_incident.assert_not_called()

    def test_no_authorization_has_no_receipt_session_reply_or_autoregistration(self):
        self.identity = 8888
        with self.assertRaises(PermissionDenied): self.send('/incidencias')
        self.assertFalse(TelegramReceipt.objects.exists()); self.assertFalse(RoutingSession.objects.exists())
        self.transport.send_text.assert_not_called()
        self.assertEqual(TechnicianChannel.objects.count(), 1)

    def test_private_identity_and_unsupported_envelopes_are_rejected(self):
        for change in ({'chat': {'id': -1, 'type': 'group'}}, {'chat': {'id': 555, 'type': 'private'}},
                       {'from': {'id': self.identity, 'is_bot': True}}, {'text': ''}, {'message_id': None}):
            update = self.update('/start'); update['message'].update(change)
            with self.subTest(change=change), self.assertRaises(PermissionDenied): self.router.receive(update)
        update = self.update('/start'); update['edited_message'] = {}
        with self.assertRaises(PermissionDenied): self.router.receive(update)
        self.assertFalse(TelegramReceipt.objects.exists())

    def test_aliases_repeated_selection_and_bot_command_suffix(self):
        for text in ('/incidencia', '/incidencias', '/incidencias@secuoyachile_incidentes_bot'):
            self.send(text); self.assertEqual(self.selected(), 'incident')
        for text in ('/contenido', '/contenidos'):
            self.send(text); self.assertEqual(self.selected(), 'content')
        self.ai.extract_incident.assert_not_called(); self.interpreter.interpret.assert_not_called()

    def test_unknown_commands_arguments_and_other_bots_never_reach_agents(self):
        self.send('/contenidos')
        for text in ('/aprobar CT-000005', '/contenidos borrar', '/incidencias@otro_bot', '/desconocido'):
            self.send(text); self.assertEqual(self.selected(), 'content')
        self.ai.extract_incident.assert_not_called(); self.interpreter.interpret.assert_not_called()
        self.assertFalse(Message.objects.exists())

    def test_no_selection_does_not_infer_from_incident_or_content_text(self):
        self.send(INITIAL)
        self.assertEqual(self.selected(), '')
        self.ai.extract_incident.assert_not_called(); self.interpreter.interpret.assert_not_called()
        self.assertFalse(IncidentDraft.objects.exists()); self.assertFalse(Message.objects.exists())

    def test_start_and_module_query_preserve_all_context_and_selection(self):
        self.send('/contenidos')
        conv = Conversation.objects.create(participant=self.coordinator, module='content',
                    draft={'title': 'Encuesta'}, pending={'action': 'create'}, context={'focus': 5})
        before = (conv.draft, conv.pending, conv.context)
        self.send('/start'); self.send('/modulo')
        conv.refresh_from_db(); self.assertEqual((conv.draft, conv.pending, conv.context), before)
        self.assertEqual(self.selected(), 'content')
        self.ai.extract_incident.assert_not_called(); self.interpreter.interpret.assert_not_called()

    def test_fresh_private_start_verifies_without_changing_content_context(self):
        consent = TelegramConsent.objects.get(participant=self.coordinator)
        consent.verified_at = None; consent.bound_at = timezone.now() - timedelta(seconds=10); consent.save()
        conv = Conversation.objects.create(participant=self.coordinator, draft={'title': 'Encuesta'}, pending={'action': 'ask'})
        update = self.update('/start'); self.router.receive(update); self.router.tick()
        self.router.receive(update); self.router.tick()
        consent.refresh_from_db(); self.assertIsNotNone(consent.verified_at)
        self.assertEqual(ParticipantAccessEvent.objects.filter(action='verified').count(), 1)
        conv.refresh_from_db(); self.assertEqual(conv.pending, {'action': 'ask'})
        self.assertFalse(Outbox.objects.exists())

    def test_unverified_content_cannot_be_selected_through_incident_authorization(self):
        TelegramConsent.objects.filter(participant=self.coordinator).update(verified_at=None)
        self.send('/contenidos'); self.assertEqual(self.selected(), '')
        self.send('/incidencias'); self.assertEqual(self.selected(), 'incident')

    def test_old_start_does_not_verify_after_binding_changed(self):
        consent = TelegramConsent.objects.get(participant=self.coordinator)
        consent.verified_at = None; consent.bound_at = timezone.now() + timedelta(seconds=10); consent.save()
        self.send('/start')
        consent.refresh_from_db(); self.assertIsNone(consent.verified_at)
        self.assertFalse(ParticipantAccessEvent.objects.exists())

    def test_selector_followed_by_input_in_one_batch_uses_new_destination(self):
        first = self.router.receive(self.update('/incidencias'))
        second = self.router.receive(self.update(INITIAL))
        self.assertEqual(first.destination, 'control'); self.assertEqual(second.destination, 'incident')
        self.router.tick()
        self.assertTrue(IncidentDraft.objects.exists()); self.assertFalse(Message.objects.exists())

    def test_duplicate_update_and_old_selector_never_reapply_selection_or_reply(self):
        update = self.update('/incidencias')
        first = self.router.receive(update); self.router.tick()
        self.send('/contenidos')
        count = self.transport.send_text.call_count
        self.assertEqual(self.router.receive(update).pk, first.pk); self.router.tick()
        self.assertEqual(self.selected(), 'content'); self.assertEqual(self.transport.send_text.call_count, count)

    def test_duplicate_id_with_changed_payload_is_rejected(self):
        update = self.update('/incidencias'); self.router.receive(update)
        changed = deepcopy(update); changed['message']['text'] = '/contenidos'
        with self.assertRaises(PermissionDenied): self.router.receive(changed)
        self.assertEqual(TelegramReceipt.objects.count(), 1)

    def test_out_of_order_update_is_durable_and_stops_without_dispatch(self):
        self.router.receive(self.update('/incidencias', 20)); self.router.tick()
        receipt = self.router.receive(self.update(INITIAL, 19))
        self.assertEqual(receipt.status, 'blocked'); self.assertEqual(receipt.reason, 'OutOfOrderUpdate')
        with self.assertRaises(RoutingBlocked): self.router.tick()
        self.assertFalse(IncidentDraft.objects.exists()); self.ai.extract_incident.assert_not_called()

    def test_pending_business_blocks_switch_without_dropping_or_rerouting(self):
        self.send('/incidencias')
        original = self.send(INITIAL, tick=False)
        blocked = self.send('/contenidos', tick=False)
        self.assertEqual(blocked.reason, 'EarlierMessagePending')
        self.assertEqual(self.selected(), 'incident')
        original.refresh_from_db(); self.assertEqual(original.destination, 'incident')
        self.assertEqual(ChannelMessage.objects.get(pk=original.incident_message_id).content, INITIAL)

    def test_destination_and_payload_cannot_be_mutated_through_model(self):
        receipt = self.send('/incidencias')
        receipt.destination = 'content'
        with self.assertRaises(ValidationError): receipt.save()

    def test_unique_session_and_receipt_are_database_constraints(self):
        receipt = self.send('/incidencias')
        with self.assertRaises(IntegrityError), transaction.atomic():
            RoutingSession.objects.create(bot_id='777', sender_id=str(self.identity), chat_id=str(self.identity))
        with self.assertRaises(IntegrityError), transaction.atomic():
            TelegramReceipt.objects.create(session=receipt.session, bot_id='777', update_id=receipt.update_id,
                                           destination='content', payload=receipt.payload)

    def test_cancelar_reaches_incident_command_and_keeps_content_draft(self):
        conv = Conversation.objects.create(participant=self.coordinator, draft={'title': 'Encuesta'})
        self.send('/incidencias'); self.send(INITIAL)
        self.send('/cancelar')
        self.assertEqual(IncidentDraft.objects.get().status, 'cancelled')
        conv.refresh_from_db(); self.assertEqual(conv.draft, {'title': 'Encuesta'})
        self.assertEqual(self.ai.extract_incident.call_count, 1)

    def test_cancelar_with_bot_suffix_keeps_original_and_never_calls_ai_for_command(self):
        self.send('/incidencias'); self.send(INITIAL)
        original = '  /cancelar@secuoyachile_incidentes_bot  '
        receipt = self.send(original)
        self.assertEqual(IncidentDraft.objects.get().status, 'cancelled')
        self.assertEqual(receipt.incident_message.content, original)
        self.assertEqual(self.ai.extract_incident.call_count, 1)

    def test_both_drafts_survive_repeated_switches(self):
        conv = Conversation.objects.create(participant=self.coordinator, draft={'title': 'Encuesta'},
                                          pending={'action': 'create'}, context={'focus': 5})
        self.send('/incidencias'); self.send(INITIAL)
        draft = IncidentDraft.objects.get(); before = deepcopy(draft.extracted_data)
        for text in ('/contenidos', '/incidencias', '/contenidos'):
            self.send(text)
        draft.refresh_from_db(); conv.refresh_from_db()
        self.assertEqual(draft.extracted_data, before)
        self.assertEqual(conv.draft, {'title': 'Encuesta'}); self.assertEqual(conv.pending, {'action': 'create'})
        self.assertEqual(conv.context, {'focus': 5})

    def test_old_incident_confirmation_is_blocked_until_fresh_summary(self):
        self.send('/incidencias'); self.send(INITIAL)
        self.send('/contenidos'); self.send('/incidencias')
        receipt = self.send('Sí, confirmo la creación del incidente.')
        self.assertEqual(receipt.destination, 'control'); self.assertFalse(Incident.objects.exists())
        self.send('Muéstrame el resumen del reporte')
        self.send('Sí, confirmo la creación del incidente.')
        self.assertEqual(Incident.objects.count(), 1)

    def test_first_selection_cannot_confirm_an_old_pre_router_summary(self):
        self.handler.handle(self.update(INITIAL))
        self.assertEqual(IncidentDraft.objects.get().status, 'ready')
        self.send('/incidencias')
        self.assertEqual(self.send('Sí, confirmo').destination, 'control')
        self.assertFalse(Incident.objects.exists())

    def test_old_content_confirmation_cannot_execute_after_switch(self):
        self.send('/contenidos')
        prompt = Message.objects.create(participant=self.coordinator, external_id='fixture-summary', text='resumen', status='done')
        Conversation.objects.create(participant=self.coordinator, module='content', draft={'title': 'Encuesta'},
            pending={'action': 'create', 'prompt_id': prompt.pk, 'channel': 'telegram', 'data': {'title': 'Encuesta'}})
        Outbox.objects.create(recipient=self.coordinator, source=prompt, key=f'reply:{prompt.pk}', text='Resumen', status='sent', sent_at=timezone.now())
        self.send('/incidencias'); self.send('/contenidos')
        receipt = self.send('Sí, confirmo')
        self.assertEqual(receipt.destination, 'control'); self.assertFalse(ContentRequest.objects.exists())
        self.interpreter.interpret.assert_not_called()

    def test_contextual_content_text_never_reaches_incident_processor(self):
        self.send('/contenidos'); receipt = self.send('¿Qué solicitudes están pendientes?')
        self.assertEqual(receipt.destination, 'content'); self.assertIsNotNone(receipt.content_message_id)
        self.assertIsNone(receipt.incident_message_id); self.ai.extract_incident.assert_not_called()

    def test_restart_before_dispatch_recovers_same_incident_once(self):
        self.send('/incidencias'); receipt = self.send(INITIAL, tick=False)
        restarted = self.new_router(); restarted.tick(); restarted.tick()
        receipt.refresh_from_db(); self.assertEqual(receipt.status, 'done')
        self.assertEqual(self.ai.extract_incident.call_count, 1)
        self.assertEqual(IncidentDraft.objects.count(), 1)

    def test_persisted_destination_does_not_depend_on_current_session_module(self):
        self.send('/incidencias'); receipt = self.send(INITIAL, tick=False)
        # Simula un estado posterior al reinicio; el destino ya confirmado manda.
        RoutingSession.objects.update(module='content')
        self.new_router().tick()
        receipt.refresh_from_db(); self.assertEqual(receipt.destination, 'incident')
        self.assertEqual(IncidentDraft.objects.count(), 1); self.assertFalse(Message.objects.exists())

    def test_restart_after_content_commit_does_not_reinterpret_or_lose_target(self):
        self.send('/contenidos'); receipt = self.send('¿Qué solicitudes están pendientes?', tick=False)
        Conversation.objects.filter(participant=self.coordinator).update(module='content')
        self.processor.process(receipt.content_message)
        before = self.interpreter.interpret.call_count
        self.new_router().tick(); self.assertEqual(self.interpreter.interpret.call_count, before)
        receipt.refresh_from_db(); self.assertEqual(receipt.status, 'done'); self.assertEqual(receipt.destination, 'content')

    def test_receipt_and_domain_enqueue_rollback_together(self):
        self.send('/incidencias'); update = self.update(INITIAL)
        with patch.object(self.handler.incoming_service, 'process_text', side_effect=RuntimeError('DB interruption')):
            with self.assertRaises(RuntimeError): self.router.receive(update)
        self.assertFalse(TelegramReceipt.objects.filter(update_id=update['update_id']).exists())
        receipt = self.router.receive(update); self.router.tick()
        self.assertEqual(receipt.destination, 'incident'); self.assertEqual(IncidentDraft.objects.count(), 1)

    def test_revoked_content_does_not_dispatch_via_incident_authorization(self):
        self.send('/contenidos'); receipt = self.send('¿Qué solicitudes están pendientes?', tick=False)
        TelegramConsent.objects.filter(participant=self.coordinator).update(revoked_at=timezone.now())
        with self.assertRaises(RoutingBlocked): self.router.tick()
        receipt.refresh_from_db(); self.assertEqual(receipt.status, 'blocked')
        self.ai.extract_incident.assert_not_called(); self.interpreter.interpret.assert_not_called()

    def test_changed_content_binding_cannot_process_old_receipt(self):
        self.send('/contenidos'); receipt = self.send('¿Qué solicitudes están pendientes?', tick=False)
        TelegramConsent.objects.filter(participant=self.coordinator).update(bound_at=timezone.now())
        with self.assertRaises(RoutingBlocked): self.router.tick()
        receipt.refresh_from_db(); self.assertEqual(receipt.reason, 'AuthorizationChangedBeforeDispatch')

    def test_incident_channel_revoked_after_receive_is_not_processed(self):
        self.send('/incidencias'); self.send(INITIAL, tick=False)
        TechnicianChannel.objects.all().update(is_active=False)
        with self.assertRaises(RoutingBlocked): self.router.tick()
        self.ai.extract_incident.assert_not_called()

    def test_uncertain_control_delivery_is_not_resent_after_restart(self):
        receipt = self.send('/incidencias', tick=False)
        self.transport.send_text.side_effect = TimeoutError('accepted without acknowledgement')
        self.router.tick(); self.new_router().tick()
        self.assertEqual(self.transport.send_text.call_count, 1)
        receipt.refresh_from_db(); self.assertIn('Uncertain', receipt.reason)

    def test_commands_only_never_processes_business_or_historical_notifications(self):
        self.router = self.new_router(commands_only=True)
        self.send('/incidencias'); self.send('/modulo'); self.send('/contenidos')
        old = Outbox.objects.create(recipient=self.coordinator, key='historical', text='Histórico')
        receipt = self.send(INITIAL, tick=False)
        with self.assertRaises(RoutingBlocked): self.router.tick()
        self.assertEqual(receipt.reason, 'CommandsOnlyBusinessInput')
        self.ai.extract_incident.assert_not_called(); self.interpreter.interpret.assert_not_called()
        old.refresh_from_db(); self.assertEqual(old.status, 'pending')

    @override_settings(TELEGRAM_GROUP_WINDOW_SECONDS=2.5)
    def test_incident_grouping_preserves_original_messages_and_extracts_once(self):
        self.send('/incidencias')
        clock = timezone.now()
        with patch('django.utils.timezone.now', return_value=clock):
            for text in ('La cámara 2 falla en Estudio A.', 'La imagen se va a negro.', 'No podemos seguir las pruebas.', 'Prioridad alta.'):
                self.send(text, tick=False)
            self.router.tick(); self.ai.extract_incident.assert_not_called()
        with patch('django.utils.timezone.now', return_value=clock + timedelta(seconds=5)):
            self.router.tick()
        self.assertEqual(self.ai.extract_incident.call_count, 1)
        self.assertEqual(TelegramReceipt.objects.filter(destination='incident').count(), 4)
        self.assertEqual(ChannelMessage.objects.filter(direction='inbound', received_via_polling=True).count(), 4)

    def test_historical_pending_is_read_only_and_never_guessed(self):
        Message.objects.create(participant=self.coordinator, external_id='telegram:old', text='Sí, confirma', payload={})
        before = list(Message.objects.values())
        with self.assertRaises(RoutingBlocked): check_legacy_pending()
        self.assertEqual(list(Message.objects.values()), before)
        self.ai.extract_incident.assert_not_called(); self.interpreter.interpret.assert_not_called()

    def test_transfer_and_ct5_are_unchanged_and_no_admin_privilege_is_inherited(self):
        source = self.actors['Admin']
        transfer = TelegramLinkTransfer.objects.create(id='bdf62dea-4d35-474e-8fad-5cebf91215b8',
            source=source, target=self.coordinator, telegram_id=str(self.identity), operator=source,
            source_snapshot={'role': 'admin'}, target_snapshot={'role': 'coordinator'})
        request = ContentRequest.objects.create(pk=5, requester=self.journalist, title='Encuesta', description='Datos sintéticos',
                    requesting_area='Prensa', responsible_area='Gráfica', state='review')
        Task.objects.create(request=request, kind='review', assignee=self.reviewer)
        ContentVersion.objects.create(request=request, number='1.0', state='review', author=self.worker)
        models = [Participant, TelegramConsent, TelegramLinkTransfer, ContentRequest, Task, ContentVersion, History]
        before = [list(model.objects.order_by('pk').values()) for model in models]
        for text in ('/incidencias', '/contenidos', '/start', '/modulo'):
            self.send(text)
        self.assertEqual([list(model.objects.order_by('pk').values()) for model in models], before)
        transfer.refresh_from_db(); self.assertEqual(transfer.state, 'active')
        self.send('/incidencias'); self.send(INITIAL)
        self.assertEqual(IncidentDraft.objects.get().technician, self.reporter)
        self.assertEqual([list(model.objects.order_by('pk').values()) for model in models], before)
        with self.assertRaises(PermissionDenied):
            from content_coordination.services import execute
            execute(self.coordinator, request.pk, 'approve', {'version': '1.0'}, confirmed=True)

    def test_content_notification_delivery_does_not_change_incident_selection(self):
        from content_coordination.delivery import deliver_one
        self.send('/incidencias')
        out = Outbox.objects.create(recipient=self.coordinator, key='test:new-notice', text='CT-000005 requiere seguimiento')
        deliver_one(self.transport)
        out.refresh_from_db(); self.assertEqual(out.status, 'sent')
        self.assertEqual(self.selected(), 'incident')
        self.assertEqual(self.transport.send_text.call_args.args[0], str(self.identity))
        deliver_one(self.transport); self.assertEqual(Outbox.objects.count(), 1)

    def test_transient_incident_failure_recovers_original_destination_once(self):
        from incidents.ai.base import AITemporaryError
        self.send('/incidencias')
        self.ai.extract_incident.side_effect = [AITemporaryError('simulado', retry_after=0), IncidentExtractionResult(**DATA)]
        receipt = self.send(INITIAL)
        self.assertEqual(receipt.status, 'queued')
        self.new_router().tick(); self.new_router().tick()
        receipt.refresh_from_db(); self.assertEqual(receipt.status, 'done'); self.assertEqual(receipt.destination, 'incident')
        self.assertEqual(IncidentDraft.objects.count(), 1); self.assertEqual(self.ai.extract_incident.call_count, 2)

    def invoke_receiver(self, batch, *, commands_only=False, pending=None, accepted='', before_poll=None):
        module = 'content_coordination.management.commands.run_secuoya_demo'
        http = Mock()
        replies = iter([
            {'ok': True, 'result': {'id': 777, 'username': 'secuoyachile_incidentes_bot'}},
            {'ok': True, 'result': {'url': ''}}, {'ok': True, 'result': pending or []},
            {'ok': True, 'result': batch},
        ])
        def post(url, *, json):
            try:
                body = next(replies)
            except StopIteration:
                if before_poll: before_poll(json)
                raise KeyboardInterrupt()
            return httpx.Response(200, json=body)
        http.post.side_effect = post
        out = StringIO()
        with patch(module + '.ensure_demo'), patch(module + '.check_other_receivers'), \
                patch(module + '.receiver_lock', return_value=nullcontext()), patch(module + '.httpx.Client') as client, \
                patch(module + '.TelegramRouter', side_effect=lambda bot, **kw: self.new_router(commands_only=kw['commands_only'])), \
                patch(module + '.deliver_one', return_value=None) as delivery, \
                patch(module + '.schedule_reminders') as reminders, patch(module + '.drain_notifications') as mail, \
                patch(module + '.recover_delivery_interruptions') as recovery:
            client.return_value.__enter__.return_value = http
            call_command('run_secuoya_demo', live=True, commands_only=commands_only, accept_pending_ids=accepted, stdout=out)
        return out.getvalue(), http, delivery, reminders, mail, recovery

    def test_combined_receiver_commands_only_uses_router_without_ai_or_history(self):
        batch = [self.update(text) for text in ('/start', '/incidencias', '/contenidos', '/modulo')]
        output, http, delivery, reminders, mail, recovery = self.invoke_receiver(batch, commands_only=True)
        self.assertIn('comandos solamente', output); self.assertIn('DETENIDO', output)
        self.assertEqual(self.selected(), 'content'); self.assertEqual(TelegramReceipt.objects.count(), 4)
        self.ai.extract_incident.assert_not_called(); self.interpreter.interpret.assert_not_called()
        for effect in (delivery, reminders, mail, recovery): effect.assert_not_called()

    def test_combined_receiver_persists_before_offset_and_runs_incident_backend(self):
        batch = [self.update('/incidencias'), self.update(INITIAL)]
        def persisted(payload):
            self.assertEqual(payload['offset'], batch[-1]['update_id'] + 1)
            self.assertEqual(TelegramReceipt.objects.filter(destination='incident').count(), 1)
            self.assertEqual(IncidentDraft.objects.count(), 1)
        self.invoke_receiver(batch, before_poll=persisted)
        self.assertEqual(self.ai.extract_incident.call_count, 1)

    def test_combined_receiver_unreviewed_pending_blocks_before_queue_or_offset(self):
        update = self.update('/contenidos')
        with self.assertRaisesMessage(CommandError, 'Revisa el contexto'):
            self.invoke_receiver([], pending=[update])
        self.assertFalse(TelegramReceipt.objects.exists()); self.assertFalse(Message.objects.exists())

    def test_combined_receiver_legacy_pending_blocks_without_processing(self):
        Message.objects.create(participant=self.coordinator, external_id='telegram:legacy', text='Confirmo')
        with self.assertRaisesMessage(CommandError, 'Pendientes históricos'):
            self.invoke_receiver([])
        self.ai.extract_incident.assert_not_called(); self.interpreter.interpret.assert_not_called()

    def test_combined_receiver_does_not_ack_unsafe_input_after_selector(self):
        batch = [self.update('/incidencias'), self.update(INITIAL)]
        batch[-1]['message']['chat']['type'] = 'group'
        with self.assertRaisesMessage(CommandError, 'no autorizada'):
            self.invoke_receiver(batch)
        self.assertEqual(TelegramReceipt.objects.count(), 1)
        self.ai.extract_incident.assert_not_called()

    def test_pending_inspection_supports_incident_only_without_writes(self):
        from content_coordination.telegram_polling import inspect_pending
        self.coordinator.telegram_id = None; self.coordinator.save()
        update = self.update('/incidencias')
        output = inspect_pending([update], update['update_id'], bot_id='777')
        self.assertIn('"authorized": true', output)
        self.assertFalse(TelegramReceipt.objects.exists()); self.assertFalse(RoutingSession.objects.exists())

    def test_new_content_summary_allows_confirmation_after_return(self):
        from content_coordination.tests import DATA as CONTENT_DATA
        self.send('/contenidos')
        conv = Conversation.objects.create(participant=self.coordinator, module='content', draft=deepcopy(CONTENT_DATA))
        self.send('revisar borrador')
        from content_coordination.delivery import deliver_one
        deliver_one(self.transport)
        self.send('/incidencias'); self.send('/contenidos')
        self.assertEqual(self.send('Sí, confirmo').destination, 'control')
        self.send('revisar borrador'); deliver_one(self.transport)
        self.send('Sí, confirmo')
        self.assertEqual(ContentRequest.objects.count(), 1)

    def test_scope_distinguishes_bots_without_reusing_selection(self):
        self.send('/incidencias')
        other = TelegramRouter(778, transport=self.transport)
        receipt = other.receive(self.update('/modulo'))
        self.assertEqual(receipt.session.module, '')
        self.assertEqual(RoutingSession.objects.count(), 2)

    def test_default_commands_only_never_constructs_any_ai_provider(self):
        with patch('content_coordination.processor.GeminiContentInterpreter', side_effect=AssertionError('IA prohibida')), \
                patch('incidents.services.incident_processor.get_ai_provider', side_effect=AssertionError('IA prohibida')):
            router = TelegramRouter(777, transport=self.transport, commands_only=True)
            for text in ('/start', '/incidencias', '/contenidos', '/modulo'):
                router.receive(self.update(text)); router.tick()

    def test_crash_after_domain_insert_before_receipt_reference_rolls_back(self):
        self.send('/incidencias'); update = self.update(INITIAL)
        original_save = TelegramReceipt.save
        def interrupted(receipt, *args, **kwargs):
            if kwargs.get('update_fields') == ['incident_message']:
                raise RuntimeError('Corte antes de enlazar el inbox')
            return original_save(receipt, *args, **kwargs)
        with patch.object(TelegramReceipt, 'save', interrupted):
            with self.assertRaises(RuntimeError): self.router.receive(update)
        self.assertFalse(ChannelMessage.objects.filter(received_via_polling=True).exists())
        self.assertFalse(TelegramReceipt.objects.filter(update_id=update['update_id']).exists())
        self.router.receive(update); self.router.tick()
        self.assertEqual(IncidentDraft.objects.count(), 1)


@override_settings(DEBUG=False, TELEGRAM_AUTO_REGISTER=False)
class RoutingConcurrencyTests(Fixture, TransactionTestCase):
    def test_concurrent_duplicate_controls_have_one_session_and_receipt(self):
        self.authorize_telegram(self.coordinator)
        identity = int(self.coordinator.telegram_id)
        update = {'update_id': 1, 'message': {'message_id': 1, 'from': {'id': identity},
                  'chat': {'id': identity, 'type': 'private'}, 'text': '/contenidos', 'date': int(timezone.now().timestamp())}}
        def receive():
            close_old_connections()
            try:
                return TelegramRouter(777).receive(deepcopy(update)).pk
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: receive(), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(RoutingSession.objects.count(), 1)
        self.assertEqual(TelegramReceipt.objects.count(), 1)
        self.assertEqual(ChannelMessage.objects.count(), 1)

    @override_settings(TELEGRAM_GROUP_WINDOW_SECONDS=0, PILOT_MESSAGE_RETRY_SECONDS=0)
    def test_real_combined_worker_recovers_and_closes_without_duplicate_processing(self):
        self.authorize_telegram(self.coordinator)
        identity = int(self.coordinator.telegram_id)
        reporter, ai, transport, handler = handler_fixture(identity)
        ai.extract_incident.return_value = IncidentExtractionResult(**DATA)
        router = TelegramRouter(777, incident_handler=handler, transport=transport, threaded=True)
        def update(number, text):
            return {'update_id': number, 'message': {'message_id': number, 'from': {'id': identity},
                'chat': {'id': identity, 'type': 'private'}, 'text': text, 'date': int(timezone.now().timestamp())}}
        router.receive(update(1, '/incidencias')); router.tick()
        router.receive(update(2, INITIAL))
        router.tick(); router.close(); router.sync()
        self.assertEqual(IncidentDraft.objects.filter(technician=reporter).count(), 1)
        self.assertEqual(ai.extract_incident.call_count, 1)
        self.assertEqual(TelegramReceipt.objects.get(update_id=2).status, 'done')


class ReceiverLockTests(SimpleTestCase):
    @override_settings(TELEGRAM_BOT_TOKEN='offline-test-token')
    def test_second_local_receiver_is_rejected(self):
        with receiver_lock(), self.assertRaises(CommandError):
            with receiver_lock():
                self.fail('Dos receptores obtuvieron el mismo bloqueo.')

    def test_incident_command_uses_shared_lock_before_polling(self):
        from incidents.management.commands.run_telegram_bot import Command
        with patch('telegram_routing.receiver_lock.receiver_lock', side_effect=CommandError('competidor')), \
                patch('incidents.management.commands.run_telegram_bot.httpx.Client') as http:
            with self.assertRaisesMessage(CommandError, 'competidor'):
                Command().handle(discard_pending=False)
        http.assert_not_called()

    def test_windows_process_check_matches_python_and_uv_executables(self):
        import re
        from content_coordination.management.commands.run_secuoya_demo import check_other_receivers
        with patch('content_coordination.management.commands.run_secuoya_demo.os.name', 'nt'), \
                patch('content_coordination.management.commands.run_secuoya_demo.subprocess.run',
                      return_value=SimpleNamespace(returncode=0, stdout='')) as process:
            check_other_receivers()
        pattern = re.search(r"Name -match '([^']+)'", process.call_args.args[0][-1])[1]
        for name in ('python.exe', 'pythonw.exe', 'uv.exe', 'python', 'uv'):
            self.assertIsNotNone(re.fullmatch(pattern, name))


class RoutingMigrationTests(Fixture, TransactionTestCase):
    def test_additive_migration_on_populated_database_preserves_domain_rows(self):
        # Solo la BD temporal creada por DiscoverRunner; nunca se ejecuta en demo.
        self.assertEqual(connection.settings_dict['NAME'], 'test_secuoya_telegram_routing')
        request = self.request(worker=self.worker)
        self.action(self.worker, request, 'submit', {'version': '1.0'})
        protected = [Participant, TelegramConsent, TelegramLinkTransfer, ContentRequest, Task, ContentVersion,
                     History, Conversation, Message, Outbox, Incident, IncidentDraft, ChannelMessage]
        before = [list(model.objects.order_by('pk').values()) for model in protected]
        executor = MigrationExecutor(connection)
        executor.migrate([('telegram_routing', None)])
        try:
            self.assertNotIn(TelegramReceipt._meta.db_table, connection.introspection.table_names())
        finally:
            MigrationExecutor(connection).migrate([('telegram_routing', '0001_initial')])
        self.assertEqual([list(model.objects.order_by('pk').values()) for model in protected], before)

    def test_missing_router_schema_stops_before_telegram(self):
        module = 'content_coordination.management.commands.run_secuoya_demo'
        with patch(module + '.ensure_demo'), patch(module + '.connection.introspection.table_names', return_value=[]), \
                patch(module + '.httpx.Client') as http:
            with self.assertRaisesMessage(CommandError, 'Falta la migración'):
                call_command('run_secuoya_demo', live=True, commands_only=True)
        http.assert_not_called()
