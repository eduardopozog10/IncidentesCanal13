import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from incidents.ai.base import AITemporaryError
from incidents.messaging.telegram_provider import TelegramDeliveryUncertainError, TelegramRejectedError
from incidents.models import Incident
from .ai import validate_plan
from .models import Participant, ContentRequest, ContentVersion, Task, Message, Outbox, Conversation, History, ProgressQuery
from .processor import Processor, enqueue
from .routing import queue_update
from .delivery import deliver_one, recover_delivery_interruptions
from . import services as service


DATA = {'title': 'Economía', 'description': 'Gráfica de encuesta', 'requesting_area': 'Prensa',
        'responsible_area': 'Gráfica', 'due_at': 'sin plazo'}


class Fixture:
    def setUp(self):
        self.actors = {}
        for name, role in [('Periodista', 'journalist'), ('Fernando', 'coordinator'), ('Daniela', 'designer'),
                           ('Felipe', 'designer'), ('Revisor', 'reviewer'), ('Editor', 'editor'),
                           ('Switch', 'switch'), ('Admin', 'admin')]:
            user = get_user_model().objects.create_user(name.lower())
            self.actors[name] = Participant.objects.create(name=name, role=role, user=user)
        self.journalist = self.actors['Periodista']; self.coordinator = self.actors['Fernando']
        self.worker = self.actors['Daniela']; self.other = self.actors['Felipe']
        self.reviewer = self.actors['Revisor']; self.editor = self.actors['Editor']; self.switch = self.actors['Switch']

    def request(self, title='Economía', worker=None):
        r = service.create_request(self.journalist, {**DATA, 'title': title})
        if worker:
            service.execute(self.coordinator, r.pk, 'assign', {'assignee_id': worker.pk}, confirmed=True)
        r.refresh_from_db()
        return r

    def action(self, who, r, action, data=None, **kwargs):
        return service.execute(who, r.pk, action, data or {}, **kwargs)

    def review(self):
        r = self.request(worker=self.worker)
        self.action(self.worker, r, 'submit', {'version': '1.0'})
        return r

    def authorize_telegram(self, actor):
        from .models import TelegramConsent
        actor.telegram_id = str(90000000 + actor.pk)
        actor.telegram_authorized = True
        actor.save()
        TelegramConsent.objects.create(participant=actor, telegram_id=actor.telegram_id,
                                       consent_at=timezone.now(), verified_at=timezone.now())

    def plan(self, actor, text, updates=None, channel='dashboard', external_id=None):
        if channel == 'telegram' and actor.telegram_id is None:
            # Simula una identidad previamente verificada por el receptor; solo
            # datos sintéticos de la base temporal, nunca cuentas reales.
            from .models import TelegramConsent
            actor.telegram_id = str(90000000 + actor.pk)
            actor.telegram_authorized = True
            actor.save()
            TelegramConsent.objects.create(participant=actor, telegram_id=actor.telegram_id,
                                           consent_at=timezone.now(), verified_at=timezone.now())
        interpreter = Mock()
        interpreter.interpret.return_value = {'updates': updates or [{'action': 'query', 'evidence': text, 'data': {}}]}
        msg = enqueue(actor, external_id or str(uuid.uuid4()), text, channel=channel)
        reply = Processor(interpreter).process(msg)
        msg.refresh_from_db()
        return reply, msg


