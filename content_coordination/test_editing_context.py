from django.test import TestCase
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from .tests import Fixture
from .models import Participant, Conversation, Outbox
from .reporting import query
from .interpretation import versions_in
from .presentation import progress_steps, work_info


class EditingContextTests(Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.actors['Admin']
        self.admin.name = 'Eduardo Pozo'
        self.admin.save()
        self.control = self.request('Control sin tocar', self.other)
        self.control_state = self.control.state

    def approved(self):
        r = self.review()
        self.action(self.reviewer, r, 'approve', {'version':'1.0'}, confirmed=True)
        r.refresh_from_db()
        return r

    def say(self, text, action, data=None, actor=None):
        return self.plan(actor or self.admin, text, [{'action':action,'evidence':text,'data':data or {}}])

    def test_exact_editing_statement_corrects_gemini_receive_label(self):
        r = self.approved()
        text = f'Ya incorporé la versión 1.0 aprobada de {r.code} a la nota del noticiario de las 21:00'
        reply, msg = self.say(text, 'receive', {'version':'1.0'})
        r.refresh_from_db()
        self.assertEqual(r.state, 'editing')
        self.assertEqual(msg.status, 'done')
        self.assertIn('incorporada a la nota', reply)
        v = r.versions.get()
        self.assertEqual(v.edited_by, self.admin)
        self.assertIsNone(v.received_at)
        self.assertIsNone(v.emitted_at)
        self.assertEqual(r.tasks.get(kind='switch').state, 'pending')
        self.assertIsNone(r.tasks.get(kind='switch').assignee)

    def test_assign_editor_is_confirmed_and_preserves_other_stages(self):
        r = self.approved()
        before = list(r.tasks.exclude(kind='editing').order_by('pk').values())
        text = f'Asigna la etapa de Edición de {r.code} a Eduardo Pozo'
        reply, _ = self.say(text, 'assign')
        self.assertIn('Edición', reply)
        self.assertIsNone(r.tasks.get(kind='editing').assignee)
        self.say('confirmo', 'unknown')
        self.assertEqual(r.tasks.get(kind='editing').assignee, self.admin)
        self.assertEqual(list(r.tasks.exclude(kind='editing').order_by('pk').values()), before)
        self.assertEqual(r.history.filter(operation='assign_edit').count(), 1)
        self.assertTrue(Outbox.objects.filter(key__startswith='assign_edit:').exists())

    def test_assignment_checks_permissions_eligible_role_and_version(self):
        r = self.approved()
        with self.assertRaises(PermissionDenied):
            self.action(self.worker, r, 'assign_edit', {'assignee_id':self.editor.pk}, confirmed=True)
        with self.assertRaises(ValidationError):
            self.action(self.coordinator, r, 'assign_edit', {'assignee_id':self.worker.pk}, confirmed=True)
        with self.assertRaises(ValidationError):
            self.action(self.coordinator, r, 'assign_edit', {'assignee_id':self.editor.pk})
        before = self.review()
        with self.assertRaises(ValidationError):
            self.action(self.coordinator, before, 'assign_edit', {'assignee_id':self.editor.pk}, confirmed=True)

    def test_reassign_editor_only_while_editing_pending(self):
        r = self.approved()
        self.action(self.coordinator, r, 'assign_edit', {'assignee_id':self.editor.pk}, confirmed=True)
        self.action(self.coordinator, r, 'assign_edit', {'assignee_id':self.admin.pk}, confirmed=True)
        self.assertEqual(r.tasks.get(kind='editing').assignee, self.admin)
        self.action(self.admin, r, 'edit', {'version':'1.0'})
        with self.assertRaises(ValidationError):
            self.action(self.coordinator, r, 'assign_edit', {'assignee_id':self.editor.pk}, confirmed=True)

    def test_read_query_does_not_filter_by_participant_named_editor(self):
        r = self.approved()
        text = f'¿Qué falta para incorporar la versión 1.0 aprobada de {r.code} a Edición? ¿Hay alguien asignado como editor?'
        # El solicitante puede leer el trabajo sin ser editor ni tener Edición asignada.
        answer, _ = query(self.journalist, text)
        self.assertIn(r.code, answer)
        self.assertIn('Edición: Sin asignar', answer)
        self.assertIn('Versión 1.0: aprobada', answer)
        self.assertNotIn('Última aprobada: 1.0', answer)  # No repetir la misma aprobación.
        self.assertIn('editor autorizado', answer)
        self.assertIn('Después de Edición', answer)
        reply, _ = self.say(text, 'edit', {'version':'1.0'}, actor=self.journalist)
        self.assertIn('Edición: Sin asignar', reply)
        self.assertIsNone(r.versions.get().edited_at)

    def test_query_names_assigned_editor_and_never_grants_write_permission(self):
        r = self.approved()
        self.action(self.coordinator, r, 'assign_edit', {'assignee_id':self.editor.pk}, confirmed=True)
        answer, _ = query(self.journalist, f'¿Quién es el editor de {r.code} y qué falta para incorporar?')
        self.assertIn('Edición: Editor', answer)
        self.assertIn('Falta que Editor', answer)
        with self.assertRaises(PermissionDenied):
            self.action(self.journalist, r, 'edit', {'version':'1.0'})

    def test_editor_cannot_record_another_editors_work(self):
        r = self.approved()
        self.action(self.coordinator, r, 'assign_edit', {'assignee_id':self.editor.pk}, confirmed=True)
        other = Participant.objects.create(name='Otra editora', role='editor')
        # Se concede solo lectura mediante la solicitud, no la tarea de Edición.
        r.requester = other; r.save()
        with self.assertRaisesMessage(PermissionDenied, 'otro trabajador'):
            self.action(other, r, 'edit', {'version':'1.0'})
        self.assertIsNone(r.versions.get().edited_at)

    def test_unapproved_version_is_not_incorporated(self):
        r = self.review()
        with self.assertRaises(ValidationError):
            self.action(self.admin, r, 'edit', {'version':'1.0'})
        self.assertIsNone(r.versions.get().edited_at)
        self.assertFalse(r.tasks.filter(kind='switch').exists())

    def test_versions_accept_punctuation_prefix_and_words(self):
        for text in ('versión 1.0.', 'v1.0', 'la 1.0', 'versión uno punto cero'):
            with self.subTest(text=text):
                self.assertEqual(versions_in(text), ['1.0'])
        self.assertEqual(versions_in('CT-000002 a las 21:00'), [])
        self.assertNotIn('1.0', versions_in('1.0.2'))

    def test_exact_approval_with_final_period_preserves_version(self):
        r = self.review()
        text = f'Revisé la gráfica {r.code} y apruebo la versión 1.0.'
        reply, _ = self.say(text, 'approve', {'version':'1.0'})
        self.assertIn('¿Confirmas', reply)
        self.assertIsNone(r.versions.get().approved_at)
        self.say('confirmo', 'unknown')
        self.assertIsNotNone(r.versions.get().approved_at)

    def test_natural_approval_variants_use_focus_and_explicit_confirmation(self):
        for phrase in ('Revisé la gráfica y apruebo la 1.0', 'La v1.0 está aprobada', 'Aprobada la versión uno punto cero'):
            with self.subTest(phrase=phrase):
                r = self.review()
                Conversation.objects.update_or_create(participant=self.admin, defaults={'context':{'focus':r.pk},'pending':{}})
                reply, _ = self.say(phrase, 'approve')
                self.assertIn('¿Confirmas', reply)
                self.assertIsNone(r.versions.get().approved_at)
                self.say('confirmo', 'unknown')
                self.assertIsNotNone(r.versions.get().approved_at)

    def test_missing_version_keeps_intent_code_and_revision_across_messages(self):
        r = self.review()
        self.say(f'Aprueba {r.code}', 'approve')
        context = Conversation.objects.get(participant=self.admin).context['operation']
        self.assertEqual(context['action'], 'approve')
        self.assertEqual(context['request_id'], r.pk)
        reply, _ = self.say('1.0', 'query', {'version':'1.0'})
        self.assertIn('¿Confirmas', reply)
        self.assertIn(r.code, reply)
        self.assertNotIn(self.control.code, reply)
        self.assertIsNone(r.versions.get().approved_at)
        self.say('confirmo', 'unknown')
        self.assertIsNotNone(r.versions.get().approved_at)

    def test_version_answer_cannot_change_request_or_bypass_role(self):
        r = self.review()
        self.say(f'Aprueba {r.code}', 'approve')
        reply, _ = self.say(f'1.0 de {self.control.code}', 'query')
        self.assertIn('otra solicitud', reply)
        self.assertIsNone(r.versions.get().approved_at)
        reply, _ = self.say(f'Aprueba {r.code}', 'approve', actor=self.worker)
        self.assertIn('revisor', reply)
        self.assertFalse(Conversation.objects.get(participant=self.worker).context.get('operation'))

    def test_nonexistent_hallucinated_and_duplicate_approvals_rejected(self):
        r = self.review()
        self.say(f'Apruebo la versión 9.9 de {r.code}', 'approve', {'version':'9.9'})
        self.assertIsNone(r.versions.get().approved_at)
        reply, _ = self.say(f'Apruebo la versión 1.0 de {r.code}', 'approve', {'version':'2.0'})
        self.assertIn('no coincide', reply)
        self.say(f'Apruebo la versión 1.0 de {r.code}', 'approve')
        self.say('confirmo', 'unknown')
        before = r.versions.get().approved_at
        self.say(f'Apruebo la versión 1.0 de {r.code}', 'approve')
        self.say('confirmo', 'unknown')
        self.assertEqual(r.versions.get().approved_at, before)
        self.assertEqual(r.history.filter(operation='approve').count(), 1)

    def test_late_clarification_does_not_accept_changed_revision(self):
        r = self.review()
        self.say(f'Aprueba {r.code}', 'approve')
        self.action(self.coordinator, r, 'assign_review', {'assignee_id':self.reviewer.pk}, confirmed=True)
        reply, _ = self.say('1.0', 'query')
        self.assertIn('cambió', reply)
        self.assertIsNone(r.versions.get().approved_at)

    def test_telegram_clarification_still_requires_delivered_confirmation(self):
        r = self.review()
        Conversation.objects.create(participant=self.admin, module='content')
        text = f'Aprueba {r.code}'
        self.plan(self.admin, text, [{'action':'approve','evidence':text,'data':{}}], channel='telegram')
        _, msg = self.plan(self.admin, 'v1.0', channel='telegram')
        self.plan(self.admin, 'confirmo', channel='telegram')
        self.assertIsNone(r.versions.get().approved_at)

    def test_full_isolated_flow_stops_after_editing_and_matches_dashboard(self):
        r = self.request('Recorrido aislado', self.worker)
        self.action(self.worker, r, 'start')
        self.action(self.worker, r, 'submit', {'version':'1.0'})
        self.action(self.coordinator, r, 'assign_review', {'assignee_id':self.reviewer.pk}, confirmed=True)
        self.action(self.reviewer, r, 'approve', {'version':'1.0'}, confirmed=True)
        self.action(self.coordinator, r, 'assign_edit', {'assignee_id':self.editor.pk}, confirmed=True)
        self.action(self.editor, r, 'edit', {'version':'1.0'})
        r.refresh_from_db()
        self.assertEqual(r.state, 'editing')
        self.assertIsNone(r.versions.get().received_at)
        self.assertIsNone(r.versions.get().emitted_at)
        steps = progress_steps(r, work_info(r))
        self.assertEqual(steps[5]['status'], 'done')
        self.assertEqual(steps[6]['status'], 'current')
        self.assertEqual(steps[7]['status'], 'pending')
        self.client.force_login(self.coordinator.user)
        response = self.client.get(f'/contents/{r.pk}/data/')
        self.assertContains(response, 'Asignación de Edición')
        self.assertContains(response, 'Falta confirmar la recepción')
        self.assertContains(response, 'No confirmada')
        self.control.refresh_from_db()
        self.assertEqual(self.control.state, self.control_state)

    def test_negated_incorporation_never_advances(self):
        r = self.approved()
        self.say(f'No incorporé la versión 1.0 de {r.code} a la nota', 'receive', {'version':'1.0'})
        self.assertIsNone(r.versions.get().edited_at)
        self.assertIsNone(r.versions.get().received_at)

    def test_specific_old_approved_version_query_does_not_use_new_work_as_editing_status(self):
        r = self.approved()
        self.action(self.reviewer, r, 'corrections', {'version':'1.0','new_version':'1.1'}, confirmed=True)
        answer, _ = query(self.journalist, f'¿Qué falta para incorporar la versión 1.0 de {r.code} a Edición?')
        self.assertIn('Versión 1.0: aprobada', answer)
        self.assertIn('Edición todavía no tiene responsable', answer)
        self.assertNotIn('Hay correcciones en la versión 1.1', answer)

    def test_missing_editing_task_is_explained_as_technical_inconsistency(self):
        r = self.approved()
        r.tasks.filter(kind='editing').delete()  # Solo base temporal; simula inconsistencia.
        reply, _ = self.say(f'Ya incorporé la versión 1.0 de {r.code} a la nota', 'edit')
        self.assertIn('revisión técnica', reply)
        self.assertIsNone(r.versions.get().edited_at)

    def test_missing_version_clarification_cannot_be_confirmed_without_supplied_version(self):
        r = self.review()
        self.say(f'Aprueba {r.code}', 'approve')
        reply, _ = self.say('confirmo', 'unknown')
        self.assertIn('No hay una operación pendiente de confirmación', reply)
        self.assertIsNone(r.versions.get().approved_at)

    def test_clarification_cannot_cross_channels(self):
        r = self.review()
        Conversation.objects.create(participant=self.admin, module='content')
        text = f'Aprueba {r.code}'
        self.plan(self.admin, text, [{'action':'approve','evidence':text,'data':{}}], channel='telegram')
        reply, _ = self.say('1.0', 'query')
        self.assertIn('mismo canal', reply)
        self.assertIsNone(r.versions.get().approved_at)
