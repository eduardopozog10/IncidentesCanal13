"""Segunda intervención: contratos locales. No certifican Gemini real."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
import json
from datetime import timedelta

from django.test import TestCase, SimpleTestCase, override_settings

from incidents.ai.schemas import IncidentExtractionResult
from incidents.ai.gemini_provider import GeminiAIProvider
from incidents.models import ChannelMessage, Incident, IncidentDraft
from incidents.messaging.telegram_provider import TelegramRejectedError
from incidents.services.incident_processor import IncidentProcessor
from incidents import test_conversation_real27 as prior
from incidents.test_natural_confirmation import DATA, INITIAL, ConfirmationFixture
from incidents.test_telegram_groups import GroupFixture
from incidents.test_user_management import SETTINGS


@override_settings(DEBUG=False, TELEGRAM_AUTO_REGISTER=False, PILOT_MESSAGE_RETRY_SECONDS=0, **SETTINGS)
class SecondConversationTests(ConfirmationFixture, TestCase):
    seed = prior.ConversationReal27Tests.seed

    def interpret(self, **kwargs):
        extra = {name: kwargs.pop(name) for name in list(kwargs) if name in
                 {'dialogue_action', 'action_evidence', 'requested_fields', 'help_field', 'rewrite_clauses'}}
        result = IncidentExtractionResult(**kwargs)
        for name, value in extra.items(): setattr(result, name, value)
        self.ai.extract_incident.return_value = result

    def test_informal_administrative_uncertainty_gets_orientation(self):
        draft = self.seed(area=None, equipment='Consola de audio')
        self.send('No cacho a qué área corresponde esto, si a producción o audio. ¿Me podis ayudar con eso?')
        draft.refresh_from_db()
        self.assertIsNone(draft.extracted_data['area'])
        self.assertIn('Audio', self.last_reply())
        self.assertIn('Producción', self.last_reply())
        self.assertNotIn('dato correcto y su nuevo valor', self.last_reply())
        self.assertFalse(Incident.objects.exists())

    def test_help_for_unknown_equipment_never_fills_it_from_ai(self):
        draft = self.seed(equipment=None)
        self.interpret(**DATA, dialogue_action='help', action_evidence='No cacho qué equipo es', help_field='equipment')
        self.send('No cacho qué equipo es, ¿cómo lo identifico sin inventarlo?')
        draft.refresh_from_db()
        self.assertIsNone(draft.extracted_data['equipment'])
        self.assertIn('identific', self.last_reply().lower())

    def test_actual_temporal_retraction_replaces_old_onset(self):
        draft = self.seed(description='La Cámara 3 presenta cortes. Inicio reportado: desde hace 40 minutos.')
        self.interpret(**draft.extracted_data, update_kind='correction', field_evidence={})
        self.send('Oye otra cosa, me dicen que la falla empezó hace como una hora en realidad, no hace 40 minutos. Cambia eso porfa. Mantén el síntoma y los demás datos.')
        draft.refresh_from_db()
        self.assertIn('una hora', draft.extracted_data['description'])
        self.assertNotIn('40 minutos', draft.extracted_data['description'])
        self.assertIn('cortes', draft.extracted_data['description'])
        self.assertIn('una hora', self.last_reply())

    def test_query_of_onset_does_not_correct_it(self):
        draft = self.seed(description='La cámara presenta cortes desde hace 40 minutos.')
        old = (deepcopy(draft.extracted_data), draft.updated_at, draft.last_question)
        self.send('Oye, dime nomás hace cuánto empezó la falla, sin cambiar nada')
        draft.refresh_from_db()
        self.assertEqual((draft.extracted_data, draft.updated_at, draft.last_question), old)
        self.assertIn('40 minutos', self.last_reply())
        self.assertNotIn('¿Confirmas', self.last_reply())

    def test_natural_cancel_uses_existing_service(self):
        draft = self.ready()
        with patch('incidents.services.incident_draft_service.IncidentDraftService.cancel_active_draft', wraps=self.handler.draft_service.cancel_active_draft) as cancel:
            self.send('Cancela el borrador actual de la Cámara 2 del Estudio A. No quiero registrar ninguna incidencia')
        cancel.assert_called_once()
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'cancelled')
        self.assertFalse(Incident.objects.exists())
        self.assertEqual(ChannelMessage.objects.filter(direction='inbound').last().draft_id, draft.pk)

    def test_cancel_does_not_delete_completed_incident(self):
        self.ready(); self.send('Sí')
        incident = Incident.objects.get()
        self.send('Descarta el reporte que estamos haciendo')
        self.assertTrue(Incident.objects.filter(pk=incident.pk).exists())
        self.assertEqual(IncidentDraft.objects.get().status, 'completed')

    def test_intermittent_symptom_is_not_lost_to_generic_description(self):
        draft = self.seed(failure_type='Sin imagen', description='La Cámara 3 está sin imagen.')
        text = 'La cámara 3 no está negra todo el rato, en verdad la imagen se va a negro por unos segundos y después vuelve'
        self.interpret(**{**draft.extracted_data, 'failure_type':None, 'description':text}, update_kind='correction', field_evidence={'description':text})
        self.send(text)
        draft.refresh_from_db()
        self.assertNotIn(draft.extracted_data['failure_type'], (None, 'Sin imagen'))
        self.assertIn('intermitente', draft.extracted_data['failure_type'].lower())
        self.assertNotIn('¿Qué problema presenta', self.last_reply())

    def monitor(self):
        return self.seed(equipment='Monitor de audio', asset_category='audio',
            failure_type='Zumbido', description='El monitor original presenta un zumbido desde hace 10 minutos. Tenemos un monitor de respaldo disponible, todavía no conectado. Sospechamos el cable, sin confirmar.',
            operational_impact='La prueba de sonido está interrumpida; el respaldo todavía no está conectado.')

    def test_backup_transition_applies_even_if_ai_repeats_old_values(self):
        draft = self.monitor()
        text = 'Al final conectamos el otro monitor y pudimos seguir con la prueba de sonido. El monitor original sigue fallando'
        self.interpret(**draft.extracted_data, update_kind='additional', field_evidence={'operational_impact':text})
        self.send(text)
        draft.refresh_from_db()
        self.assertIn('pudimos seguir', draft.extracted_data['operational_impact'])
        self.assertNotIn('todavía no está conectado', draft.extracted_data['operational_impact'])
        self.assertNotIn('Ese dato ya estaba', self.last_reply())
        self.assertFalse(Incident.objects.exists())

    def test_partial_recovery_keeps_fault_time_and_hypothesis(self):
        draft = self.monitor()
        text = 'Ya estamos usando el monitor alternativo, pero solo recuperamos parte de la operación. El original sigue con zumbido'
        self.interpret(**{**draft.extracted_data, 'description':'Usamos otro monitor.', 'operational_impact':'Operación parcialmente recuperada con respaldo'},
            update_kind='evolution', field_evidence={'description':text, 'operational_impact':text})
        self.send(text); draft.refresh_from_db()
        description = draft.extracted_data['description']
        for fact in ('10 minutos', 'zumbido', 'Sospechamos el cable'):
            self.assertIn(fact, description)
        self.assertNotIn('todavía no conectado', description)
        self.assertIn('parte', draft.extracted_data['operational_impact'])
        self.assertEqual(draft.extracted_data['failure_type'], 'Zumbido')

    def test_unrelated_correction_preserves_backup_information(self):
        draft = self.monitor()
        self.interpret(**{**draft.extracted_data, 'description':'El monitor produce un zumbido desde hace 10 minutos.'},
            update_kind='correction', field_evidence={'description':'El monitor produce un zumbido'})
        self.send('El monitor produce un zumbido, ordena ese síntoma sin borrar el respaldo')
        draft.refresh_from_db()
        self.assertIn('respaldo disponible', draft.extracted_data['description'])
        self.assertIn('Sospechamos', draft.extracted_data['description'])

    def test_informal_targeted_query_never_shows_full_form(self):
        draft = self.monitor()
        old = (deepcopy(draft.extracted_data), draft.last_question, draft.updated_at, draft.status)
        self.send('Ya pero no me tires todo el reporte po jajaja, dime nomás hace cuánto empezó la falla y si estamos usando el otro monitor o todavía no')
        draft.refresh_from_db()
        self.assertEqual((draft.extracted_data, draft.last_question, draft.updated_at, draft.status), old)
        self.assertIn('10 minutos', self.last_reply())
        self.assertIn('todavía no', self.last_reply())
        self.assertNotIn('¿Confirmas', self.last_reply())
        self.assertNotIn('• Área', self.last_reply())

    def test_semantic_query_dispatches_before_legacy_merge(self):
        draft = self.seed()
        old = (deepcopy(draft.extracted_data), draft.updated_at, draft.last_question)
        self.interpret(**{**DATA, 'equipment':'Inventado'}, dialogue_action='query', action_evidence='necesito recuperar el dato del recinto que anotamos', requested_fields=['location_context'])
        self.send('Buenas, necesito recuperar el dato del recinto que anotamos')
        self.ai.extract_incident.assert_called_once()
        draft.refresh_from_db()
        self.assertEqual((draft.extracted_data, draft.updated_at, draft.last_question), old)
        self.assertIn('Estudio B', self.last_reply())
        self.assertNotIn('¿Confirmas', self.last_reply())

    def test_explicit_rewrite_reorders_only_supported_clauses(self):
        draft = self.seed(description='tenemos otro monitor de respaldo disponible. La cámara presenta cortes. La cámara presenta cortes. Sospechamos el cable, sin confirmar.')
        clauses = ['La cámara presenta cortes.', 'Sospechamos el cable, sin confirmar.', 'tenemos otro monitor de respaldo disponible.']
        self.interpret(**draft.extracted_data, dialogue_action='rewrite', action_evidence='ordena la descripción y elimina repeticiones', rewrite_clauses=clauses)
        self.send('Está enredado: ordena la descripción y elimina repeticiones, conserva todos los hechos')
        draft.refresh_from_db()
        self.assertTrue(draft.extracted_data['description'].startswith('La cámara presenta cortes.'))
        self.assertEqual(draft.extracted_data['description'].count('La cámara presenta cortes'), 1)
        self.assertIn('Tenemos otro monitor de respaldo disponible.', draft.extracted_data['description'])
        self.assertIn('sin confirmar', draft.extracted_data['description'])

    def test_unsafe_rewrite_cannot_remove_backup_or_turn_hypothesis_into_fact(self):
        draft = self.monitor()
        old = deepcopy(draft.extracted_data)
        self.interpret(**{**draft.extracted_data, 'description':'El cable está roto.'}, dialogue_action='rewrite',
            action_evidence='reescribe la descripción', rewrite_clauses=['El cable está roto.'])
        self.send('Por favor, reescribe la descripción sin perder los hechos')
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data, old)
        self.assertNotIn('actualicé', self.last_reply())
        self.assertNotIn('El cable está roto', self.last_reply())

    def test_real_natural_confirmation_creates_once(self):
        draft = self.ready()
        text = 'Ya, ahora sí po jajaja. Confirmo el reporte de la Cámara 2 del Estudio A. Está todo bien, regístralo nomás'
        self.send(text)
        self.assertEqual(Incident.objects.count(), 1)
        self.send(text); self.assertEqual(Incident.objects.count(), 1)
        draft.refresh_from_db(); self.assertEqual(draft.status, 'completed')

    def test_confirmation_referencing_other_equipment_never_creates(self):
        self.ready(); self.send('Confirmo el reporte de la Cámara 99 del Estudio Z, regístralo')
        self.assertFalse(Incident.objects.exists())

    def test_provider_confirmation_alone_is_not_authorization(self):
        self.ready()
        text = 'Mi compañero dijo que sí'
        self.interpret(**DATA, dialogue_action='confirm', action_evidence=text)
        self.send(text); self.assertFalse(Incident.objects.exists())

    def test_permuted_equipment_and_location_ids_cannot_authorize_draft(self):
        draft = self.ready()
        self.send('Confirmo el reporte de la Cámara A del Estudio 2, regístralo')
        self.assertFalse(Incident.objects.exists())
        draft.refresh_from_db(); self.assertNotEqual(draft.status, 'completed')

    def test_quoted_affirmative_is_not_confirmation(self):
        self.ready()
        self.send("'Sí, confirmo'")
        self.assertFalse(Incident.objects.exists())

    def test_natural_confirm_cannot_authorize_incomplete_draft(self):
        draft = self.seed(area=None)
        self.send('Ya, regístralo nomás')
        self.assertFalse(Incident.objects.exists())
        draft.refresh_from_db(); self.assertEqual(draft.status, 'collecting')

    def test_natural_confirm_cannot_use_summary_of_previous_draft(self):
        old = self.ready()
        self.send('/cancelar')
        self.seed(equipment='Cámara 8')
        self.send('Confirmo el reporte de la Cámara 2 del Estudio A, regístralo')
        self.assertFalse(Incident.objects.exists())
        old.refresh_from_db(); self.assertEqual(old.status, 'cancelled')

    def test_failed_summary_still_blocks_natural_authorization(self):
        original = self.messaging.send_text.side_effect
        self.messaging.send_text.side_effect = TelegramRejectedError()
        self.send(INITIAL)
        self.messaging.send_text.side_effect = original
        self.send('Ya, regístralo')
        self.assertFalse(Incident.objects.exists())
        self.handler.recover_pending()
        self.assertFalse(Incident.objects.exists())
        self.send('Ya, regístralo')
        self.assertEqual(Incident.objects.count(), 1)

    def test_targeted_query_preserves_collecting_phase_and_last_question(self):
        draft = self.seed(area=None)
        original = (deepcopy(draft.extracted_data), draft.status, draft.last_question, draft.updated_at)
        self.send('¿En qué estudio está la cámara y qué prioridad anotamos?')
        draft.refresh_from_db()
        self.assertEqual((draft.extracted_data, draft.status, draft.last_question, draft.updated_at), original)
        self.assertIn('Estudio B', self.last_reply())
        self.assertNotIn('¿Confirmas', self.last_reply())

    def test_explicit_full_summary_still_returns_all_fields(self):
        self.seed()
        self.send('Muéstrame el resumen completo del borrador')
        for label in ('Área', 'Equipo', 'Síntoma', 'Prioridad'):
            self.assertIn(label, self.last_reply())
        self.assertFalse(Incident.objects.exists())

    def test_question_with_new_fact_updates_only_supported_field(self):
        draft = self.seed()
        self.interpret(**{**draft.extracted_data, 'priority':'critical'}, update_kind='correction', field_evidence={'priority':'Crítica'})
        self.send('Cambia la prioridad a Crítica. ¿Qué equipo teníamos anotado?')
        draft.refresh_from_db()
        self.assertEqual(draft.extracted_data['priority'], 'critical')
        self.assertEqual(draft.extracted_data['equipment'], 'Cámara 3')
        self.assertIn('Cámara 3', self.last_reply())

    def test_rewrite_omitting_a_unique_fact_leaves_draft_intact(self):
        draft = self.monitor()
        original = deepcopy(draft.extracted_data)
        self.interpret(**draft.extracted_data, dialogue_action='rewrite', action_evidence='ordena la descripción',
            rewrite_clauses=['El monitor original presenta un zumbido desde hace 10 minutos.'])
        self.send('Por favor ordena la descripción sin quitar nada')
        draft.refresh_from_db(); self.assertEqual(draft.extracted_data, original)
        self.assertNotIn('actualicé', self.last_reply())

    def test_rewrite_cannot_change_other_fields_even_if_ai_proposes_them(self):
        draft = self.monitor(); original = deepcopy(draft.extracted_data)
        clauses = original['description'].split('. ')
        self.interpret(**{**draft.extracted_data, 'equipment':'Inventado', 'priority':'critical'}, dialogue_action='rewrite',
            action_evidence='ordena la descripción', rewrite_clauses=list(reversed(clauses)))
        self.send('Por favor, ordena la descripción conservando todo')
        draft.refresh_from_db()
        for name in original:
            if name != 'description': self.assertEqual(draft.extracted_data[name], original[name])
        self.assertFalse(Incident.objects.exists())

    def test_consecutive_time_and_priority_changes_keep_other_facts(self):
        draft = self.monitor()
        for text, priority in [('La falla empezó hace como dos horas, no hace 10 minutos', 'high'),
                               ('Cambia la prioridad a Crítica', 'critical')]:
            draft.refresh_from_db()
            self.interpret(**{**draft.extracted_data, 'priority':priority}, update_kind='correction', field_evidence={'priority':'Crítica'} if priority == 'critical' else {})
            self.send(text)
        draft.refresh_from_db()
        for value in ('dos horas', 'respaldo disponible', 'Sospechamos el cable'):
            self.assertIn(value, draft.extracted_data['description'])
        self.assertNotIn('10 minutos', draft.extracted_data['description'])
        self.assertEqual(draft.extracted_data['priority'], 'critical')

    def test_conditional_backup_does_not_become_actual_usage(self):
        draft = self.monitor()
        text = 'Si conectamos el otro monitor podríamos seguir, todavía no lo hacemos'
        self.interpret(**{**draft.extracted_data, 'operational_impact':text}, update_kind='additional', field_evidence={'operational_impact':text})
        self.send(text); draft.refresh_from_db()
        self.assertNotIn('Ya estamos usando', draft.extracted_data['operational_impact'])
        self.assertIn('todavía no', draft.extracted_data['operational_impact'])
        self.assertFalse(Incident.objects.exists())

    def test_loss_of_used_backup_updates_current_impact_without_resolving_fault(self):
        draft = self.monitor()
        text = 'Perdimos el monitor de respaldo y ya no podemos continuar la prueba. El original sigue con zumbido'
        self.interpret(**{**draft.extracted_data, 'operational_impact':text}, update_kind='evolution', field_evidence={'operational_impact':text})
        self.send(text); draft.refresh_from_db()
        self.assertIn('ya no podemos continuar', draft.extracted_data['operational_impact'])
        self.assertIn('zumbido', draft.extracted_data['description'])
        self.assertEqual(draft.extracted_data['failure_type'], 'Zumbido')

    def test_explicit_original_repair_is_stored_as_evolution_without_auto_creation(self):
        draft = self.monitor()
        text = 'El monitor original fue reparado y ahora funciona sin zumbido. Reanudamos la prueba con ese equipo'
        self.interpret(**{**draft.extracted_data, 'description':text, 'operational_impact':'Prueba reanudada con el monitor original reparado'},
            update_kind='evolution', field_evidence={'description':text, 'operational_impact':'Reanudamos la prueba con ese equipo'})
        self.send(text); draft.refresh_from_db()
        self.assertIn('original fue reparado', draft.extracted_data['description'])
        self.assertIn('original reparado', draft.extracted_data['operational_impact'])
        self.assertFalse(Incident.objects.exists())

    def test_observability_contains_events_and_no_conversation_content(self):
        private = 'Dato privado QA-SECRET-987. ' + INITIAL
        with self.assertLogs('incidents', level='INFO') as events:
            self.send(private)
        output = '\n'.join(events.output)
        for event in ('message_persisted', 'processing_started', 'conversation.completed', 'reply_sent'):
            self.assertIn(event, output)
        self.assertNotIn('QA-SECRET-987', output)
        self.assertNotIn(INITIAL, output)

    def test_operational_update_keeps_fault_in_same_sentence_as_old_backup(self):
        draft = self.monitor()
        draft.extracted_data['description'] = 'El monitor original presenta un zumbido y tenemos otro monitor de respaldo disponible.'
        draft.save(update_fields=['extracted_data'])
        text = 'Conectamos el monitor de respaldo y retomamos la prueba de sonido'
        self.interpret(**draft.extracted_data, update_kind='additional', field_evidence={'operational_impact':text})
        self.send(text); draft.refresh_from_db()
        self.assertIn('zumbido', draft.extracted_data['description'])
        self.assertIn('retomamos', draft.extracted_data['description'])
        self.assertNotIn('respaldo disponible', draft.extracted_data['description'])

    def test_uninterpreted_time_change_is_not_reported_as_already_correct(self):
        draft = self.monitor()
        self.interpret(**draft.extracted_data, update_kind='correction', field_evidence={'description':'hace un rato'})
        self.send('Corrige el inicio de la falla, fue hace un rato')
        self.assertNotIn('ya estaba registrado correctamente', self.last_reply())
        self.assertNotIn('actualicé', self.last_reply())
        self.assertIn('¿', self.last_reply())

    def test_confirmed_priority_does_not_discard_diagnostic_hypothesis(self):
        draft = self.monitor()
        text = 'La prioridad está confirmada como crítica. La operación está detenida'
        self.interpret(**{**draft.extracted_data, 'priority':'critical', 'description':'El monitor presenta un zumbido.', 'operational_impact':'La operación está detenida'},
            update_kind='evolution', field_evidence={'priority':'crítica', 'description':'La operación está detenida', 'operational_impact':'La operación está detenida'})
        self.send(text); draft.refresh_from_db()
        self.assertIn('Sospechamos el cable', draft.extracted_data['description'])


@override_settings(TELEGRAM_GROUP_WINDOW_SECONDS=2.5, PILOT_MESSAGE_RETRY_SECONDS=0, DEBUG=False, TELEGRAM_AUTO_REGISTER=False, **SETTINGS)
class SecondGroupingTests(GroupFixture, TestCase):
    def test_two_messages_inside_exact_window(self):
        self.queue('Cámara 2 Estudio A', delta=0)
        self.queue('Producción. Sin imagen. Impide pruebas. Prioridad alta', delta=1.5)
        self.assertEqual(self.groups.ready_reporters(now=self.clock + timedelta(seconds=3.9)), [])
        self.flush(delta=4.0)
        self.assertEqual(self.ai.extract_incident.call_count, 1)
        self.assertEqual(self.transport.send_text.call_count, 1)
        self.assertEqual(ChannelMessage.objects.filter(direction='inbound', status='processed').count(), 2)

    def test_natural_cancel_in_group_wins_over_queued_confirmation(self):
        self.queue(INITIAL); self.flush()
        self.queue('Sí', delta=11)
        self.queue('Descarta este borrador, quiero empezar de nuevo', delta=11.5)
        self.flush(delta=15)
        self.assertFalse(Incident.objects.exists())
        self.assertEqual(IncidentDraft.objects.get().status, 'cancelled')

    def test_fact_with_question_is_not_routed_as_query_only_group(self):
        self.queue(INITIAL); self.flush()
        text = 'La prioridad es crítica. ¿Qué equipo anotaste?'
        self.ai.extract_incident.return_value = IncidentExtractionResult(**{**DATA, 'priority':'critical'}, update_kind='correction', field_evidence={'priority':'crítica'})
        self.queue(text, delta=11); self.queue('¿En qué estudio?', delta=11.5)
        self.flush(delta=15)
        self.assertEqual(IncidentDraft.objects.get().extracted_data['priority'], 'critical')

    def test_gap_above_window_creates_two_frozen_groups(self):
        self.queue(INITIAL, delta=0)
        self.queue('Agrego un antecedente sobre el mismo equipo', delta=2.6)
        self.flush(delta=6)
        first = ChannelMessage.objects.filter(direction='inbound').order_by('pk').first()
        self.assertEqual(first.processing_result['_group_member_ids'], [first.pk])
        self.assertEqual(ChannelMessage.objects.filter(direction='inbound', status='received').count(), 1)
        self.flush(delta=7)
        self.assertEqual(self.ai.extract_incident.call_count, 2)
        self.assertEqual(ChannelMessage.objects.filter(direction='inbound', status='processed').count(), 2)
        self.assertEqual(self.transport.send_text.call_count, 1, 'La respuesta superada debe omitirse')

    def test_group_logs_controlled_wait_and_preserves_private_originals(self):
        self.queue(INITIAL, delta=0)
        self.queue('No difundir contenido privado QA-ONLY-123', delta=1)
        with self.assertLogs('incidents', level='INFO') as events:
            self.flush(delta=3.5)
        output = '\n'.join(events.output)
        self.assertIn('count=2', output)
        self.assertIn('span=1.000s', output)
        self.assertIn('wait=2.500s', output)
        self.assertNotIn('QA-ONLY-123', output)


class SecondTextContractTests(SimpleTestCase):
    def test_time_handles_approximation_and_ignores_retracted_duration(self):
        from incidents.services.conversation_content import time_fact
        self.assertEqual(time_fact('Empezó hace como una hora, no hace 40 minutos'), 'hace como una hora')

    def test_confirmations_are_compositional_instead_of_whole_phrase_list(self):
        processor = IncidentProcessor(ai_provider=object())
        for text in ('Sí, confírmalo', 'Ya, regístralo', 'Dale, guárdalo nomás', 'Sí, crea la incidencia',
                     'Está todo correcto, puedes crearlo', 'Gracias. Autorizo registrar este reporte, está correcto'):
            with self.subTest(text=text): self.assertTrue(processor._is_confirmation(text))

    def test_hypothetical_quoted_deferred_or_mixed_confirmation_is_blocked(self):
        processor = IncidentProcessor(ai_provider=object())
        for text in ('Todavía no lo registres', 'Después lo confirmo', 'Cuando termine te aviso',
                     '¿Qué pasa si digo que sí?', 'Mi compañero dijo que sí', 'No sé si confirmarlo',
                     'Confirmo el reporte, pero cambia el equipo', 'Si todo está bien, regístralo',
                     'Confirmo que recibí el mensaje', 'La instrucción es «registra el reporte»'):
            with self.subTest(text=text): self.assertFalse(processor._is_confirmation(text))

    def test_provider_contract_retains_dialogue_action_even_for_conversation(self):
        provider = object.__new__(GeminiAIProvider); provider.model = 'qa'
        data = dict(intent='conversation', dialogue_action='query', action_evidence='dime el lugar', requested_fields=['location_context'])
        with patch.object(provider, '_create_interaction_with_retry', return_value=SimpleNamespace(output_text=json.dumps(data))):
            result = provider.extract_incident('dime el lugar', previous_data=DATA)
        self.assertEqual(getattr(result, 'dialogue_action', None), 'query')
        self.assertEqual(getattr(result, 'requested_fields', None), ['location_context'])

    def test_cancellation_grammar_separates_intent_from_queries_and_refusal(self):
        from incidents.services.conversation_content import natural_cancel
        for text in ('Cancela este reporte', 'Mejor no lo registremos', 'Olvida este incidente, quiero empezar de nuevo',
                     'Ya no quiero seguir con este borrador', 'Descarta el reporte que estamos haciendo'):
            with self.subTest(text=text): self.assertTrue(natural_cancel(text))
        for text in ('Todavía no lo registres', 'No canceles el borrador', '¿Puedes cancelar el reporte?',
                     'Quiero saber cómo cancelar este reporte', 'Si termino, cancela este borrador',
                     'Mi compañero dijo cancela el borrador', 'Ya no quiero seguir usando el otro monitor',
                     'No quiero cancelar el borrador', 'Descarta el reporte, pero cambia el equipo'):
            with self.subTest(text=text): self.assertFalse(natural_cancel(text))

    def test_rewrite_fidelity_rejects_altered_numbers_negation_and_missing_facts(self):
        from incidents.services.conversation_content import faithful_rewrite
        text = 'Falla hace 10 minutos. El respaldo no está conectado. Sospechamos del cable.'
        for clauses in (['Falla hace 20 minutos.', 'El respaldo no está conectado.', 'Sospechamos del cable.'],
                        ['Falla hace 10 minutos.', 'El respaldo está conectado.', 'Sospechamos del cable.'],
                        ['Falla hace 10 minutos.', 'El respaldo no está conectado.']):
            with self.subTest(clauses=clauses): self.assertIsNone(faithful_rewrite(text, clauses))

    def test_provider_rejects_malformed_dialogue_lists(self):
        provider = object.__new__(GeminiAIProvider); provider.model = 'qa'
        for field, value in [('requested_fields', 'equipment'), ('requested_fields', ['invented']),
                             ('rewrite_clauses', [42]), ('dialogue_action', 'delete')]:
            with self.subTest(field=field, value=value), patch.object(provider, '_create_interaction_with_retry',
                return_value=SimpleNamespace(output_text=json.dumps({'intent':'conversation', field:value}))):
                with self.assertRaises(ValueError): provider.extract_incident('texto', previous_data=DATA)

    def test_invented_action_evidence_cannot_activate_semantic_dispatch(self):
        provider = object.__new__(GeminiAIProvider); provider.model = 'qa'
        response = {'intent':'conversation', 'dialogue_action':'cancel', 'action_evidence':'Cancela este borrador'}
        with patch.object(provider, '_create_interaction_with_retry', return_value=SimpleNamespace(output_text=json.dumps(response))):
            result = provider.extract_incident('Hola', previous_data=DATA)
        self.assertIsNone(result.dialogue_action)

    def test_pilot_console_is_configured_without_enabling_http_debug(self):
        from django.conf import settings
        logger = settings.LOGGING['loggers']['incidents']
        self.assertIn('pilot_console', logger['handlers'])
        self.assertFalse(logger['propagate'])
        self.assertNotIn('httpx', settings.LOGGING['loggers'])

    def test_confirming_priority_does_not_resolve_a_hypothesis(self):
        from incidents.services.conversation_content import preserve_description_context
        result = preserve_description_context('El monitor falla. Sospechamos del cable, sin confirmar.',
            'El monitor está sin sonido.', 'La prioridad está confirmada como crítica')
        self.assertIn('Sospechamos del cable', result)

    def test_normalization_does_not_remove_a_fact_inside_a_different_clause(self):
        from incidents.services.conversation_content import join_description
        result = join_description('No sabemos si el cable funciona.', 'El cable funciona.')
        self.assertIn('No sabemos si el cable funciona.', result)
        self.assertIn('El cable funciona.', result)