class DomainTests(Fixture, TestCase):
    def test_create_has_audit_and_durable_coordinator_notification(self):
        r = self.request()
        self.assertTrue(r.code.startswith('CT-'))
        self.assertEqual(r.tasks.count(), 1)
        self.assertEqual(r.history.get().operation, 'create')
        self.assertTrue(Outbox.objects.filter(recipient=self.coordinator, request=r).exists())
        self.assertFalse(Incident.objects.exists())

    def test_required_data_not_invented(self):
        with self.assertRaises(ValidationError):
            service.create_request(self.journalist, {'title': 'Gráfica'})
        self.assertFalse(ContentRequest.objects.exists())

    def test_assignment_requires_authority_and_confirmation(self):
        r = self.request()
        with self.assertRaises(PermissionDenied):
            self.action(self.worker, r, 'assign', {'assignee_id': self.worker.pk}, confirmed=True)
        with self.assertRaises(ValidationError):
            self.action(self.coordinator, r, 'assign', {'assignee_id': self.worker.pk})
        self.assertIsNone(r.tasks.get().assignee)

    def test_full_workflow_separates_approval_edit_receive_emission(self):
        r = self.request(worker=self.worker)
        self.action(self.worker, r, 'start')
        self.action(self.worker, r, 'submit', {'version': '1.0'})
        self.action(self.reviewer, r, 'approve', {'version': '1.0'}, confirmed=True)
        v = r.versions.get()
        self.assertEqual(v.approved_by, self.reviewer)
        self.assertIsNone(v.edited_at)
        self.assertIsNone(v.received_at)
        self.action(self.editor, r, 'edit', {'version': '1.0'})
        self.action(self.switch, r, 'receive', {'version': '1.0'})
        r.refresh_from_db(); v.refresh_from_db()
        self.assertEqual(r.state, 'received'); self.assertIsNone(v.emitted_at)
        with self.assertRaises(ValidationError):
            self.action(self.switch, r, 'emit', {'version': '1.0'})
        self.action(self.switch, r, 'emit', {'version': '1.0'}, confirmed=True)
        v.refresh_from_db(); self.assertEqual(v.emitted_by, self.switch)
        self.assertEqual(r.history.count(), 8)

    def test_worker_cannot_approve_or_modify_someone_elses_task(self):
        r = self.review()
        with self.assertRaises(PermissionDenied):
            self.action(self.worker, r, 'approve', {'version': '1.0'}, confirmed=True)
        with self.assertRaises(PermissionDenied):
            self.action(self.other, r, 'progress', {'progress': 'terminado'})
        self.assertEqual(r.versions.get().state, 'review')

    def test_version_approval_is_not_overwritten_by_corrections(self):
        r = self.review()
        self.action(self.reviewer, r, 'approve', {'version': '1.0'}, confirmed=True)
        before = r.versions.get().approved_at
        self.action(self.reviewer, r, 'corrections', {'version': '1.0', 'new_version': '1.1'}, confirmed=True)
        self.assertEqual(r.versions.get(number='1.0').approved_at, before)
        self.assertEqual(r.versions.get(number='1.1').state, 'working')
        self.assertEqual(r.versions.get(number='1.0').state, 'approved')

    def test_no_reception_before_editing_or_emission_before_receipt(self):
        r = self.review()
        with self.assertRaises((ValidationError, PermissionDenied)):
            self.action(self.switch, r, 'receive', {'version': '1.0'})
        self.assertIsNone(r.versions.get().received_at)

    def test_duplicate_submission_does_not_overwrite(self):
        r = self.review()
        with self.assertRaises(ValidationError):
            self.action(self.worker, r, 'submit', {'version': '1.0'})
        self.assertEqual(r.versions.count(), 1)

    def test_stale_confirmation_rejected(self):
        r = self.request(worker=self.worker)
        revision = r.revision
        self.action(self.worker, r, 'start')
        with self.assertRaises(ValidationError):
            self.action(self.coordinator, r, 'cancel', revision=revision, confirmed=True)
        r.refresh_from_db(); self.assertEqual(r.state, 'production')

    def test_task_reference_does_not_mix_jobs(self):
        economy = self.request(worker=self.worker)
        self.request('Deportes', self.worker)
        politics = self.request('Política', self.other)
        self.assertEqual(service.resolve_request(self.worker, 'Terminé Economía'), economy)
        with self.assertRaises(ValidationError):
            service.resolve_request(self.worker, 'Ya terminé')
        with self.assertRaises(PermissionDenied):
            service.resolve_request(self.worker, politics.code)

    def test_unknown_title_never_falls_back_to_single_owned_task(self):
        self.request(worker=self.worker)
        with self.assertRaises(ValidationError):
            service.resolve_request(self.worker, 'Terminé Política')

    def test_eta_never_completes_task(self):
        r = self.request(worker=self.worker)
        self.action(self.worker, r, 'progress', {'progress': 'Me faltan 15 minutos', 'eta_minutes': 15})
        task = r.tasks.get(); self.assertEqual(task.state, 'pending'); self.assertEqual(task.eta_minutes, 15)
        self.assertIsNone(task.completed_at)

    def test_reminders_once_and_no_state_change(self):
        r = self.request(worker=self.worker)
        r.tasks.update(due_at=timezone.now() + timedelta(minutes=2))
        self.assertEqual(service.schedule_reminders(), 1)
        self.assertEqual(service.schedule_reminders(), 0)
        self.assertEqual(r.tasks.get().state, 'pending')

    def test_progress_query_is_deduplicated_and_answered_by_real_update(self):
        self.authorize_telegram(self.worker)
        r = self.request(worker=self.worker)
        service.ask_updates(self.coordinator, self.worker)
        service.ask_updates(self.coordinator, self.worker)
        self.assertEqual(ProgressQuery.objects.count(), 1)
        Outbox.objects.filter(key__startswith='query:').update(status='sent',sent_at=timezone.now())
        self.action(self.worker, r, 'progress', {'progress': 'Faltan dos gráficos'})
        self.assertIsNotNone(ProgressQuery.objects.get().answered_at)
        self.assertTrue(Outbox.objects.filter(recipient=self.coordinator, key__startswith='query-answer:').exists())

    def test_inactive_actor_cannot_change_existing_task(self):
        r = self.request(worker=self.worker)
        Participant.objects.filter(pk=self.worker.pk).update(active=False)
        with self.assertRaises(PermissionDenied):
            self.action(self.worker, r, 'start')


