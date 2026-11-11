import math
from contextlib import nullcontext
from io import StringIO
from uuid import uuid4
from unittest.mock import Mock, patch
from concurrent.futures import ThreadPoolExecutor

from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction, close_old_connections
from django.test import TestCase, TransactionTestCase, SimpleTestCase, override_settings
from django.utils import timezone

from .tests import Fixture
from .models import Participant, TelegramConsent, TelegramLinkTransfer, ParticipantAccessEvent, Conversation, Message, Outbox
from .access import telegram_allowed, message_binding_valid, content_only
from .routing import admit, queue_update
from .processor import Processor, enqueue
from .transfers import transfer_link, restore_link, guard_demo
from .services import execute


class TransferFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.source=self.actors['Admin'];self.target=self.coordinator
        self.source.telegram_id='10000001';self.source.telegram_authorized=True;self.source.save()
        TelegramConsent.objects.create(participant=self.source,telegram_id='10000001',consent_at=timezone.now(),verified_at=timezone.now())
        self.transfer_id=uuid4()
        self.guard=patch('content_coordination.transfers.guard_demo')
        self.guard.start();self.addCleanup(self.guard.stop)

    def transfer(self, **kwargs):
        return transfer_link(self.source.user.username,self.target.user.username,'10000001',self.source.user.username,
                             self.transfer_id,consent_confirmed=True,remote_queue_empty=True,**kwargs)

    def restore(self):
        return restore_link(self.transfer_id,self.source.user.username,consent_confirmed=True,remote_queue_empty=True)

    def fresh_start(self, actor, update_id):
        consent=TelegramConsent.objects.get(participant=actor)
        update={'update_id':update_id,'message':{'message_id':update_id,'date':math.ceil(consent.bound_at.timestamp())+2,
                'from':{'id':10000001},'chat':{'id':10000001,'type':'private'},'text':'/start'}}
        return queue_update(update)


