from unittest.mock import Mock
from django.test import TestCase
from django.utils import timezone
from incidents.ai.base import AITemporaryError
from .tests import Fixture, DATA
from .models import Conversation, ContentRequest, Outbox
from .processor import Processor, enqueue
from .ai import validate_plan

INITIAL = 'Necesito una gráfica con los resultados de una encuesta para el noticiario de las 21:00. La solicita el área de Prensa y corresponde al equipo de Gráfica. No tengo un plazo de entrega definido todavía.'
CORRECTION = 'Gráfica es el área encargada, pero todavía no hay una persona asignada. Tampoco he definido la prioridad.'
HOLD = 'No la ingreses todavía. Quiero que quede pendiente de asignación, sin una persona responsable definida. El área encargada es Gráfica.'
REAL_DATA = {**DATA, 'title': 'Gráfica resultados de encuesta noticiario 21:00',
             'description': 'Resultados de una encuesta para el noticiario de las 21:00'}


class PendingDraftTests(Fixture, TestCase):
    def send_draft(self, text, data=None, **extra):
        return self.plan(self.journalist, text, [{'action': 'create', 'evidence': text,
                                                 'data': REAL_DATA if data is None else data, **extra}], channel='telegram')

    def setUp(self):
        super().setUp()
        Conversation.objects.create(participant=self.journalist, module='content')

    def test_exact_real_conversation_persists_corrections_and_holds_without_creation(self):
        initial, _ = self.send_draft(INITIAL)
        self.assertIn('Área encargada: Gráfica', initial)
        self.assertIn('Diseñador: sin asignar', initial)
        self.assertIn('valor predeterminado', initial)
        conv = Conversation.objects.get(participant=self.journalist)
        original = conv.draft.copy()
        correction, _ = self.send_draft(CORRECTION)  # Mismo contrato completo que devolvió Gemini en el caso real.
        conv.refresh_from_db()
        self.assertNotEqual(conv.draft, original)
        self.assertNotIn('priority', conv.draft)
        self.assertTrue(conv.draft['_priority_unspecified'])
        self.assertTrue(conv.draft['_assignment_confirmed_unassigned'])
        self.assertNotEqual(correction, initial)
        self.assertIn('No indicada por ti', correction)
        held, _ = self.send_draft(HOLD)
        conv.refresh_from_db()
        self.assertTrue(conv.draft['_on_hold'])
        self.assertEqual(conv.pending, {})
        self.assertEqual(conv.draft['responsible_area'], 'Gráfica')
        self.assertEqual(conv.draft['description'], REAL_DATA['description'])
        self.assertIn('No la ingresé', held)
        self.assertNotIn('¿La ingreso así?', held)
        self.assertFalse(ContentRequest.objects.exists())
        reply, _ = self.plan(self.journalist, 'confirmo', channel='telegram')
        self.assertIn('sigue en pausa', reply)
        self.assertFalse(ContentRequest.objects.exists())

    def test_previous_priority_is_removed_even_if_ai_returns_stale_value(self):
        self.send_draft(INITIAL + ' Prioridad alta.', {**REAL_DATA, 'priority': 'high'})
        reply, _ = self.send_draft(CORRECTION, {**REAL_DATA, 'priority': 'high'})
        conv = Conversation.objects.get(participant=self.journalist)
        self.assertNotIn('priority', conv.draft)
        self.assertNotIn('Alta', reply)

    def test_hold_survives_ai_failure_and_invalidates_old_confirmation(self):
        self.send_draft(INITIAL)
        msg = enqueue(self.journalist, 'hold-error', HOLD)
        ai = Mock(); ai.interpret.side_effect = AITemporaryError('Unavailable')
        Processor(ai).process(msg)
        conv = Conversation.objects.get(participant=self.journalist)
        self.assertTrue(conv.draft['_on_hold'])
        self.assertEqual(conv.pending, {})
        self.assertFalse(ContentRequest.objects.exists())

    def test_resume_requires_new_delivered_summary_then_creates_unassigned_with_default(self):
        self.send_draft(INITIAL)
        self.send_draft(HOLD)
        reply, msg = self.plan(self.journalist, 'revisar borrador', channel='telegram')
        self.assertIn('¿La ingreso así?', reply)
        Outbox.objects.filter(key=f'reply:{msg.pk}').update(status='sent', sent_at=timezone.now())
        self.plan(self.journalist, 'confirmo', channel='telegram')
        r = ContentRequest.objects.get()
        self.assertEqual(r.responsible_area, 'Gráfica')
        self.assertEqual(r.state, 'pending')
        self.assertIsNone(r.tasks.get().assignee)
        self.assertEqual(r.priority, 'normal')

    def test_unchanged_draft_does_not_repeat_summary(self):
        self.send_draft(INITIAL)
        reply, _ = self.send_draft('El área es Gráfica', {'responsible_area': 'Gráfica'})
        self.assertNotIn(REAL_DATA['description'], reply)
        self.assertEqual(Conversation.objects.get(participant=self.journalist).pending, {})
        self.assertFalse(ContentRequest.objects.exists())

    def test_clear_fields_removes_value_instead_of_treating_null_as_deletion(self):
        self.send_draft(INITIAL + ' Prioridad alta.', {**REAL_DATA, 'priority': 'high'})
        self.send_draft('Retira esa prioridad', {}, clear_fields=['priority'])
        self.assertNotIn('priority', Conversation.objects.get(participant=self.journalist).draft)
        with self.assertRaises(ValueError):
            validate_plan({'updates': [{'action': 'assign', 'evidence': 'Asigna', 'data': {},
                                        'clear_fields': ['priority']}]}, 'Asigna')