class ConversationTests(Fixture, TestCase):
    def test_fields_split_by_ai_are_merged_without_losing_information(self):
        text = 'Economía, resultados regionales, Prensa, Gráfica, sin plazo'
        updates = [{'action': 'create', 'evidence': text, 'data': {key: value}} for key, value in DATA.items()]
        reply, _ = self.plan(self.journalist, text, updates)
        self.assertIn('¿La ingreso así?', reply)
        self.assertEqual(Conversation.objects.get(participant=self.journalist).draft, DATA)
        self.assertFalse(ContentRequest.objects.exists())

    def test_distinct_new_requests_are_not_merged(self):
        text = 'Necesito Economía y Deportes'
        reply, _ = self.plan(self.journalist, text, [
            {'action': 'create', 'evidence': text, 'data': {'title': 'Economía'}},
            {'action': 'create', 'evidence': text, 'data': {'title': 'Deportes'}}])
        self.assertIn('una a la vez', reply)
        self.assertFalse(ContentRequest.objects.exists())

    def test_complete_draft_requires_direct_confirmation_and_creates_once(self):
        text = 'Necesito gráfica Economía para Prensa, responsable Gráfica, sin plazo'
        reply, _ = self.plan(self.journalist, text, [{'action': 'create', 'evidence': text, 'data': DATA}])
        self.assertIn('¿La ingreso así?', reply)
        self.assertFalse(ContentRequest.objects.exists())
        reply, msg = self.plan(self.journalist, 'confirmo')
        self.assertEqual(ContentRequest.objects.count(), 1)
        Processor(Mock()).process(msg)
        self.plan(self.journalist, 'confirmo')
        self.assertEqual(ContentRequest.objects.count(), 1)

    def test_incomplete_draft_keeps_fields_and_asks_one_question(self):
        text = 'Gráfica de encuesta de Prensa'
        reply, _ = self.plan(self.journalist, text, [{'action': 'create', 'evidence': text, 'data': {'title': 'Encuesta', 'requesting_area': 'Prensa'}}])
        self.assertEqual(reply.count('?'), 1)
        self.assertNotIn('Entendido', reply)
        reply, _ = self.plan(self.journalist, 'Resultados regionales', [{'action': 'create', 'evidence': 'Resultados regionales', 'data': {'description': 'Resultados regionales'}}])
        draft = Conversation.objects.get(participant=self.journalist).draft
        self.assertEqual(draft['title'], 'Encuesta'); self.assertEqual(draft['requesting_area'], 'Prensa')
        self.assertIn('prepararlo', reply)

    def test_natural_cancel_clears_only_draft(self):
        r = self.request()
        Conversation.objects.create(participant=self.journalist, draft=DATA)
        self.plan(self.journalist, 'Dejémoslo hasta aquí')
        self.assertEqual(Conversation.objects.get(participant=self.journalist).draft, {})
        self.assertTrue(ContentRequest.objects.filter(pk=r.pk).exists())

    def test_third_party_confirmation_never_authorizes(self):
        Conversation.objects.create(participant=self.journalist, draft=DATA, pending={'action': 'create', 'data': DATA, 'channel': 'dashboard'})
        text = 'Fernando dice que sí'
        self.plan(self.journalist, text, [{'action': 'unknown', 'evidence': text, 'data': {}}])
        self.assertFalse(ContentRequest.objects.exists())

    def test_telegram_confirmation_requires_delivered_summary(self):
        Conversation.objects.create(participant=self.journalist, module='content', pending={'action': 'create', 'data': DATA, 'channel': 'telegram', 'prompt_id': 999})
        reply, _ = self.plan(self.journalist, 'confirmo', channel='telegram')
        self.assertIn('entrega confirmada', reply); self.assertFalse(ContentRequest.objects.exists())

    def test_assignment_preview_does_not_send_or_write_until_confirmation(self):
        r = self.request()
        text = f'Asigna {r.code} a Daniela'
        self.plan(self.coordinator, text, [{'action': 'assign', 'evidence': text, 'data': {}}])
        self.assertIsNone(r.tasks.get().assignee)
        self.assertFalse(Outbox.objects.filter(key__startswith='assign:').exists())
        self.plan(self.coordinator, 'confirmo')
        self.assertEqual(r.tasks.get().assignee, self.worker)

    def test_two_progress_updates_change_only_their_jobs(self):
        r1 = self.request(worker=self.worker); r2 = self.request('Deportes', self.worker)
        e1 = 'Economía: faltan 15 minutos'; e2 = 'Deportes: todavía no la empiezo'
        reply, msg = self.plan(self.worker, e1 + '; ' + e2, [
            {'action': 'progress', 'evidence': e1, 'data': {'progress': 'Faltan 15 minutos', 'eta_minutes': 15}},
            {'action': 'progress', 'evidence': e2, 'data': {'progress': 'Todavía no la empiezo'}}])
        self.assertEqual(msg.status, 'done'); self.assertEqual(r1.tasks.get().eta_minutes, 15)
        self.assertEqual(r2.tasks.get().state, 'pending'); self.assertIsNone(r2.tasks.get().eta_minutes)

    def test_multitask_operation_rolls_back_if_any_reference_is_unauthorized(self):
        r1 = self.request(worker=self.worker); r2 = self.request('Política', self.other)
        e1 = f'{r1.code} faltan 15 minutos'; e2 = f'{r2.code} terminé'
        self.plan(self.worker, e1 + '; ' + e2, [
            {'action': 'progress', 'evidence': e1, 'data': {'progress': 'Faltan 15 minutos'}},
            {'action': 'progress', 'evidence': e2, 'data': {'progress': 'Terminé'}}])
        self.assertEqual(r1.tasks.get().last_progress, '')

    def test_duplicate_external_id_cannot_change_sender_or_payload(self):
        enqueue(self.worker, 'unique', 'Hola')
        with self.assertRaises(PermissionDenied):
            enqueue(self.other, 'unique', 'Hola')
        with self.assertRaises(PermissionDenied):
            enqueue(self.worker, 'unique', 'Otra cosa')

    def test_invalid_ai_evidence_rejected(self):
        with self.assertRaises(ValueError):
            validate_plan({'updates': [{'action': 'start', 'evidence': 'inventado', 'data': {}}]}, 'Hola')

    def test_failure_is_durable_and_retry_limit_is_enforced(self):
        msg = enqueue(self.worker, 'retry', '¿Mis trabajos?', channel='dashboard')
        interpreter = Mock(); interpreter.interpret.side_effect = AITemporaryError('No disponible')
        processor = Processor(interpreter)
        for _ in range(3):
            Message.objects.filter(pk=msg.pk).update(retry_at=timezone.now())
            processor.process(msg)
        msg.refresh_from_db(); self.assertEqual(msg.status, 'exhausted'); self.assertEqual(msg.attempts, 3)
        processor.process(msg); self.assertEqual(interpreter.interpret.call_count, 3)

    def test_explicit_module_selection_never_creates_incident(self):
        self.plan(self.worker, '/contenido', channel='telegram')
        self.assertEqual(Conversation.objects.get(participant=self.worker).module, 'content')
        self.assertFalse(Incident.objects.exists())

    def test_query_reports_actual_states_without_inventing_progress(self):
        self.authorize_telegram(self.worker)
        r = self.request(worker=self.worker)
        reply, _ = self.plan(self.coordinator, '¿Cómo va Daniela?')
        self.assertIn(r.code, reply); self.assertIn('Sin avance informado', reply)
        self.assertIn('¿Quieres que le consulte a Daniela', reply)
        self.plan(self.coordinator, 'Sí, pregúntale')
        self.assertEqual(ProgressQuery.objects.count(), 1)


