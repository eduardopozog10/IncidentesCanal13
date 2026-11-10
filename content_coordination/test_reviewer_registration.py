"""Registro y aviso de revisor: PostgreSQL temporal y Telegram simulado."""
import json
import math
from io import StringIO
from unittest.mock import Mock, patch
from concurrent.futures import ThreadPoolExecutor

from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.exceptions import PermissionDenied
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, SimpleTestCase, override_settings
from django.utils import timezone

from .tests import Fixture
from .models import Participant, TelegramConsent, ParticipantAccessEvent, Outbox, Message
from .access import telegram_allowed
from .routing import queue_update
from .processor import Processor
from .services import snapshot
from .delivery import deliver_one


class ReviewerFixture(Fixture):
    def setUp(self):
        super().setUp()
        for name in ['content_coordination.management.commands.content_participant.ensure_demo',
                     'content_coordination.management.commands.run_secuoya_demo.ensure_demo']:
            guard=patch(name);guard.start();self.addCleanup(guard.stop)
        self.sequence=1000

    def command(self,action,**kwargs):
        output=StringIO()
        call_command('content_participant',action,username=self.reviewer.user.username,stdout=output,**kwargs)
        return output.getvalue()

    def register(self):
        return self.command('register',role='reviewer',telegram_id='10000007',consent_confirmed=True)

    def update(self,text,**changes):
        self.sequence+=1
        c=TelegramConsent.objects.get(participant=self.reviewer)
        data={'update_id':self.sequence,'message':{'message_id':self.sequence,
            'date':math.ceil(c.bound_at.timestamp())+2,'from':{'id':10000007},
            'chat':{'id':10000007,'type':'private'},'text':text}}
        data['message'].update(changes)
        return data

    def send(self,text,action=None,data=None):
        msg=queue_update(self.update(text))
        interpreter=Mock();interpreter.interpret.return_value={'updates':[{'action':action or 'query','evidence':text,'data':data or {}}]}
        reply=Processor(interpreter).process(msg)
        return msg,reply

    def verify(self):
        self.send('/start');self.send('/contenido')
        self.reviewer.refresh_from_db()
        self.assertTrue(telegram_allowed(self.reviewer))

    def assigned_review(self):
        r=self.review()
        self.action(self.coordinator,r,'assign_review',{'assignee_id':self.reviewer.pk},confirmed=True)
        r.refresh_from_db()
        return r,Outbox.objects.get(recipient=self.reviewer,key__startswith='assign_review:')

    def release(self,row):
        return self.command('release-review',notice_id=row.pk,operator=self.actors['Admin'].user.username,release_confirmed=True)


