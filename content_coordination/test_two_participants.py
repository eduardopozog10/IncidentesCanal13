from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.exceptions import PermissionDenied
from django.db import close_old_connections
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from incidents.messaging.telegram_provider import TelegramRejectedError
from .tests import Fixture
from .models import Participant, TelegramConsent, ParticipantAccessEvent, Message, Conversation, Outbox, ProgressQuery
from .processor import Processor, enqueue
from .routing import queue_update
from .delivery import deliver_one
from .activity import last_activity
from .access import telegram_allowed
from . import services
from .reporting import query


class Accounts:
    def grant(self, actor, identity, verified=True):
        # Identificadores sintéticos SOLO en base temporal. Nunca Bot API real.
        actor.telegram_id=identity;actor.telegram_authorized=True;actor.save()
        TelegramConsent.objects.create(participant=actor,telegram_id=identity,consent_at=timezone.now(),
                                       verified_at=timezone.now() if verified else None)

    def update(self, identity, update_id, text):
        return {'update_id':update_id,'message':{'message_id':update_id,'from':{'id':int(identity)},
                'chat':{'id':int(identity),'type':'private'},'text':text}}


@override_settings(CONTENT_ALLOWED_TELEGRAM_IDS=[], CONTENT_LIVE_TELEGRAM=True)
class TwoParticipantTests(Accounts, Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.grant(self.coordinator,'10000001');self.grant(self.worker,'10000002')

    def test_self_assignment_has_one_confirmation_and_audited_suppression(self):
        admin=self.actors['Admin'];self.grant(admin,'10000003')
        r=self.request()
        text=f'Asigna {r.code} a Admin'
        self.plan(admin,text,[{'action':'assign','evidence':text,'data':{}}])
        confirmation, _ = self.plan(admin,'confirmo')
        notice=Outbox.objects.get(key__startswith='assign:')
        self.assertEqual(notice.status,'suppressed')
        self.assertEqual(r.history.filter(operation='assign').count(),1)
        self.assertIn('asignación',confirmation)

    def test_same_display_name_is_not_the_same_account(self):
        self.worker.name=self.coordinator.name;self.worker.save()
        r=self.request()
        msg=enqueue(self.coordinator,'assignment','Asignación',channel='dashboard')
        services.execute(self.coordinator,r.pk,'assign',{'assignee_id':self.worker.pk},confirmed=True,message=msg)
        notice=Outbox.objects.get(key__startswith='assign:')
        self.assertEqual(notice.recipient,self.worker)
        self.assertEqual(notice.status,'pending')

    def test_supervisor_confirmation_does_not_leak_to_worker_context(self):
        r=self.request()
        text=f'Asigna {r.code} a Daniela'
        self.plan(self.coordinator,text,[{'action':'assign','evidence':text,'data':{}}])
        self.plan(self.worker,'confirmo')
        self.assertIsNone(r.tasks.get().assignee)
        self.plan(self.coordinator,'confirmo')
        self.assertEqual(r.tasks.get().assignee,self.worker)
        self.assertEqual(Conversation.objects.get(participant=self.worker).pending,{})

    def test_assignment_delivers_only_to_worker_and_alerts_are_not_suppressed(self):
        r=self.request(worker=self.worker)
        # Quitar solo de selección los avisos de preparación del test, sin borrar registros.
        Outbox.objects.filter(key__startswith='new:').update(status='blocked')
        sender=Mock();sender.send_text.return_value={'result':{'message_id':123}}
        out=deliver_one(sender)
        self.assertEqual(out.recipient,self.worker)
        self.assertEqual(sender.send_text.call_args.args[0],'10000002')
        self.assertIn('Diseño',sender.send_text.call_args.args[1])
        r.tasks.update(due_at=timezone.now()+timedelta(minutes=2))
        services.schedule_reminders();services.schedule_reminders()
        alert=Outbox.objects.get(key__startswith='deadline:')
        self.assertEqual(alert.status,'pending')

    def test_scoped_query_requires_confirmation_before_contact_and_survives_restart(self):
        economy=self.request(worker=self.worker);sports=self.request('Deportes',self.worker)
        Outbox.objects.update(status='blocked')
        text='¿Cómo va Daniela con Deportes?'
        self.plan(self.coordinator,text,[{'action':'query','evidence':text,'data':{}}])
        self.assertFalse(ProgressQuery.objects.exists())
        self.assertFalse(Outbox.objects.filter(key__startswith='query:').exists())
        self.plan(self.coordinator,'Sí, pregúntale')
        q=ProgressQuery.objects.get()
        self.assertEqual(set(q.tasks.values_list('request_id',flat=True)),{sports.pk})
        sender=Mock();sender.send_text.return_value={'result':{'message_id':124}}
        deliver_one(sender)
        self.assertEqual(sender.send_text.call_args.args[0],'10000002')
        # Procesador nuevo representa reinicio; el alcance está en PostgreSQL.
        msg=enqueue(self.worker,'eta-after-restart','Me faltan 15 minutos',channel='dashboard')
        ai=Mock();ai.interpret.return_value={'updates':[{'action':'progress','evidence':msg.text,
                                                    'data':{'progress':msg.text,'eta_minutes':15}}]}
        Processor(ai).process(msg)
        self.assertEqual(sports.tasks.get().eta_minutes,15)
        self.assertIsNone(economy.tasks.get().eta_minutes)
        self.assertEqual(sports.tasks.get().state,'pending')
        self.assertTrue(Outbox.objects.filter(key__startswith='query-answer:',recipient=self.coordinator).exists())

    def test_multiple_delivered_queries_do_not_choose_arbitrarily(self):
        r1=self.request(worker=self.worker);r2=self.request('Deportes',self.worker)
        services.ask_updates(self.coordinator,self.worker,[r1.tasks.get().pk])
        services.ask_updates(self.coordinator,self.worker,[r2.tasks.get().pk])
        Outbox.objects.filter(key__startswith='query:').update(status='sent')
        text='Me faltan 15 minutos'
        self.plan(self.worker,text,[{'action':'progress','evidence':text,'data':{'progress':text,'eta_minutes':15}}])
        self.assertIsNone(r1.tasks.get().eta_minutes);self.assertIsNone(r2.tasks.get().eta_minutes)

    def test_long_delivery_retries_only_unacknowledged_part(self):
        Outbox.objects.update(status='blocked')
        out=services.notify(self.worker,'Texto '*1200,'long')
        sender=Mock();sender.send_text.side_effect=[{'result':{'message_id':1}},TelegramRejectedError()]
        deliver_one(sender);out.refresh_from_db()
        self.assertEqual(out.status,'retry');self.assertEqual(len(out.delivery_parts),1)
        out.retry_at=timezone.now();out.save()
        sender.send_text.side_effect=None;sender.send_text.return_value={'result':{'message_id':2}}
        deliver_one(sender);out.refresh_from_db()
        self.assertEqual(out.status,'sent');self.assertEqual(len(out.delivery_parts),2)
        self.assertEqual(sender.send_text.call_count,3)

    def test_last_event_follows_version_through_emission_without_using_notices(self):
        r=self.review()
        self.action(self.reviewer,r,'approve',{'version':'1.0'},confirmed=True)
        self.action(self.editor,r,'edit',{'version':'1.0'})
        self.action(self.switch,r,'receive',{'version':'1.0'})
        self.assertIn('recibida por Switch',last_activity(r)['text'])
        self.action(self.switch,r,'emit',{'version':'1.0'},confirmed=True)
        services.notify(self.worker,'Recordatorio','not-a-progress',r)
        self.assertEqual(last_activity(r)['text'],'Emisión de la versión 1.0 confirmada')
        answer,_=query(self.coordinator,f'¿Cómo está {r.code}?')
        self.assertIn('Emisión de la versión 1.0 confirmada',answer)
        self.assertNotIn('Último avance: Versión 1.0 lista para revisión',answer)
        self.client.force_login(self.coordinator.user)
        self.assertContains(self.client.get('/contents/'),last_activity(r)['text'])

    def test_old_version_event_does_not_complete_new_version_activity(self):
        r=self.review();self.action(self.reviewer,r,'approve',{'version':'1.0'},confirmed=True)
        self.action(self.reviewer,r,'corrections',{'version':'1.0','new_version':'1.1'},confirmed=True)
        self.action(self.editor,r,'edit',{'version':'1.0'})
        self.assertIn('1.1',last_activity(r,version='1.1')['text'])
        self.assertIn('incorporada a Edición',last_activity(r,version='1.0')['text'])

    def test_worker_cannot_consult_or_modify_other_workers_jobs(self):
        r=self.request('Política',self.other)
        text=f'{r.code}: ya estoy trabajando'
        self.plan(self.worker,text,[{'action':'start','evidence':text,'data':{}}])
        self.assertEqual(r.tasks.get().state,'pending')
        answer,_=query(self.worker,r.code)
        self.assertNotIn('Política',answer)

    def test_last_event_query_count_does_not_grow_per_work_item(self):
        self.request(worker=self.worker)
        with CaptureQueriesContext(connection) as first:
            query(self.worker,'¿Qué trabajos tengo asignados?')
        for number in range(6):self.request(f'Trabajo {number}',self.worker)
        with CaptureQueriesContext(connection) as many:
            query(self.worker,'¿Qué trabajos tengo asignados?')
        self.assertLessEqual(len(many),len(first)+1)


@override_settings(CONTENT_ALLOWED_TELEGRAM_IDS=[], CONTENT_LIVE_TELEGRAM=True)
class ConsentTests(Accounts,Fixture,TestCase):
    def command(self,action,**kwargs):
        with patch('content_coordination.management.commands.content_participant.ensure_demo'):
            call_command('content_participant',action,stdout=StringIO(),**kwargs)

    def test_registration_requires_consent_and_does_not_send(self):
        with self.assertRaises(CommandError):
            self.command('register',username=self.worker.user.username,telegram_id='10000002',role='designer')
        self.assertFalse(TelegramConsent.objects.exists())
        self.command('register',username=self.worker.user.username,telegram_id='10000002',role='designer',consent_confirmed=True)
        self.worker.refresh_from_db()
        self.assertFalse(telegram_allowed(self.worker))
        self.assertTrue(telegram_allowed(self.worker,inbound_start=True))
        self.assertFalse(Outbox.objects.exists())

    def test_verification_only_from_private_start_and_revocation_blocks_both_directions(self):
        self.grant(self.worker,'10000002',verified=False)
        with self.assertRaises(PermissionDenied):queue_update(self.update('10000002',1,'/contenido'))
        queue_update(self.update('10000002',2,'/start'))
        self.assertTrue(telegram_allowed(self.worker))
        self.assertEqual(ParticipantAccessEvent.objects.filter(action='verified').count(),1)
        queue_update(self.update('10000002',2,'/start'))
        self.assertEqual(ParticipantAccessEvent.objects.filter(action='verified').count(),1)
        out=services.notify(self.worker,'Mensaje','revoked')
        self.command('revoke',username=self.worker.user.username)
        self.worker.refresh_from_db();out.refresh_from_db()
        self.assertFalse(telegram_allowed(self.worker));self.assertEqual(out.status,'blocked')
        with self.assertRaises(PermissionDenied):queue_update(self.update('10000002',3,'/start'))

    def test_revocation_blocks_an_already_queued_operation(self):
        self.grant(self.worker,'10000002')
        r=self.request(worker=self.worker)
        Conversation.objects.create(participant=self.worker,module='content')
        msg=queue_update(self.update('10000002',20,'Ya puedo comenzar'))
        self.command('revoke',username=self.worker.user.username)
        ai=Mock();ai.interpret.return_value={'updates':[{'action':'start','evidence':msg.text,'data':{}}]}
        Processor(ai).process(msg)
        ai.interpret.assert_not_called()
        self.assertEqual(r.tasks.get().state,'pending')

    def test_new_consent_does_not_activate_old_notices_or_change_roles(self):
        out=services.notify(self.worker,'Aviso anterior','old')
        with self.assertRaises(CommandError):
            self.command('register',username=self.worker.user.username,telegram_id='10000002',role='coordinator',consent_confirmed=True)
        self.command('register',username=self.worker.user.username,telegram_id='10000002',role='designer',consent_confirmed=True)
        out.refresh_from_db();self.assertEqual(out.status,'blocked')
        self.assertEqual(Participant.objects.get(pk=self.worker.pk).role,'designer')


class IndependentConcurrencyTests(Accounts,Fixture,TransactionTestCase):
    def test_supervisor_query_and_worker_progress_do_not_lose_updates(self):
        self.authorize_telegram(self.worker)
        r=self.request(worker=self.worker)
        def run(kind):
            close_old_connections()
            try:
                if kind=='query':return services.ask_updates(self.coordinator,self.worker,[r.tasks.get().pk])
                return services.execute(self.worker,r.pk,'progress',{'progress':'Me faltan 15 minutos','eta_minutes':15})
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(run,['query','progress']))
        self.assertEqual(r.tasks.get().eta_minutes,15)
        self.assertEqual(ProgressQuery.objects.get().worker,self.worker)
        answer,_=query(self.coordinator,f'¿Cómo está {r.code}?')
        self.assertIn('Me faltan 15 minutos',answer)

    def test_two_accounts_confirm_independent_assignments_simultaneously(self):
        # Administración y coordinación son participantes distintos; ningún ID real.
        first=self.request();second=self.request('Deportes')
        a=self.coordinator;b=self.actors['Admin']
        for actor,r,worker in ((a,first,self.worker),(b,second,self.other)):
            text=f'Asigna {r.code} a {worker.name}'
            self.plan(actor,text,[{'action':'assign','evidence':text,'data':{}}])
        msgs=[enqueue(a,'concurrent-a','confirmo',channel='dashboard'),enqueue(b,'concurrent-b','confirmo',channel='dashboard')]
        def run(pk):
            close_old_connections()
            try:return Processor(Mock()).process(Message.objects.get(pk=pk))
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(run,[m.pk for m in msgs]))
        self.assertEqual(first.tasks.get().assignee,self.worker)
        self.assertEqual(second.tasks.get().assignee,self.other)
