from django.test import TestCase
from django.core.exceptions import PermissionDenied, ValidationError
from .tests import Fixture
from .models import Conversation
from .resolution import resolve, ChooseTask
from .reporting import query, work_info
from .presentation import history_cards, progress_steps


class WorkflowPolishTests(Fixture, TestCase):
    def test_related_query_intents_are_answered_together(self):
        r = self.review()
        text = f'¿Quién revisa {r.code} y qué falta para aprobar la versión 1.0?'
        reply, _ = self.plan(self.coordinator, text, [
            {'action':'query', 'evidence':text, 'data':{}},
            {'action':'query', 'evidence':text, 'data':{}}])
        self.assertIn('Revisión: Sin asignar', reply)
        self.assertIn('asignar un revisor autorizado', reply)
        self.assertNotIn('una operación sensible', reply)

    def start_message(self, text, who=None):
        return self.plan(who or self.worker, text, [{'action':'start','evidence':text,'data':{}}])

    def test_no_eligible_start_never_reactivates_review_or_received(self):
        r = self.review()
        reply, _ = self.start_message('Ya voy a comenzar a trabajar')
        self.assertIn('no tienes tareas pendientes de comenzar', reply)
        self.assertEqual(r.tasks.get(kind='design').state, 'review')

    def test_unique_pending_task_resolved_without_code(self):
        r = self.request(worker=self.worker)
        self.start_message('Voy a comenzar con la gráfica')
        r.refresh_from_db()
        self.assertEqual(r.state, 'production')

    def test_multiple_candidates_clarification_survives_next_message(self):
        economy = self.request(worker=self.worker)
        sports = self.request('Deportes', self.worker)
        reply, _ = self.start_message('Voy a comenzar a trabajar')
        self.assertIn(economy.code, reply)
        self.assertIn(sports.code, reply)
        context = Conversation.objects.get(participant=self.worker).context
        self.assertEqual(context['choice']['action'], 'start')
        self.plan(self.worker, 'Con Economía')
        self.assertEqual(economy.tasks.get(kind='design').state, 'working')
        self.assertEqual(sports.tasks.get(kind='design').state, 'pending')

    def test_natural_reference_selects_only_named_task(self):
        economy = self.request(worker=self.worker)
        sports = self.request('Deportes', self.worker)
        self.start_message('Voy a comenzar la gráfica de Economía')
        self.assertEqual(economy.tasks.get().state, 'working')
        self.assertEqual(sports.tasks.get().state, 'pending')

    def test_other_worker_code_and_unknown_title_rejected(self):
        r = self.request('Política', self.other)
        with self.assertRaises(PermissionDenied): resolve(self.worker, r.code, 'start')
        self.request(worker=self.worker)
        with self.assertRaises(ValidationError): resolve(self.worker, 'Voy a comenzar Cultura', 'start')

    def test_explicit_review_task_cannot_restart(self):
        r = self.review()
        with self.assertRaises(ValidationError): resolve(self.worker, r.code, 'start')
        with self.assertRaises(ValidationError): self.action(self.worker, r, 'start')

    def test_query_distinguishes_no_assignments_from_other_stages(self):
        answer, _ = query(self.worker, '¿Tengo alguna tarea pendiente de comenzar?')
        self.assertIn('No tienes tareas asignadas', answer)
        r = self.review()
        answer, _ = query(self.worker, '¿Tengo alguna tarea pendiente de comenzar? Revisa mis asignaciones actuales')
        self.assertIn('No tienes tareas pendientes de comenzar', answer)
        self.assertIn(r.code, answer)
        self.assertIn('En revisión', answer)

    def test_queries_production_and_all_assignments_are_scoped(self):
        r = self.request(worker=self.worker)
        other = self.request('Política', self.other)
        self.action(self.worker, r, 'start')
        for text in ('¿Qué trabajos tengo asignados?', '¿Qué trabajos tengo en producción?', '¿Qué tengo que hacer ahora?'):
            answer, _ = query(self.worker, text)
            self.assertIn(r.code, answer)
            self.assertNotIn(other.code, answer)

    def test_reviewer_assignment_and_reassignment_never_change_designer(self):
        r = self.review()
        self.action(self.coordinator, r, 'assign_review', {'assignee_id':self.reviewer.pk}, confirmed=True)
        self.assertEqual(r.tasks.get(kind='review').assignee, self.reviewer)
        self.action(self.coordinator, r, 'assign_review', {'assignee_id':self.actors['Admin'].pk}, confirmed=True)
        self.assertEqual(r.tasks.get(kind='design').assignee, self.worker)
        self.assertEqual(r.tasks.get(kind='review').assignee, self.actors['Admin'])

    def test_reviewer_requires_role_confirmation_and_authority(self):
        r = self.review()
        with self.assertRaises(ValidationError): self.action(self.coordinator, r, 'assign_review', {'assignee_id':self.worker.pk}, confirmed=True)
        with self.assertRaises(ValidationError): self.action(self.coordinator, r, 'assign_review', {'assignee_id':self.reviewer.pk})
        with self.assertRaises(PermissionDenied): self.action(self.worker, r, 'assign_review', {'assignee_id':self.reviewer.pk}, confirmed=True)
        with self.assertRaises(PermissionDenied): self.action(self.worker, r, 'approve', {'version':'1.0'}, confirmed=True)

    def test_chat_distinguishes_reviewer_even_if_ai_says_assign(self):
        r = self.review()
        text = f'Asigna la revisión de {r.code} a Revisor'
        reply, _ = self.plan(self.coordinator, text, [{'action':'assign','evidence':text,'data':{}}])
        self.assertIn('revisión', reply)
        self.plan(self.coordinator, 'confirmo')
        self.assertEqual(r.tasks.get(kind='review').assignee, self.reviewer)
        self.assertEqual(r.tasks.get(kind='design').assignee, self.worker)

    def test_compound_review_query_answers_who_version_and_next_step(self):
        r = self.review()
        text = f'¿Quién tiene asignada la revisión de {r.code} y qué falta para que se apruebe la versión 1.0?'
        answer, _ = query(self.coordinator, text)
        self.assertIn('Sin asignar', answer)
        self.assertIn('asignar un revisor autorizado', answer)
        self.assertIn('1.0', answer)
        self.action(self.coordinator, r, 'assign_review', {'assignee_id':self.reviewer.pk}, confirmed=True)
        answer, _ = query(self.coordinator, text)
        self.assertIn('Revisor', answer)
        self.action(self.reviewer, r, 'approve', {'version':'1.0'}, confirmed=True)
        answer, _ = query(self.coordinator, text)
        self.assertIn('Versión 1.0: aprobada', answer)
        self.assertNotIn('Última aprobada: 1.0', answer)  # La consulta presenta una sola versión aprobada.
        self.assertNotIn('Falta la aprobación', answer)

    def test_approved_old_version_and_new_work_are_independent(self):
        r = self.review()
        self.action(self.reviewer, r, 'approve', {'version':'1.0'}, confirmed=True)
        self.action(self.reviewer, r, 'corrections', {'version':'1.0','new_version':'1.1'}, confirmed=True)
        self.action(self.editor, r, 'edit', {'version':'1.0'})
        self.action(self.switch, r, 'receive', {'version':'1.0'})
        r.refresh_from_db()
        self.assertEqual(r.state, 'production')
        info = work_info(r)
        self.assertEqual(info['current'].number, '1.1')
        self.assertEqual(info['received'].number, '1.0')
        steps = progress_steps(r, info)
        self.assertNotEqual(steps[6]['status'], 'done')
        self.assertIn('correcciones', info['next'])

    def test_history_is_translated_diff_and_keeps_original_json(self):
        r = self.request(worker=self.worker)
        event = r.history.get(operation='assign')
        original = event.after.copy()
        card = history_cards([event])[0]
        self.assertEqual(card['label'], 'Asignación de diseño')
        self.assertTrue(any(c['after'] == 'Daniela' for c in card['changes']))
        self.assertFalse(any(c['label'] == 'revision' for c in card['changes']))
        event.refresh_from_db()
        self.assertEqual(event.after, original)

    def test_progress_review_and_switch_do_not_imply_emission(self):
        r = self.review()
        steps = progress_steps(r, work_info(r))
        self.assertEqual(steps[2]['status'], 'done')
        self.assertEqual(steps[3]['status'], 'current')
        self.assertEqual(steps[4]['status'], 'pending')
        self.assertEqual(steps[7]['status'], 'pending')

    def test_assignment_queries_answer_person_for_specific_code(self):
        r = self.request(worker=self.worker)
        answer, _ = query(self.coordinator, f'¿Quién tiene asignada esta tarea {r.code}?')
        self.assertIn('Diseño: Daniela', answer)

    def test_stale_clarification_cannot_start_a_task_that_already_changed(self):
        r = self.request(worker=self.worker)
        self.request('Deportes', self.worker)
        self.start_message('Voy a comenzar a trabajar')
        self.action(self.worker, r, 'start')
        self.plan(self.worker, 'Con Economía')
        self.assertEqual(r.history.filter(operation='start').count(), 1)

    def test_greeting_preserves_draft_and_never_creates(self):
        from .models import ContentRequest
        Conversation.objects.create(participant=self.journalist, draft={'title':'Encuesta'})
        reply, _ = self.plan(self.journalist, 'Hola')
        self.assertIn('borrador se conserva', reply)
        self.assertFalse(ContentRequest.objects.exists())

    def test_dashboard_contains_theme_controls_operational_panels_and_real_journey(self):
        r = self.review()
        self.client.force_login(self.coordinator.user)
        response = self.client.get('/contents/')
        for label in ('Acciones pendientes', 'Alertas operativas', 'theme-toggle', 'secuoya-dashboard-theme'):
            self.assertContains(response, label)
        self.assertNotContains(response, 'Tu conversación reciente')
        self.assertNotContains(response, 'Tus notificaciones')
        response = self.client.get(f'/contents/{r.pk}/')
        self.assertContains(response, 'aria-current="step"')
        self.assertContains(response, 'Última aprobada')
        self.assertContains(response, 'Asignación de diseño')
        self.assertContains(response, 'Sin asignar')
        self.assertNotContains(response, "{'state':")
        self.assertNotContains(response, ' · assign')

    def test_reviewer_context_query_and_next_step_remain_private(self):
        r = self.review()
        self.client.force_login(self.other.user)
        self.assertEqual(self.client.get(f'/contents/{r.pk}/data/').status_code, 404)
        answer, _ = query(self.other, f'¿Quién revisa {r.code}?')
        self.assertNotIn(r.title, answer)

    def test_inactive_reviewer_cannot_be_assigned(self):
        r = self.review()
        self.reviewer.active = False
        self.reviewer.save()
        with self.assertRaises(ValidationError):
            self.action(self.coordinator, r, 'assign_review', {'assignee_id':self.reviewer.pk}, confirmed=True)