@override_settings(CONTENT_ALLOWED_TELEGRAM_IDS=[],CONTENT_LIVE_TELEGRAM=True)
class ReviewerRegistrationTests(ReviewerFixture,TestCase):
    def test_cli_accepts_reviewer_without_changing_its_role_and_never_sends(self):
        with patch('incidents.messaging.telegram_provider.httpx.post') as post:
            call_command('content_participant','register','--username',self.reviewer.user.username,
                '--role','reviewer','--telegram-id','10000007','--consent-confirmed',stdout=StringIO())
            post.assert_not_called()
        self.reviewer.refresh_from_db()
        self.assertEqual(self.reviewer.role,'reviewer');self.assertEqual(self.reviewer.telegram_id,'10000007')
        self.assertTrue(self.reviewer.telegram_authorized)
        self.assertFalse(telegram_allowed(self.reviewer))
        c=TelegramConsent.objects.get(participant=self.reviewer)
        self.assertIsNone(c.verified_at);self.assertIsNotNone(c.bound_at)
        self.assertEqual(ParticipantAccessEvent.objects.get().action,'consent')

    def test_consent_and_explicit_supported_role_are_required_in_backend(self):
        for role in ['reviewer','journalist','editor','switch','admin']:
            with self.subTest(role=role),self.assertRaises(CommandError):
                self.command('register',role=role,telegram_id='10000007',consent_confirmed=role!='reviewer')
        self.assertFalse(TelegramConsent.objects.exists())

    def test_existing_role_and_telegram_binding_cannot_be_overwritten(self):
        with self.assertRaises(CommandError):
            self.command('register',role='designer',telegram_id='10000007',consent_confirmed=True)
        self.register()
        with self.assertRaises(CommandError):
            self.command('register',role='reviewer',telegram_id='10000008',consent_confirmed=True)
        self.reviewer.refresh_from_db();self.assertEqual(self.reviewer.telegram_id,'10000007')

    def test_invalid_or_noncanonical_id_is_rejected_without_registering(self):
        for identity in ['0','-1','00010000007','١٢٣','',None,'9'*31]:
            with self.subTest(identity=identity),self.assertRaises(CommandError):
                self.command('register',role='reviewer',telegram_id=identity,consent_confirmed=True)
        self.assertFalse(TelegramConsent.objects.exists())
        self.assertFalse(ParticipantAccessEvent.objects.exists())

    def test_inactive_participant_or_user_is_not_reactivated(self):
        self.reviewer.active=False;self.reviewer.save()
        with self.assertRaises(CommandError):self.register()
        self.reviewer.active=True;self.reviewer.save()
        self.reviewer.user.is_active=False;self.reviewer.user.save()
        with self.assertRaises(CommandError):self.register()
        self.assertFalse(TelegramConsent.objects.exists())

    def test_duplicate_telegram_or_active_consent_is_rejected(self):
        self.worker.telegram_id='10000007';self.worker.save()
        with self.assertRaises(CommandError):self.register()
        self.worker.telegram_id=None;self.worker.save()
        TelegramConsent.objects.create(participant=self.worker,telegram_id='10000007',consent_at=timezone.now())
        with self.assertRaises(CommandError):self.register()
        self.reviewer.refresh_from_db();self.assertIsNone(self.reviewer.telegram_id)

    def test_idempotent_registration_preserves_real_verification_and_audit(self):
        self.register();self.verify()
        before=list(TelegramConsent.objects.values())
        count=ParticipantAccessEvent.objects.count()
        self.register()
        self.assertEqual(list(TelegramConsent.objects.values()),before)
        self.assertEqual(ParticipantAccessEvent.objects.count(),count)

    def test_only_fresh_private_start_can_verify_and_its_replay_is_deduplicated(self):
        self.register()
        with self.assertRaises(PermissionDenied):queue_update(self.update('/contenido'))
        c=TelegramConsent.objects.get(participant=self.reviewer)
        bad=[self.update('/start',date=math.floor(c.bound_at.timestamp())-5),
             self.update('/start',chat={'id':-1,'type':'group'}),
             self.update('/start',chat={'id':10000008,'type':'private'}),
             self.update('/start',**{'from':{'id':10000008}})]
        for update in bad:
            with self.assertRaises(PermissionDenied):queue_update(update)
        fresh=self.update('/start')
        msg=queue_update(fresh);again=queue_update(fresh)
        self.assertEqual(msg.pk,again.pk)
        self.assertEqual(ParticipantAccessEvent.objects.filter(action='verified').count(),1)
        state=json.loads(self.command('show'))
        self.assertTrue(state['verified']);self.assertTrue(state['delivery_eligible'])

    def test_registration_blocks_historical_notices_but_does_not_change_review(self):
        r,assignment=self.assigned_review();before=snapshot(r)
        self.register();self.verify()
        r.refresh_from_db();self.assertEqual(snapshot(r),before)
        self.assertFalse(Outbox.objects.filter(recipient=self.reviewer,status__in=['pending','retry'],key__startswith='assign_review:').exists())
        assignment.refresh_from_db();self.assertEqual(assignment.status,'blocked')
        self.assertEqual(assignment.attempts,0)
        self.assertIsNone(assignment.sent_at)
        plan=json.loads(self.command('review-notices'))
        self.assertEqual(plan[0]['notice_id'],assignment.pk)
        self.assertTrue(plan[0]['release_eligible'])

    def test_new_assignment_after_verification_delivers_once_without_releasing_history(self):
        self.register();self.verify()
        r=self.review()
        self.action(self.coordinator,r,'assign_review',{'assignee_id':self.reviewer.pk},confirmed=True)
        row=Outbox.objects.get(recipient=self.reviewer,key__startswith='assign_review:')
        Outbox.objects.exclude(pk=row.pk).update(status='local')
        sender=Mock();sender.send_text.return_value={'result':{'message_id':812}}
        deliver_one(sender);deliver_one(sender)
        sender.send_text.assert_called_once()
        self.assertEqual(sender.send_text.call_args.args[0],'10000007')

    def test_explicit_release_reuses_exact_row_is_audited_and_not_a_delivery_claim(self):
        r,row=self.assigned_review();self.register();self.verify()
        before=snapshot(r);total=Outbox.objects.count()
        first=json.loads(self.release(row));second=json.loads(self.release(row))
        self.assertTrue(first['changed']);self.assertFalse(second['changed'])
        self.assertEqual(first['status'],'pending')
        self.assertEqual(Outbox.objects.count(),total)
        self.assertEqual(ParticipantAccessEvent.objects.filter(action='review_notice_release').count(),1)
        r.refresh_from_db();self.assertEqual(snapshot(r),before)
        self.assertEqual(Outbox.objects.get(recipient=self.reviewer,key__startswith='review:').status,'blocked')

    def test_release_requires_verification_confirmation_and_local_admin(self):
        _,row=self.assigned_review();self.register()
        with self.assertRaises(CommandError):self.release(row)
        self.verify()
        with self.assertRaises(CommandError):self.command('release-review',notice_id=row.pk,operator=self.coordinator.user.username,release_confirmed=True)
        with self.assertRaises(CommandError):self.command('release-review',notice_id=row.pk,operator=self.actors['Admin'].user.username)
        row.refresh_from_db();self.assertEqual(row.status,'blocked')

    def test_release_rejects_uncertain_sent_partial_old_and_foreign_notices(self):
        _,row=self.assigned_review();self.register();self.verify()
        for status,changes in [('sent',{}),('uncertain',{}),('failed',{}),('blocked',{'attempts':1}),('blocked',{'delivery_parts':[{'message_id':'1'}]})]:
            Outbox.objects.filter(pk=row.pk).update(**{'status':status,'attempts':0,'delivery_parts':[],**changes})
            with self.assertRaises(CommandError):self.release(row)
            Outbox.objects.filter(pk=row.pk).update(attempts=0,delivery_parts=[],status='blocked')
        generic=Outbox.objects.get(recipient=self.reviewer,key__startswith='review:')
        with self.assertRaises(CommandError):self.release(generic)
        with self.assertRaises(CommandError):self.release(Outbox.objects.create(recipient=self.worker,key='foreign',text='Privado',status='blocked'))

    def test_reassignment_or_approval_makes_old_notice_ineligible(self):
        r,row=self.assigned_review();self.register();self.verify()
        self.action(self.coordinator,r,'assign_review',{'assignee_id':self.actors['Admin'].pk},confirmed=True)
        with self.assertRaises(CommandError):self.release(row)
        self.action(self.coordinator,r,'assign_review',{'assignee_id':self.reviewer.pk},confirmed=True)
        with self.assertRaises(CommandError):self.release(row)  # Hay una asignación posterior.

    def test_completed_review_cannot_release_a_historical_assignment(self):
        r,row=self.assigned_review();self.register();self.verify()
        self.action(self.reviewer,r,'approve',{'version':'1.0'},confirmed=True)
        with self.assertRaises(CommandError):self.release(row)
        row.refresh_from_db();self.assertEqual(row.status,'blocked')

    def test_delivered_review_notice_cannot_be_sent_again_by_repeating_release(self):
        _,row=self.assigned_review();self.register();self.verify();self.release(row)
        Outbox.objects.exclude(pk=row.pk).filter(status='pending').update(status='local')
        sender=Mock();sender.send_text.return_value={'result':{'message_id':813}}
        deliver_one(sender)
        output=json.loads(self.release(row))
        self.assertFalse(output['changed']);self.assertEqual(output['status'],'sent')
        self.assertIsNone(deliver_one(sender));sender.send_text.assert_called_once()

    def test_revocation_and_reconsent_require_another_start_without_releasing_history(self):
        r,row=self.assigned_review();self.register();self.verify();self.release(row)
        old=self.update('/start')
        self.command('revoke')
        with self.assertRaises(PermissionDenied):queue_update(self.update('/start'))
        self.register()
        self.reviewer.refresh_from_db();self.assertFalse(telegram_allowed(self.reviewer))
        old['message']['date']=0
        with self.assertRaises(PermissionDenied):queue_update(old)
        self.verify();row.refresh_from_db();self.assertEqual(row.status,'blocked')
        self.assertEqual(r.versions.get().state,'review')

    def test_revocation_prevents_delivery_of_the_released_review_notice(self):
        _,row=self.assigned_review();self.register();self.verify();self.release(row)
        Outbox.objects.exclude(pk=row.pk).filter(status__in=['pending','retry']).update(status='local')
        self.command('revoke')
        sender=Mock();self.assertIsNone(deliver_one(sender));sender.send_text.assert_not_called()

    def test_reviewer_can_confirm_own_review_but_cannot_design_edit_switch_or_emit(self):
        r,_=self.assigned_review();self.register();self.verify()
        for action,text in [('start',f'Voy a comenzar el diseño de {r.code}'),
                            ('edit',f'Incorporé la versión 1.0 de {r.code} a la nota'),
                            ('receive',f'Switch recibió la versión 1.0 de {r.code}'),
                            ('emit',f'Confirma la emisión de la versión 1.0 de {r.code}')]:
            self.send(text,action,{'version':'1.0'})
            self.assertIsNone(r.versions.get().approved_at)
            self.assertIsNone(r.versions.get().edited_at);self.assertIsNone(r.versions.get().received_at)
        text=f'Apruebo la versión 1.0 de {r.code}'
        prompt,reply=self.send(text,'approve',{'version':'1.0'})
        self.assertIn('Confirmación pendiente',reply)
        self.assertIsNone(r.versions.get().approved_at)
        self.send('confirmo')
        self.assertIsNone(r.versions.get().approved_at)  # Resumen no entregado.
        Outbox.objects.filter(key=f'reply:{prompt.pk}').update(status='sent',sent_at=timezone.now())
        self.send('confirmo')
        self.assertEqual(r.versions.get().approved_by,self.reviewer)

    def test_reviewer_cannot_approve_another_assigned_review(self):
        r=self.review();self.action(self.coordinator,r,'assign_review',{'assignee_id':self.actors['Admin'].pk},confirmed=True)
        self.register();self.verify()
        self.send(f'Apruebo la versión 1.0 de {r.code}','approve',{'version':'1.0'})
        self.send('confirmo')
        self.assertIsNone(r.versions.get().approved_at)