@override_settings(CONTENT_LIVE_TELEGRAM=True, CONTENT_ALLOWED_TELEGRAM_IDS=['6690350935'])
class DeliveryTests(Fixture, TestCase):
    def test_old_assignment_notice_is_not_delivered_after_task_started(self):
        self.worker.telegram_id = '6690350935'
        self.worker.telegram_authorized = True
        self.worker.save()
        r = self.request(worker=self.worker)
        self.action(self.worker, r, 'start')
        provider = Mock()
        while deliver_one(provider) is not None:
            pass
        provider.send_text.assert_not_called()
        self.assertTrue(Outbox.objects.filter(key__startswith='assign:', status='superseded').exists())

    def outbox(self):
        self.worker.telegram_id = '6690350935'; self.worker.telegram_authorized = True; self.worker.save()
        return service.notify(self.worker, 'Mensaje de prueba', 'test')

    def test_success_requires_receipt_and_is_not_resent(self):
        out = self.outbox(); provider = Mock(); provider.send_text.return_value = {'result': {'message_id': 123}}
        deliver_one(provider); deliver_one(provider)
        out.refresh_from_db(); self.assertEqual(out.status, 'sent'); self.assertEqual(provider.send_text.call_count, 1)

    def test_uncertain_delivery_not_retried(self):
        out = self.outbox(); provider = Mock(); provider.send_text.side_effect = TelegramDeliveryUncertainError()
        deliver_one(provider); deliver_one(provider)
        out.refresh_from_db(); self.assertEqual(out.status, 'uncertain'); self.assertEqual(provider.send_text.call_count, 1)

    def test_unauthorized_recipient_never_calls_telegram(self):
        out = service.notify(self.other, 'Mensaje', 'blocked'); provider = Mock()
        deliver_one(provider); out.refresh_from_db()
        self.assertEqual(out.status, 'blocked'); provider.send_text.assert_not_called()

    def test_interrupted_send_requires_review(self):
        out = self.outbox(); out.status = 'sending'; out.save()
        recover_delivery_interruptions(); out.refresh_from_db(); self.assertEqual(out.status, 'uncertain')

    def test_known_rejection_schedules_bounded_retry(self):
        out = self.outbox(); provider = Mock(); provider.send_text.side_effect = TelegramRejectedError()
        for _ in range(3):
            Outbox.objects.filter(pk=out.pk).update(retry_at=timezone.now())
            deliver_one(provider)
        out.refresh_from_db(); self.assertEqual(out.status, 'failed'); self.assertEqual(out.attempts, 3)

    def test_forged_group_or_third_party_update_rejected(self):
        self.outbox()
        update = {'update_id': 123, 'message': {'from': {'id': 6690350935}, 'chat': {'type': 'group', 'id': 6690350935}, 'text': '/contenido'}}
        with self.assertRaises(PermissionDenied): queue_update(update)
        update['message']['chat']['type'] = 'private'; update['message']['from']['id'] = 1
        with self.assertRaises(PermissionDenied): queue_update(update)
        self.assertFalse(Message.objects.exists())


