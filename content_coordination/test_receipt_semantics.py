"""Errores reales de extracción, con actores y PostgreSQL de pruebas."""
from django.test import TestCase
from .tests import Fixture
from .models import Conversation, History, Outbox
from .services import snapshot
from .ai import validate_plan, GeminiContentInterpreter


class ReceiptSemanticsTests(Fixture, TestCase):
    def say(self, r, text, proposed='receive', data=None, actor=None):
        text=text.replace('CT-000004',r.code)
        return self.plan(actor or self.worker,text,[{'action':proposed,'evidence':text,'data':data or {}}])

    def test_exact_real_message_overrides_wrong_receive_without_inheriting_switch_permissions(self):
        r=self.request('Gráfica del tiempo',self.worker)
        text='Ya recibí CT-000004 y voy a comenzar a trabajar en el diseño de la gráfica del tiempo.'
        reply,msg=self.say(r,text,data={'receipt_scope':'switch','reply_to_followup':False})
        r.refresh_from_db()
        self.assertEqual(r.state,'production')
        self.assertEqual(r.tasks.get().state,'working')
        self.assertEqual(r.history.filter(operation='start').count(),1)
        self.assertFalse(r.tasks.filter(kind='switch').exists())
        self.assertFalse(r.versions.exists())
        self.assertNotIn('requiere un responsable de Switch',reply)
        self.assertTrue(reply.startswith('✅ Diseño iniciado'))
        self.assertEqual(msg.result['interpretation']['updates'][0]['action'],'receive')
        self.assertEqual(msg.result['resolved_updates'][0]['action'],'start')

    def test_start_forms_with_or_without_ct_resolve_only_the_owned_design(self):
        texts=['Voy a comenzar el diseño de CT-000004','Recibí la tarea y voy a empezar',
               'Me asignaron el diseño y estoy comenzando','Comencé a trabajar en CT-000004']
        for text in texts:
            with self.subTest(text=text):
                r=self.request('Gráfica del tiempo',self.worker)
                _,msg=self.say(r,text)
                self.assertEqual(msg.status,'done')
                self.assertEqual(r.tasks.get().state,'working')
                # Apartar esta tarea ya iniciada; la próxima frase debe resolver
                # únicamente la nueva que aún está pendiente de comenzar.
                self.assertEqual(r.history.filter(operation='start').count(),1)

    def test_receiving_assignment_alone_acknowledges_without_mutating_task(self):
        r=self.request(worker=self.worker)
        for text in ['Ya recibí el trabajo','Me llegó la tarea','Recibido, voy a revisarlo']:
            with self.subTest(text=text):
                before=snapshot(r);count=History.objects.count()
                reply,msg=self.say(r,text,data={'receipt_scope':'assignment'})
                r.refresh_from_db()
                self.assertEqual(snapshot(r),before)
                self.assertEqual(History.objects.count(),count)
                self.assertEqual(msg.result['resolved_updates'][0]['action'],'acknowledge')
                self.assertTrue(reply.startswith('📩 Asignación recibida'))

    def test_partial_progress_and_remaining_time_never_submit_or_receive(self):
        r=self.request(worker=self.worker);self.action(self.worker,r,'start')
        for text,data in [('Llevo la mitad',{}),('Ya tengo avanzada la gráfica',{}),
                          ('Me faltan 15 minutos',{'eta_minutes':15}),
                          ('Me faltan aproximadamente 15 minutos',{'eta_minutes':15})]:
            with self.subTest(text=text):
                _,msg=self.say(r,text,proposed='receive',data=data)
                self.assertEqual(msg.result['resolved_updates'][0]['action'],'progress')
                r.refresh_from_db();self.assertEqual(r.state,'production')
                self.assertEqual(r.tasks.get().state,'working')
                self.assertIsNone(r.tasks.get().completed_at)
                self.assertFalse(r.versions.exists())
        self.assertEqual(r.tasks.get().eta_minutes,15)

    def test_submit_is_distinct_from_receipt_and_approval(self):
        r=self.request(worker=self.worker)
        self.say(r,'Entrego la versión 1.0 para revisión de CT-000004',proposed='submit',data={'version':'1.0'})
        r.refresh_from_db();self.assertEqual(r.state,'review')
        self.assertEqual(r.versions.get().state,'review')
        self.assertIsNone(r.versions.get().approved_at)
        self.assertIsNone(r.versions.get().received_at)

    def test_partial_with_contrast_and_negated_completion_preserves_the_whole_fact(self):
        r=self.request(worker=self.worker);self.action(self.worker,r,'start')
        text='Llevo la mitad, pero no he terminado el diseño de CT-000004'
        self.say(r,text,proposed='submit',data={'progress':'Llevo la mitad'})
        self.assertEqual(r.tasks.get().last_progress,text.replace('CT-000004',r.code))
        self.assertFalse(r.versions.exists())
        r.refresh_from_db();self.assertEqual(r.state,'production')

    def test_negated_remaining_time_cannot_be_saved_as_a_positive_eta(self):
        r=self.request(worker=self.worker);before=snapshot(r)
        self.say(r,'No me faltan 15 minutos para CT-000004',proposed='progress',data={'eta_minutes':15})
        r.refresh_from_db();self.assertEqual(snapshot(r),before)

    def edited(self):
        r=self.review()
        self.action(self.reviewer,r,'approve',{'version':'1.0'},confirmed=True)
        self.action(self.editor,r,'edit',{'version':'1.0'})
        r.refresh_from_db();return r

    def test_real_switch_reception_still_requires_switch_role_and_not_emission(self):
        r=self.edited();before=snapshot(r)
        text='Switch recibió la versión 1.0 aprobada de CT-000004'
        reply,_=self.say(r,text,data={'version':'1.0','receipt_scope':'switch'})
        self.assertIn('Switch autorizado',reply)
        r.refresh_from_db();self.assertEqual(snapshot(r),before)
        reply,_=self.say(r,text,data={'version':'1.0','receipt_scope':'switch'},actor=self.switch)
        self.assertIsNotNone(r.versions.get().received_at)
        self.assertIsNone(r.versions.get().emitted_at)
        self.assertIn('emisión sigue sin confirmar',reply)

    def test_ambiguous_receipt_clarifies_instead_of_rejecting_wrong_role(self):
        r=self.request(worker=self.worker);before=snapshot(r)
        reply,_=self.say(r,'Ya recibí CT-000004',data={'receipt_scope':'unspecified'})
        self.assertIn('asignación para diseñar o una versión en Switch',reply)
        self.assertNotIn('requiere un responsable de Switch',reply)
        r.refresh_from_db();self.assertEqual(snapshot(r),before)

    def test_foreign_task_cannot_be_started_by_reclassifying_receipt(self):
        r=self.request('Privado',self.other);before=snapshot(r)
        reply,_=self.say(r,'Ya recibí CT-000004 y voy a empezar el diseño')
        r.refresh_from_db();self.assertEqual(snapshot(r),before)
        self.assertFalse(r.history.filter(operation='start').exists())
        self.assertNotIn('✅',reply)

    def test_acknowledgement_and_start_clauses_collapse_to_one_operation(self):
        r=self.request(worker=self.worker)
        first=f'Ya recibí {r.code}';second='voy a comenzar a trabajar en el diseño'
        reply,msg=self.plan(self.worker,first+' y '+second,[
            {'action':'receive','evidence':first,'data':{}},
            {'action':'start','evidence':second,'data':{}}])
        self.assertEqual(len(msg.result['resolved_updates']),1)
        self.assertEqual(r.history.filter(operation='start').count(),1)
        self.assertEqual(reply.count('✅'),1)

    def test_independent_stages_in_one_message_require_clarification(self):
        r=self.edited();other=self.request('Segundo diseño',self.worker)
        before=snapshot(r),snapshot(other)
        a=f'Switch recibió la versión 1.0 de {r.code}';b=f'Voy a comenzar el diseño de {other.code}'
        reply,_=self.plan(self.actors['Admin'],a+' y '+b,[
            {'action':'receive','evidence':a,'data':{'version':'1.0'}},
            {'action':'start','evidence':b,'data':{}}])
        r.refresh_from_db();other.refresh_from_db()
        self.assertEqual((snapshot(r),snapshot(other)),before)
        self.assertIn('una a la vez',reply)

    def test_negated_hypothetical_and_quoted_start_never_mutate(self):
        r=self.request(worker=self.worker)
        for text in ['No voy a comenzar CT-000004','Cuando llegue voy a comenzar CT-000004',
                     'Daniela dijo: «Voy a comenzar CT-000004»','Si me asignan CT-000004 voy a empezar']:
            with self.subTest(text=text):
                before=snapshot(r)
                self.say(r,text,proposed='start')
                r.refresh_from_db();self.assertEqual(snapshot(r),before)

    def test_light_typo_understood_by_interpreter_retains_domain_safety(self):
        r=self.request(worker=self.worker)
        self.say(r,'Voy a comensar el diseño de CT-000004',proposed='start')
        self.assertEqual(r.tasks.get().state,'working')

    def test_replay_and_second_start_never_duplicate_transition(self):
        r=self.request(worker=self.worker)
        _,msg=self.say(r,'Voy a comenzar el diseño de CT-000004',proposed='start')
        from .processor import Processor
        from unittest.mock import Mock
        Processor(Mock()).process(msg)
        reply,_=self.say(r,'Ya recibí CT-000004 y voy a comenzar el diseño')
        self.assertEqual(r.history.filter(operation='start').count(),1)
        self.assertNotIn('✅',reply)

    def test_acknowledgement_does_not_clear_unrelated_confirmation(self):
        admin=self.actors['Admin'];r=self.request(worker=admin);other=self.request('Otro CT')
        text=f'Asigna {other.code} a Daniela'
        self.plan(admin,text,[{'action':'assign','evidence':text,'data':{}}])
        before=Conversation.objects.get(participant=admin).pending
        self.say(r,'Recibí la tarea de CT-000004',data={'receipt_scope':'assignment'},actor=admin)
        pending=Conversation.objects.get(participant=admin).pending
        self.assertEqual(pending['request_id'],before['request_id'])
        self.assertTrue(pending['needs_review'])
        self.plan(admin,'confirmo')
        self.assertIsNone(other.tasks.get().assignee)

    def test_contract_explicitly_distinguishes_receipt_object(self):
        self.assertIn('acknowledge',GeminiContentInterpreter.RESPONSE_SCHEMA['properties']['updates']['items']['properties']['action']['enum'])
        with self.assertRaises(ValueError):
            validate_plan({'updates':[{'action':'receive','evidence':'Recibí','data':{'receipt_scope':'designer-admin'}}]},'Recibí')