class ReviewerRegistrationConcurrencyTests(ReviewerFixture,TransactionTestCase):
    def test_concurrent_same_id_cannot_bind_two_participants(self):
        other=Participant.objects.create(user=type(self.reviewer.user).objects.create_user('otro_revisor'),name='Otro revisor',role='reviewer')
        def run(actor):
            close_old_connections()
            try:
                call_command('content_participant','register',username=actor.user.username,role='reviewer',telegram_id='10000007',consent_confirmed=True,stdout=StringIO())
                return 'registered'
            except CommandError:return 'rejected'
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,[self.reviewer,other]))
        self.assertEqual(sorted(results),['registered','rejected'])
        self.assertEqual(Participant.objects.filter(telegram_id='10000007').count(),1)
        self.assertEqual(TelegramConsent.objects.filter(telegram_id='10000007',revoked_at__isnull=True).count(),1)

    def test_concurrent_release_keeps_one_audit_event_and_one_existing_outbox(self):
        _,row=self.assigned_review();self.register();self.verify()
        count=Outbox.objects.count()
        def run(_):
            close_old_connections()
            try:return json.loads(self.release(row))['changed']
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,[1,2]))
        self.assertEqual(sorted(results),[False,True])
        self.assertEqual(Outbox.objects.count(),count)
        self.assertEqual(ParticipantAccessEvent.objects.filter(action='review_notice_release').count(),1)


class ReviewerDemoGuardTests(SimpleTestCase):
    @override_settings(CONTENT_COORDINATION_ENABLED=False)
    def test_non_demo_command_is_rejected_before_accessing_the_database(self):
        with self.assertRaises(CommandError):
            call_command('content_participant','register',username='demo_revisor',role='reviewer',
                         telegram_id='10000007',consent_confirmed=True,stdout=StringIO())