class DashboardTests(Fixture, TestCase):
    def test_filters_detail_and_refresh_show_persisted_telegram_progress(self):
        r = self.request(worker=self.worker)
        self.request('Deportes', self.other)
        self.client.force_login(self.coordinator.user)
        response = self.client.get('/contents/', {'q': r.code, 'area': 'Gráfica', 'state': 'assigned'})
        self.assertContains(response, r.code)
        self.assertNotContains(response, '>Deportes<')
        Conversation.objects.create(participant=self.worker, module='content')
        text = f'{r.code}: faltan 15 minutos'
        self.plan(self.worker, text, [{'action': 'progress', 'evidence': text,
                                     'data': {'progress': 'Faltan 15 minutos', 'eta_minutes': 15}}], channel='telegram')
        response = self.client.get(f'/contents/{r.pk}/data/')
        self.assertContains(response, 'Faltan 15 minutos')
        self.assertContains(response, 'Estimación informada: 15 minutos')

    def test_web_send_uses_csrf_and_returns_real_persisted_query(self):
        r = self.request(worker=self.worker)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.worker.user)
        client.get('/contents/')
        from django.conf import settings
        token = client.cookies[settings.CSRF_COOKIE_NAME].value
        plan = {'updates': [{'action': 'query', 'evidence': 'Mis trabajos', 'data': {}}]}
        with patch('content_coordination.processor.GeminiContentInterpreter') as provider:
            provider.return_value.interpret.return_value = plan
            response = client.post('/contents/send/', {'nonce': str(uuid.uuid4()), 'text': 'Mis trabajos',
                                                      'csrfmiddlewaretoken': token})
        self.assertEqual(response.status_code, 200)
        self.assertIn(r.code, response.json()['reply'])
        self.assertEqual(Message.objects.get().status, 'done')

    def test_anonymous_dashboard_and_api_protected(self):
        self.assertEqual(self.client.get('/contents/').status_code, 302)
        self.assertEqual(self.client.get('/contents/data/').status_code, 302)

    def test_unassigned_user_cannot_inspect_other_job(self):
        r = self.request(worker=self.worker)
        self.client.force_login(self.other.user)
        self.assertEqual(self.client.get(f'/contents/{r.pk}/').status_code, 404)

    def test_post_requires_csrf_and_get_cannot_send(self):
        client = Client(enforce_csrf_checks=True); client.force_login(self.worker.user)
        self.assertEqual(client.post('/contents/send/', {'nonce': str(uuid.uuid4()), 'text': 'Hola'}).status_code, 403)
        self.assertEqual(client.get('/contents/send/').status_code, 405)
        self.assertFalse(Message.objects.exists())

    def test_actual_data_html_escaped_and_no_store(self):
        r = self.request('<script>alert(1)</script>', self.worker)
        self.client.force_login(self.worker.user)
        response = self.client.get('/contents/')
        self.assertContains(response, r.code)
        self.assertNotContains(response, '<script>alert(1)</script>')
        self.assertIn('no-store', response.headers['Cache-Control'])

    def test_logout_invalidates_session(self):
        self.client.force_login(self.worker.user)
        self.assertEqual(self.client.post('/contents/logout/').status_code, 302)
        self.assertEqual(self.client.get('/contents/').status_code, 302)

    def test_user_without_content_membership_is_denied(self):
        user = get_user_model().objects.create_user('unrelated')
        self.client.force_login(user)
        self.assertEqual(self.client.get('/contents/').status_code, 403)


class ConcurrentTests(Fixture, TransactionTestCase):
    def test_simultaneous_confirmations_process_one_message_once(self):
        Conversation.objects.create(participant=self.journalist, pending={'action': 'create', 'data': DATA, 'channel': 'dashboard'})
        msg = enqueue(self.journalist, 'same-confirmation', 'confirmo', channel='dashboard')
        def run():
            close_old_connections()
            try:
                return Processor(Mock()).process(Message.objects.get(pk=msg.pk))
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: run(), range(2)))
        self.assertEqual(ContentRequest.objects.count(), 1)
        self.assertEqual(History.objects.filter(operation='create').count(), 1)
        self.assertEqual(results[0], results[1])
