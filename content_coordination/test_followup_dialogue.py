"""Regresión multicuenta en PostgreSQL temporal; Gemini/Telegram simulados."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import Mock

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from incidents.ai.base import AITemporaryError
from .tests import Fixture, DATA
from .models import Conversation, Message, Outbox, ProgressQuery, TelegramConsent, Participant
from .processor import Processor, enqueue, context_for
from .ai import validate_plan
from .interpretation import explicit_action
from .dialogue import response_act
from . import services
from .delivery import deliver_one


class FollowupFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.authorize_telegram(self.coordinator)
        self.authorize_telegram(self.worker)
        Conversation.objects.create(participant=self.coordinator, module='content')
        Conversation.objects.create(participant=self.worker, module='content')

    def pending(self):
        return Conversation.objects.get(participant=self.coordinator).pending

    def prompt_delivered(self):
        Outbox.objects.filter(key=f"reply:{self.pending()['prompt_id']}").update(status='sent',sent_at=timezone.now())

    def ask(self, r, *, proposed='ask', channel='dashboard'):
        text = f'Quiero que le preguntes a Daniela cómo va con {r.code} y cuánto tiempo estima que le falta para terminar el diseño.'
        return self.plan(self.coordinator,text,[{'action':proposed,'evidence':text,'data':{}}],channel=channel)


class PendingResponseTests(FollowupFixture, TestCase):
    def assignment(self):
        r=self.request()
        text=f'Asigna la solicitud {r.code} a Daniela como diseñadora responsable.'
        self.plan(self.coordinator,text,[{'action':'assign','evidence':text,'data':{}}],channel='telegram')
        return r

    def test_exact_typo_confirms_only_delivered_pending_assignment_and_executes_once(self):
        r=self.assignment();self.prompt_delivered()
        reply,msg=self.plan(self.coordinator,'confirmoS',channel='telegram')
        self.assertIn('asignación quedó guardada',reply)
        self.assertEqual(r.tasks.get().assignee,self.worker)
        Processor(Mock()).process(msg)
        self.plan(self.coordinator,'confirmo',channel='telegram')
        self.assertEqual(r.history.filter(operation='assign').count(),1)
        self.assertEqual(Outbox.objects.filter(key__startswith='assign:').count(),1)

    def test_typo_does_not_skip_delivery_or_channel_guards(self):
        r=self.assignment()
        self.plan(self.coordinator,'confirmoS',channel='telegram')
        self.assertIsNone(r.tasks.get().assignee)
        self.assertTrue(self.pending())
        self.prompt_delivered()
        self.plan(self.coordinator,'confirmoS',channel='dashboard')
        self.assertIsNone(r.tasks.get().assignee)
        self.plan(self.coordinator,'confirmoS',channel='telegram')
        self.assertEqual(r.tasks.get().assignee,self.worker)

    def test_general_positive_forms_and_light_typos_are_contextual(self):
        for text in ['confimo','confrimo','confirmos','Sí, confirmo la asignación','Sí, por favor','De acuerdo']:
            with self.subTest(text=text):
                self.assertEqual(response_act(text,{'action':'assign'},['asignacion']),'confirm')
        for text in ['confirmo?', 'si confirmo mañana', 'Daniela dijo confirmo', 'no confirmo',
                     'sí, pero cambia la persona', 'confirmo CT-999999', 'confirmaría', 'si tal vez']:
            with self.subTest(text=text):
                self.assertNotEqual(response_act(text,{'action':'assign'}),'confirm')

    def test_unknown_preserves_operation_and_original_delivery_evidence(self):
        r=self.assignment();before=self.pending()
        text='mmm, no estoy seguro de cómo responder'
        self.plan(self.coordinator,text,[{'action':'unknown','evidence':text,'data':{}}],channel='telegram')
        self.assertEqual(self.pending(),before)
        # Entregar una aclaración genérica no equivale a entregar el resumen.
        Outbox.objects.exclude(key=f"reply:{before['prompt_id']}").update(status='sent',sent_at=timezone.now())
        self.plan(self.coordinator,'confirmo',channel='telegram')
        self.assertIsNone(r.tasks.get().assignee)
        self.prompt_delivered()
        self.plan(self.coordinator,'confirmo',channel='telegram')
        self.assertEqual(r.tasks.get().assignee,self.worker)

    def test_hold_smalltalk_and_information_query_preserve_assignment(self):
        r=self.assignment();before=self.pending()
        for text in ['un momento','gracias',f'¿En qué estado está {r.code}?']:
            self.plan(self.coordinator,text,channel='telegram')
            self.assertEqual(self.pending(),before)
        self.assertIsNone(r.tasks.get().assignee)

    def test_negative_declines_operation_without_deleting_draft_or_work(self):
        r=self.assignment()
        Conversation.objects.filter(participant=self.coordinator).update(draft=DATA)
        self.plan(self.coordinator,'No, gracias',channel='telegram')
        self.assertEqual(self.pending(),{})
        self.assertEqual(Conversation.objects.get(participant=self.coordinator).draft,DATA)
        self.assertIsNone(r.tasks.get().assignee)

    def test_ambiguous_third_party_and_conditional_never_execute(self):
        r=self.assignment();self.prompt_delivered()
        for text in ['Daniela dijo que confirmo','¿Confirmo?','Si Daniela está libre, confirmo']:
            self.plan(self.coordinator,text,[{'action':'unknown','evidence':text,'data':{}}],channel='telegram')
            self.assertIsNone(r.tasks.get().assignee)
            self.assertTrue(self.pending())

    def test_ai_failure_retains_operation_but_requires_fresh_review(self):
        r=self.assignment();self.prompt_delivered()
        msg=enqueue(self.coordinator,'failed-correction','Cambia el diseñador',channel='telegram')
        interpreter=Mock();interpreter.interpret.side_effect=AITemporaryError('Unavailable')
        Processor(interpreter).process(msg)
        self.assertTrue(self.pending()['needs_review'])
        Message.objects.filter(pk=msg.pk).update(status='exhausted')
        self.plan(self.coordinator,'confirmo',channel='telegram')
        self.assertIsNone(r.tasks.get().assignee)

    def test_typo_never_bypasses_revision_or_role_change(self):
        r=self.assignment();self.prompt_delivered()
        Participant.objects.filter(pk=self.coordinator.pk).update(role='designer')
        self.plan(self.coordinator,'confirmoS',channel='telegram')
        self.assertIsNone(r.tasks.get().assignee)

    def test_query_of_another_ct_cannot_silently_confirm_original_operation(self):
        original=self.assignment();other=self.request('Otro trabajo',self.worker)
        self.prompt_delivered()
        self.plan(self.coordinator,f'¿Cómo va {other.code}?',channel='telegram')
        self.assertTrue(self.pending()['needs_review'])
        self.plan(self.coordinator,'confirmo',channel='telegram')
        self.assertIsNone(original.tasks.get().assignee)


class FollowupDialogueTests(FollowupFixture, TestCase):
    def test_exact_status_query_uses_database_and_offers_without_contact(self):
        r=self.request('Gráfica resultados partido de Chile',self.worker)
        self.action(self.worker,r,'start')
        text=f'¿Cómo va Daniela con la gráfica {r.code}? Dime en qué estado está y cuál fue su último avance.'
        reply,_=self.plan(self.coordinator,text,channel='telegram')
        self.assertIn('En producción',reply)
        self.assertIn('Sin avance informado',reply)
        self.assertIn('¿Quieres que le consulte a Daniela',reply)
        self.assertEqual(self.pending()['task_ids'],[r.tasks.get().pk])
        self.assertFalse(ProgressQuery.objects.exists())
        self.assertFalse(Outbox.objects.filter(key__startswith='query:').exists())

    def test_both_real_contact_formulations_prepare_context_even_if_old_model_says_query(self):
        r=self.request(worker=self.worker)
        texts=[f'Quiero que le preguntes a Daniela cómo va con {r.code} y cuánto tiempo estima que le falta para terminar el diseño.',
               f'Envía un mensaje a Daniela, responsable de {r.code}, preguntándole cuánto le falta para terminar el diseño. Confírmame antes de enviarlo.']
        for text in texts:
            with self.subTest(text=text):
                reply,_=self.plan(self.coordinator,text,[{'action':'query','evidence':text,'data':{}}],channel='telegram')
                self.assertIn('¿Confirmas que consulte a Daniela',reply)
                self.assertIn('cuánto',reply)
                self.assertEqual(self.pending()['question'],text)
                self.assertFalse(ProgressQuery.objects.exists())

    def test_semantic_ask_supports_paraphrase_without_keyword_fallback(self):
        r=self.request(worker=self.worker)
        text=f'Necesito una actualización directamente de Daniela sobre {r.code}: que nos indique el avance y su estimación.'
        reply,_=self.plan(self.coordinator,text,[{'action':'ask','evidence':text,'data':{'question':'que nos indique el avance y su estimación'}}])
        self.assertIn('¿Confirmas que consulte',reply)
        self.assertFalse(ProgressQuery.objects.exists())

    @override_settings(CONTENT_LIVE_TELEGRAM=True)
    def test_realistic_two_account_flow_delivers_once_records_eta_and_updates_dashboard(self):
        r=self.request(worker=self.worker);self.action(self.worker,r,'start')
        Outbox.objects.update(status='blocked')
        self.ask(r,channel='telegram');self.prompt_delivered()
        reply,confirmation=self.plan(self.coordinator,'Sí, pregúntale',channel='telegram')
        self.assertIn('pendiente de envío',reply)
        q=ProgressQuery.objects.get()
        self.assertEqual(q.worker,self.worker)
        self.assertEqual(q.notification.source,confirmation)
        self.assertIn('cuánto tiempo estima',q.notification.text)
        sender=Mock();sender.send_text.return_value={'result':{'message_id':123}}
        deliver_one(sender)
        q.notification.refresh_from_db()
        self.assertEqual(q.notification.status,'sent')
        self.assertEqual(sender.send_text.call_args.args[0],self.worker.telegram_id)
        Processor(Mock()).process(confirmation)
        self.assertEqual(ProgressQuery.objects.count(),1)
        text='Me faltan 15 minutos'
        reply,msg=self.plan(self.worker,text,[{'action':'progress','evidence':text,'data':{
            'progress':text,'eta_minutes':15,'reply_to_followup':True}}],channel='telegram')
        task=r.tasks.get();r.refresh_from_db();q.refresh_from_db()
        self.assertEqual(task.last_progress,text);self.assertEqual(task.eta_minutes,15)
        self.assertEqual(task.state,'working');self.assertEqual(r.state,'production')
        self.assertFalse(r.versions.exists());self.assertIsNone(task.completed_at)
        self.assertIsNotNone(q.answered_at)
        answer=Outbox.objects.get(key__startswith='query-answer:')
        self.assertEqual(answer.recipient,self.coordinator)
        self.assertIn('15 minutos',answer.text)
        self.plan(self.coordinator,f'¿Cuál fue el último avance de {r.code}?',channel='telegram')
        latest=Message.objects.filter(participant=self.coordinator).latest('pk').result['reply']
        self.assertIn(text,latest);self.assertIn('Estimación informada: 15 minutos',latest)
        self.client.force_login(self.coordinator.user)
        self.assertContains(self.client.get(f'/contents/{r.pk}/data/'),'Estimación informada: 15 minutos')

    def test_natural_partial_response_uses_delivered_followup_context_after_restart(self):
        r=self.request(worker=self.worker);self.request('Otra pieza',self.worker)
        services.ask_updates(self.coordinator,self.worker,[r.tasks.get().pk])
        Outbox.objects.filter(key__startswith='query:').update(status='sent',sent_at=timezone.now())
        context=context_for(self.worker,Conversation.objects.get(participant=self.worker))
        self.assertEqual(context['followups'][0]['tasks'][0]['code'],r.code)
        text='Voy por la mitad; calculo otros veinte minutos.'
        self.plan(self.worker,text,[{'action':'progress','evidence':text,'data':{
            'progress':text,'eta_minutes':20,'reply_to_followup':True}}])
        self.assertEqual(r.tasks.get().eta_minutes,20)
        self.assertIsNotNone(ProgressQuery.objects.get().answered_at)

    def test_fresh_progress_does_not_offer_but_stale_progress_does(self):
        r=self.request(worker=self.worker)
        self.action(self.worker,r,'progress',{'progress':'Estoy a mitad','eta_minutes':20})
        reply,_=self.plan(self.coordinator,f'¿Cómo va {r.code}?')
        self.assertNotIn('¿Quieres que le consulte',reply)
        r.tasks.update(progress_at=timezone.now()-timedelta(hours=1))
        reply,_=self.plan(self.coordinator,f'¿Cómo va {r.code}?')
        self.assertIn('¿Quieres que le consulte',reply)

    def test_declining_offer_never_contacts_anyone(self):
        r=self.request(worker=self.worker)
        self.plan(self.coordinator,f'¿Cómo va {r.code}?')
        self.plan(self.coordinator,'No, gracias')
        self.assertEqual(self.pending(),{})
        self.assertFalse(ProgressQuery.objects.exists())

    def test_switching_query_ct_replaces_offer_with_the_new_actual_scope(self):
        first=self.request(worker=self.worker);second=self.request('Otro diseño',self.worker)
        self.plan(self.coordinator,f'¿Cómo va {first.code}?')
        self.plan(self.coordinator,f'¿Cómo va {second.code}?')
        self.assertEqual(self.pending()['task_ids'],[second.tasks.get().pk])
        self.plan(self.coordinator,'Sí, pregúntale')
        self.assertEqual(list(ProgressQuery.objects.get().tasks.all()),[second.tasks.get()])

    @override_settings(CONTENT_LIVE_TELEGRAM=True)
    def test_reassignment_after_confirmation_prevents_stale_delivery(self):
        r=self.request(worker=self.worker)
        Outbox.objects.update(status='blocked')
        services.ask_updates(self.coordinator,self.worker,[r.tasks.get().pk])
        self.action(self.coordinator,r,'assign',{'assignee_id':self.other.pk},confirmed=True)
        provider=Mock()
        result=deliver_one(provider)
        self.assertEqual(result.status,'superseded')
        provider.send_text.assert_not_called()

    def test_message_received_before_question_delivery_is_not_its_answer(self):
        r=self.request(worker=self.worker)
        services.ask_updates(self.coordinator,self.worker,[r.tasks.get().pk])
        message=enqueue(self.worker,'before-delivery',f'{r.code} Estoy a mitad',channel='dashboard')
        Outbox.objects.filter(key__startswith='query:').update(status='sent',sent_at=timezone.now()+timedelta(seconds=2))
        services.execute(self.worker,r.pk,'progress',{'progress':'Estoy a mitad'},message=message)
        self.assertIsNone(ProgressQuery.objects.get().answered_at)

    def test_overlapping_followups_from_two_coordinators_do_not_duplicate(self):
        r=self.request(worker=self.worker)
        services.ask_updates(self.coordinator,self.worker,[r.tasks.get().pk])
        with self.assertRaises(ValidationError):
            services.ask_updates(self.actors['Admin'],self.worker,[r.tasks.get().pk])
        self.assertEqual(ProgressQuery.objects.count(),1)

    def test_repeated_request_and_confirmations_do_not_duplicate_open_followup(self):
        r=self.request(worker=self.worker)
        self.ask(r);self.plan(self.coordinator,'confirmo')
        self.ask(r);self.plan(self.coordinator,'confirmo')
        self.plan(self.coordinator,'confirmo')
        self.assertEqual(ProgressQuery.objects.count(),1)
        self.assertEqual(Outbox.objects.filter(key__startswith='query:').count(),1)

    def test_wrong_recipient_reassignment_revocation_and_designer_permissions_reject(self):
        r=self.request(worker=self.worker)
        text=f'Pregúntale a Felipe por {r.code}'
        self.plan(self.coordinator,text,[{'action':'ask','evidence':text,'data':{}}])
        self.assertFalse(self.pending())
        self.ask(r)
        self.action(self.coordinator,r,'assign',{'assignee_id':self.other.pk},confirmed=True)
        self.plan(self.coordinator,'confirmo')
        self.assertFalse(ProgressQuery.objects.exists())
        text=f'Pregúntale a Daniela por {r.code}'
        self.plan(self.worker,text,[{'action':'ask','evidence':text,'data':{}}])
        self.assertFalse(ProgressQuery.objects.exists())
        self.assertFalse(Outbox.objects.filter(key__startswith='query:').exists())

    def test_revoked_or_unverified_telegram_blocks_prompt_and_confirm(self):
        r=self.request(worker=self.worker)
        self.ask(r)
        TelegramConsent.objects.filter(participant=self.worker).update(revoked_at=timezone.now())
        self.plan(self.coordinator,'confirmo')
        self.assertFalse(ProgressQuery.objects.exists())
        self.ask(r)
        self.assertFalse(ProgressQuery.objects.exists())

    def test_query_and_contact_do_not_reveal_foreign_work_to_designer(self):
        r=self.request('Privado',self.other)
        answer,_=self.plan(self.worker,f'¿Cómo va {r.code}?')
        self.assertNotIn('Privado',answer)
        text=f'Pregúntale a Felipe por {r.code}'
        self.plan(self.worker,text,[{'action':'ask','evidence':text,'data':{}}])
        self.assertFalse(ProgressQuery.objects.exists())

    def test_unrelated_update_does_not_answer_unsent_followup(self):
        r=self.request(worker=self.worker)
        services.ask_updates(self.coordinator,self.worker,[r.tasks.get().pk])
        self.action(self.worker,r,'progress',{'progress':'Un avance espontáneo'})
        self.assertIsNone(ProgressQuery.objects.get().answered_at)
        self.assertFalse(Outbox.objects.filter(key__startswith='query-answer:').exists())

    def test_multiple_delivered_tasks_require_ct_for_brief_reply(self):
        r=self.request(worker=self.worker);other=self.request('Deportes',self.worker)
        services.ask_updates(self.coordinator,self.worker)
        Outbox.objects.filter(key__startswith='query:').update(status='sent',sent_at=timezone.now())
        text='Voy a mitad; quedan quince minutos'
        self.plan(self.worker,text,[{'action':'progress','evidence':text,'data':{
            'progress':text,'eta_minutes':15,'reply_to_followup':True}}])
        self.assertIsNone(r.tasks.get().eta_minutes);self.assertIsNone(other.tasks.get().eta_minutes)
        self.assertIsNone(ProgressQuery.objects.get().answered_at)

    def test_reply_to_reassigned_followup_never_updates_another_owned_task(self):
        r=self.request(worker=self.worker);other=self.request('Otro encargo',self.worker)
        services.ask_updates(self.coordinator,self.worker,[r.tasks.get().pk])
        Outbox.objects.filter(key__startswith='query:').update(status='sent',sent_at=timezone.now())
        self.action(self.coordinator,r,'assign',{'assignee_id':self.other.pk},confirmed=True)
        text='Me faltan 15 minutos'
        self.plan(self.worker,text,[{'action':'progress','evidence':text,'data':{
            'progress':text,'eta_minutes':15,'reply_to_followup':True}}])
        self.assertIsNone(r.tasks.get().eta_minutes);self.assertIsNone(other.tasks.get().eta_minutes)

    @override_settings(CONTENT_LIVE_TELEGRAM=True)
    def test_revoked_requester_visibility_blocks_answer_delivery(self):
        r=self.request(worker=self.worker)
        Outbox.objects.update(status='blocked')
        services.ask_updates(self.coordinator,self.worker,[r.tasks.get().pk])
        Outbox.objects.filter(key__startswith='query:').update(status='sent',sent_at=timezone.now())
        self.action(self.worker,r,'progress',{'progress':'Falta el cierre'})
        Participant.objects.filter(pk=self.coordinator.pk).update(role='designer')
        provider=Mock();result=deliver_one(provider)
        self.assertEqual(result.status,'superseded')
        provider.send_text.assert_not_called()

    def test_contract_checks_question_literal_and_reply_context_type(self):
        for data in [{'question':'Pregunta inventada'}, {'reply_to_followup':'true'}]:
            with self.assertRaises(ValueError):
                validate_plan({'updates':[{'action':'ask','evidence':'Pregunta','data':data}]},'Pregunta')
        self.assertEqual(explicit_action('¿Puedes preguntarle a Daniela por CT-000003?', 'ask'),'ask')
        self.assertEqual(explicit_action('No le preguntes a Daniela por CT-000003', 'ask'),'unknown')


class FollowupConcurrencyTests(FollowupFixture, TransactionTestCase):
    def test_two_concurrent_followups_have_one_open_query_and_notification(self):
        r=self.request(worker=self.worker)
        def run(_):
            close_old_connections()
            try:return services.ask_updates(self.coordinator,self.worker,[r.tasks.get().pk])
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(run,[1,2]))
        self.assertEqual(ProgressQuery.objects.count(),1)
        self.assertEqual(Outbox.objects.filter(key__startswith='query:').count(),1)

    def test_different_operators_cannot_open_overlapping_followups_concurrently(self):
        r=self.request(worker=self.worker)
        def run(actor):
            close_old_connections()
            try:
                services.ask_updates(actor,self.worker,[r.tasks.get().pk])
                return 'created'
            except ValidationError:
                return 'blocked'
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            result=list(pool.map(run,[self.coordinator,self.actors['Admin']]))
        self.assertEqual(sorted(result),['blocked','created'])
        self.assertEqual(ProgressQuery.objects.count(),1)
        self.assertEqual(Outbox.objects.filter(key__startswith='query:').count(),1)