class TransferTests(TransferFixture,TestCase):
    def test_transfer_and_restore_preserve_users_roles_requests_and_conversations(self):
        r=self.request(worker=self.source)
        Outbox.objects.update(status='blocked')
        Conversation.objects.create(participant=self.source,module='content',draft={'title':'Borrador admin'},pending={'action':'query'},context={'focus':r.pk})
        Conversation.objects.create(participant=self.target,module='content',draft={'title':'Borrador coordinador'})
        before=list(Conversation.objects.order_by('pk').values())
        row,changed=self.transfer()
        self.source.refresh_from_db();self.target.refresh_from_db()
        self.assertTrue(changed);self.assertEqual(row.state,'active')
        self.assertIsNone(self.source.telegram_id);self.assertFalse(self.source.telegram_authorized)
        self.assertEqual(self.source.role,'admin');self.assertTrue(self.source.active)
        self.assertEqual(self.target.telegram_id,'10000001');self.assertEqual(self.target.role,'coordinator')
        self.assertFalse(telegram_allowed(self.target))
        self.assertTrue(telegram_allowed(self.target,inbound_start=True))
        self.assertIsNone(TelegramConsent.objects.get(participant=self.target).verified_at)
        self.assertEqual(before,list(Conversation.objects.order_by('pk').values()))
        restored,_=self.restore()
        self.assertEqual(restored.state,'restored')
        self.source.refresh_from_db();self.target.refresh_from_db()
        self.assertEqual(self.source.telegram_id,'10000001');self.assertTrue(self.source.telegram_authorized)
        self.assertIsNone(self.target.telegram_id);self.assertFalse(self.target.telegram_authorized)
        self.assertFalse(telegram_allowed(self.source))  # Sin excepción a /start tras restaurar.
        self.assertIsNone(TelegramConsent.objects.get(participant=self.source).verified_at)
        self.assertEqual(r.tasks.get().assignee,self.source)
        self.assertEqual(before,list(Conversation.objects.order_by('pk').values()))
        self.assertEqual(ParticipantAccessEvent.objects.filter(action__startswith='transfer').count(),4)

    def test_legacy_administrator_is_restored_with_fresh_verification_not_allowlist_bypass(self):
        TelegramConsent.objects.all().delete()  # Solo fixture temporal; simula vínculo histórico.
        with override_settings(CONTENT_ALLOWED_TELEGRAM_IDS=['10000001']):
            self.transfer();self.restore();self.source.refresh_from_db()
            self.assertFalse(telegram_allowed(self.source))
            msg=self.fresh_start(self.source,101)
            self.assertTrue(telegram_allowed(self.source))
            self.assertTrue(message_binding_valid(self.source,msg))

    def test_idempotence_does_not_repeat_audit_or_reset_verified_account(self):
        self.transfer();self.target.refresh_from_db()
        msg=self.fresh_start(self.target,102)
        Message.objects.filter(pk=msg.pk).update(status='done')
        verified=TelegramConsent.objects.get(participant=self.target).verified_at
        _,changed=self.transfer()
        self.assertFalse(changed)
        self.assertEqual(TelegramConsent.objects.get(participant=self.target).verified_at,verified)
        self.restore();_,changed=self.restore();self.assertFalse(changed)
        _,changed=self.transfer();self.assertFalse(changed)
        self.assertEqual(TelegramLinkTransfer.objects.get().state,'restored')
        self.assertEqual(ParticipantAccessEvent.objects.filter(action__startswith='transfer').count(),4)

    def test_other_operator_and_missing_consents_are_rejected(self):
        args=(self.source.user.username,self.target.user.username,'10000001',self.worker.user.username,self.transfer_id)
        with self.assertRaises(CommandError):transfer_link(*args,consent_confirmed=True,remote_queue_empty=True)
        args=args[:3]+(self.source.user.username,self.transfer_id)
        with self.assertRaises(CommandError):transfer_link(*args,consent_confirmed=False,remote_queue_empty=True)
        with self.assertRaises(CommandError):transfer_link(*args,consent_confirmed=True,remote_queue_empty=False)
        self.assertFalse(TelegramLinkTransfer.objects.exists())

    def test_wrong_id_busy_target_and_inactive_accounts_are_rejected(self):
        args=(self.source.user.username,self.target.user.username,'99999999',self.source.user.username,self.transfer_id)
        with self.assertRaises(CommandError):transfer_link(*args,consent_confirmed=True,remote_queue_empty=True)
        self.target.telegram_id='10000002';self.target.save()
        with self.assertRaises(CommandError):self.transfer()
        self.target.telegram_id=None;self.target.active=False;self.target.save()
        with self.assertRaises(CommandError):self.transfer()

    def test_unverified_source_is_rejected(self):
        TelegramConsent.objects.filter(participant=self.source).update(verified_at=None)
        with self.assertRaises(CommandError):self.transfer()

    def test_local_queued_or_uncertain_messages_block_without_deleting(self):
        msg=enqueue(self.source,'pending','Mensaje previo',channel='dashboard')
        with self.assertRaisesMessage(CommandError,'inbox'):self.transfer()
        msg.refresh_from_db();self.assertEqual(msg.status,'pending')
        msg.status='done';msg.save()
        out=Outbox.objects.create(recipient=self.target,key='uncertain',text='Aviso',status='uncertain')
        with self.assertRaisesMessage(CommandError,'outbox'):self.transfer()
        out.refresh_from_db();self.assertEqual(out.status,'uncertain')

    def test_database_rejects_two_participants_and_two_active_consents_with_same_id(self):
        with self.assertRaises(IntegrityError),transaction.atomic():
            Participant.objects.create(name='Otro',role='designer',telegram_id='10000001')
        with self.assertRaises(IntegrityError),transaction.atomic():
            TelegramConsent.objects.create(participant=self.target,telegram_id='10000001',consent_at=timezone.now())
        self.transfer()
        self.assertEqual(TelegramConsent.objects.filter(telegram_id='10000001',revoked_at__isnull=True).count(),1)

    def test_old_remote_start_or_other_private_message_cannot_verify_new_owner(self):
        self.transfer();self.target.refresh_from_db()
        consent=TelegramConsent.objects.get(participant=self.target)
        update={'update_id':103,'message':{'message_id':103,'date':math.floor(consent.bound_at.timestamp())-1,
                'from':{'id':10000001},'chat':{'id':10000001,'type':'private'},'text':'/start'}}
        with self.assertRaises(PermissionDenied):queue_update(update)
        update['message']['date']=math.ceil(consent.bound_at.timestamp())+2
        update['message']['text']='confirmo'
        with self.assertRaises(PermissionDenied):queue_update(update)
        self.assertIsNone(TelegramConsent.objects.get(participant=self.target).verified_at)
        self.assertFalse(Message.objects.exists())

    def test_fresh_start_verifies_only_private_sender_and_deduplicates(self):
        self.transfer();self.target.refresh_from_db()
        msg=self.fresh_start(self.target,104)
        duplicate=queue_update(msg.payload['update'])
        self.assertEqual(msg.pk,duplicate.pk)
        self.assertEqual(ParticipantAccessEvent.objects.filter(action='verified').count(),1)
        self.assertTrue(telegram_allowed(self.target))

    def test_old_local_message_cannot_execute_when_original_id_is_restored(self):
        msg=enqueue(self.source,'old-confirm','confirmo',channel='telegram')
        msg.status='done';msg.save()
        self.transfer();self.restore();self.source.refresh_from_db()
        fresh=self.fresh_start(self.source,105)
        Message.objects.filter(pk=fresh.pk).update(status='done')
        self.assertFalse(message_binding_valid(self.source,msg))
        Message.objects.filter(pk=msg.pk).update(status='pending')  # Emula reintento externo inválido.
        ai=Mock();reply=Processor(ai).process(msg)
        self.assertIn('no está autorizada',reply);ai.interpret.assert_not_called()

    def test_fernando_does_not_inherit_administrator_permissions_or_incident_authorization(self):
        self.transfer();self.target.refresh_from_db()
        msg=self.fresh_start(self.target,106);Message.objects.filter(pk=msg.pk).update(status='done')
        with self.assertRaises(PermissionDenied):
            r=self.request(worker=self.worker)
            execute(self.target,r.pk,'start',{})
        Outbox.objects.update(status='blocked')
        update=msg.payload['update'];update['update_id']=107;update['message']['message_id']=107;update['message']['text']='/incidencia'
        command=queue_update(update)
        reply=Processor(Mock()).process(command)
        self.assertIn('no está autorizada para Incidencias',reply)
        self.assertTrue(content_only(self.target))

    def test_restore_rejects_unexpected_link_change_and_wrong_operator(self):
        self.transfer()
        with self.assertRaises(CommandError):restore_link(self.transfer_id,self.worker.user.username,consent_confirmed=True,remote_queue_empty=True)
        Participant.objects.filter(pk=self.target.pk).update(telegram_id='10000003')
        with self.assertRaisesMessage(CommandError,'cambiaron'):self.restore()
        self.assertEqual(TelegramLinkTransfer.objects.get().state,'active')

    def test_cli_refuses_running_receiver_without_touching_links(self):
        module='content_coordination.management.commands.content_telegram_transfer'
        with patch(module+'.guard_demo'),patch(module+'.check_other_receivers'),patch(module+'.receiver_lock',side_effect=CommandError('Receptor activo')):
            with self.assertRaisesMessage(CommandError,'Receptor activo'):
                call_command('content_telegram_transfer','transfer',source=self.source.user.username,target=self.target.user.username,
                             telegram_id='10000001',operator=self.source.user.username,transfer_id=str(self.transfer_id),
                             consent_confirmed=True,remote_queue_empty_confirmed=True,stdout=StringIO())
        self.assertFalse(TelegramLinkTransfer.objects.exists())

    def test_cli_plan_is_read_only(self):
        module='content_coordination.management.commands.content_telegram_transfer'
        with patch(module+'.guard_demo'):
            call_command('content_telegram_transfer','plan',source=self.source.user.username,target=self.target.user.username,
                         telegram_id='10000001',operator=self.source.user.username,stdout=StringIO())
        self.assertFalse(TelegramLinkTransfer.objects.exists())
        self.assertFalse(ParticipantAccessEvent.objects.exists())

    def test_cli_successful_transfer_show_restore_never_contacts_telegram(self):
        module='content_coordination.management.commands.content_telegram_transfer'
        with patch(module+'.guard_demo'),patch(module+'.check_other_receivers'),patch(module+'.receiver_lock',return_value=nullcontext()), \
                override_settings(TELEGRAM_BOT_TOKEN='unit-test-only'), \
                patch('incidents.messaging.telegram_provider.httpx.post') as post:
            common={'operator':self.source.user.username,'transfer_id':str(self.transfer_id),'stdout':StringIO()}
            call_command('content_telegram_transfer','transfer',source=self.source.user.username,target=self.target.user.username,
                         telegram_id='10000001',consent_confirmed=True,remote_queue_empty_confirmed=True,**common)
            call_command('content_telegram_transfer','show',**common)
            call_command('content_telegram_transfer','restore',consent_confirmed=True,remote_queue_empty_confirmed=True,**common)
            post.assert_not_called()
        self.assertEqual(TelegramLinkTransfer.objects.get().state,'restored')

    def test_inactive_django_user_cannot_receive_a_transfer(self):
        self.target.user.is_active=False;self.target.user.save()
        with self.assertRaises(CommandError):self.transfer()
        self.assertFalse(TelegramLinkTransfer.objects.exists())

    def test_local_pending_target_message_blocks_restore_and_is_preserved(self):
        self.transfer()
        msg=enqueue(self.target,'pending-target','Avance pendiente',channel='dashboard')
        with self.assertRaises(CommandError):self.restore()
        self.assertEqual(TelegramLinkTransfer.objects.get().state,'active')
        msg.refresh_from_db();self.assertEqual(msg.status,'pending')


class TransferConcurrencyTests(TransferFixture,TransactionTestCase):
    def test_simultaneous_same_operation_is_executed_once(self):
        def run(_):
            close_old_connections()
            try:return self.transfer()[1]
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,[1,2]))
        self.assertEqual(sorted(results),[False,True])
        self.assertEqual(TelegramLinkTransfer.objects.count(),1)
        self.assertEqual(ParticipantAccessEvent.objects.filter(action__startswith='transfer').count(),2)


class DemoOnlyGuardTests(SimpleTestCase):
    @override_settings(CONTENT_COORDINATION_ENABLED=False)
    def test_guard_rejects_non_demo_configuration_without_any_database_write(self):
        with self.assertRaises(CommandError):guard_demo()
