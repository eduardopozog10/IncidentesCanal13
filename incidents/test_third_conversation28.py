"""Contratos de la tercera intervención; las pruebas reales se archivan aparte."""
from copy import deepcopy
from unittest.mock import patch
from django.test import TestCase, SimpleTestCase, override_settings
from incidents.ai.schemas import IncidentExtractionResult
from incidents.models import Incident, IncidentDraft
from incidents.messaging.telegram_provider import TelegramRejectedError
from incidents.test_natural_confirmation import ConfirmationFixture, DATA
from incidents.test_user_management import SETTINGS

MIXED = ('Oye, agrega que tenemos otra cámara de respaldo disponible en el Estudio C, pero todavía no la hemos conectado ni ocupado. '
         'Las pruebas de grabación siguen completamente detenidas por la falla de la cámara 2. Mantén los otros datos. '
         'Y dime cortito cómo quedó anotado lo del respaldo, sin mostrar todo el reporte.')
AUTH = 'Ya revisé todo y está correcto. Dale, ingresa el reporte de la cámara 2 del estudio C. Te autorizo a registrarlo ahora. Con eso quedamos listos.'


@override_settings(DEBUG=False, TELEGRAM_AUTO_REGISTER=False, PILOT_MESSAGE_RETRY_SECONDS=0, **SETTINGS)
class ThirdConversationTests(ConfirmationFixture, TestCase):
    def extraction(self, data=None, **metadata):
        extra = {name: metadata.pop(name) for name in list(metadata) if name in {'dialogue_actions','authorization','operational_updates'}}
        result = IncidentExtractionResult(**{key:value for key,value in (data or deepcopy(DATA)).items() if not key.startswith('_')}, **metadata)
        for key,value in extra.items(): setattr(result,key,value)
        self.ai.extract_incident.return_value=result

    def ready_c(self):
        self.extraction({**DATA,'location_context':'Estudio C','unit':'Estudio C','operational_impact':'Las pruebas de grabación están completamente detenidas.'})
        return self.ready()

    def test_real_mixed_update_is_not_swallowed_by_local_query(self):
        draft=self.ready_c()
        text='Tenemos otra cámara de respaldo disponible, todavía sin conectar'
        self.extraction({**draft.extracted_data,'operational_impact':text}, update_kind='additional', dialogue_action='query',
            action_evidence='dime cortito cómo quedó anotado lo del respaldo', requested_fields=['backup'],
            field_evidence={'operational_impact':text}, dialogue_actions=['update','query'],
            operational_updates=[{'facet':'backup','text':text,'evidence':'tenemos otra cámara de respaldo disponible en el Estudio C, pero todavía no la hemos conectado ni ocupado'}])
        self.send(MIXED); draft.refresh_from_db()
        self.assertIn('detenidas',draft.extracted_data['operational_impact'])
        self.assertIn('respaldo',draft.extracted_data['operational_impact'])
        self.assertNotIn('¿Confirmas',self.last_reply())
        self.assertNotIn('No se ha informado',self.last_reply())

    def test_impact_correction_is_not_blocked_by_misclassified_rewrite(self):
        draft=self.ready_c()
        text='Corrige el impacto operacional: las pruebas siguen detenidas y el respaldo sigue sin conectar. No cambies la descripción. ¿Me confirmas que quedó actualizado?'
        self.extraction({**draft.extracted_data,'operational_impact':'Pruebas detenidas con respaldo sin conectar'}, update_kind='correction',
            dialogue_action='rewrite', action_evidence='No cambies la descripción', field_evidence={'operational_impact':'las pruebas siguen detenidas y el respaldo sigue sin conectar'})
        self.send(text); draft.refresh_from_db()
        self.assertIn('respaldo',draft.extracted_data['operational_impact'])
        self.assertNotIn('no pude verificar una reescritura',self.last_reply())
        self.assertNotIn('qué dato quieres corregir',self.last_reply())

    def test_semantic_authorization_uses_existing_summary_and_creation_guards(self):
        draft=self.ready_c()
        self.extraction(draft.extracted_data, intent='incident_followup', dialogue_action='confirm', action_evidence='Te autorizo a registrarlo ahora',
            dialogue_actions=['confirm'], authorization={'kind':'confirm','evidence':'Te autorizo a registrarlo ahora','direct':True,
            'conditional':False,'delayed':False,'third_party':False,'requires_changes':False,'equipment':'cámara 2','location_context':'estudio C'})
        self.send(AUTH)
        self.assertEqual(Incident.objects.count(),1)
        self.assertIn(Incident.objects.get().code,self.last_reply())

    def test_help_and_data_do_not_discard_the_report(self):
        self.extraction({**DATA,'area':None}, dialogue_action='help', action_evidence='No cacho el área', help_field='area',
            dialogue_actions=['update','help'], field_evidence={'equipment':'Cámara 2','failure_type':'sin imagen'})
        self.send('Cámara 2 del Estudio A sin imagen, interrumpe las pruebas, prioridad alta. No cacho el área')
        draft=IncidentDraft.objects.get()
        self.assertEqual(draft.extracted_data['equipment'],'Cámara 2')
        self.assertIsNone(draft.extracted_data['area'])
        self.assertIn('Audio',self.last_reply())

    def test_temporal_update_does_not_announce_changes_then_request_unspecified_field(self):
        draft=self.ready_c()
        draft.extracted_data['description']='La Cámara 2 presenta cortes desde hace 40 minutos.'
        draft.save(update_fields=['extracted_data'])
        self.extraction(draft.extracted_data, update_kind='correction', field_evidence={})
        self.send('Actualiza el inicio: lleva cerca de una hora, no hace 40 minutos. Mantén el resto')
        draft.refresh_from_db()
        self.assertIn('una hora',draft.extracted_data['description'])
        self.assertNotIn('qué dato quieres corregir',self.last_reply())

    def authorize(self, draft, text, **changes):
        authorization={'kind':'confirm','evidence':text,'direct':True,'conditional':False,'delayed':False,
            'third_party':False,'requires_changes':False,'equipment':None,'location_context':None,**changes}
        self.extraction(draft.extracted_data, dialogue_action='confirm', action_evidence=text,
            dialogue_actions=['confirm'], authorization=authorization)

    def test_semantic_authorization_after_failed_delivery_cannot_create(self):
        original=self.messaging.send_text.side_effect
        self.messaging.send_text.side_effect=TelegramRejectedError()
        self.send('En Producción: Cámara 2 Estudio A sin imagen, impide las pruebas. Alta')
        self.messaging.send_text.side_effect=original
        draft=IncidentDraft.objects.get()
        text='Te autorizo a ingresar el reporte ahora'
        self.authorize(draft,text)
        self.send(text)
        self.assertFalse(Incident.objects.exists())

    def test_semantic_authorization_requires_complete_data(self):
        self.extraction({**DATA,'area':None})
        self.send('Cámara 2 en Estudio A sin imagen. Interrumpe las pruebas. Alta')
        draft=IncidentDraft.objects.get(); text='Te autorizo a ingresar el reporte ahora'
        self.authorize(draft,text); self.send(text)
        self.assertFalse(Incident.objects.exists())

    def test_semantic_cancel_reuses_draft_service_without_required_fields(self):
        self.extraction({**DATA,'area':None}); self.send('Cámara 2, sin imagen. No puedo probar. Alta')
        draft=IncidentDraft.objects.get(); text='Abandona este borrador, no quiero continuar llenándolo'
        self.extraction(draft.extracted_data,dialogue_action='cancel',dialogue_actions=['cancel'],action_evidence=text,
            authorization={'kind':'cancel','evidence':text,'direct':True,'conditional':False,'delayed':False,
                'third_party':False,'requires_changes':False,'equipment':None,'location_context':None})
        with patch('incidents.services.incident_draft_service.IncidentDraftService.cancel_active_draft',wraps=self.handler.draft_service.cancel_active_draft) as cancel:
            self.send(text)
        cancel.assert_called_once(); draft.refresh_from_db(); self.assertEqual(draft.status,'cancelled')
        self.assertFalse(Incident.objects.exists())

    def test_operational_facets_survive_unrelated_priority_correction(self):
        draft=self.ready_c()
        draft.extracted_data['_operational_facts']={'consequence':'Pruebas detenidas','backup':'Otra cámara disponible sin conectar'}
        draft.extracted_data['operational_impact']='Pruebas detenidas. Otra cámara disponible sin conectar.'
        draft.save(update_fields=['extracted_data'])
        self.extraction({**draft.extracted_data,'priority':'critical'},update_kind='correction',field_evidence={'priority':'Crítica'},dialogue_actions=['update'])
        self.send('Cambia la prioridad a Crítica'); draft.refresh_from_db()
        self.assertEqual(draft.extracted_data['priority'],'critical')
        self.assertIn('sin conectar',draft.extracted_data['operational_impact'])
        self.assertEqual(len(draft.extracted_data['_operational_facts']),2)

    def test_brief_update_does_not_authorize_an_undelivered_summary(self):
        draft=self.ready_c(); text='Cambia la prioridad a Crítica. ¿Qué prioridad quedó?'
        self.extraction({**draft.extracted_data,'priority':'critical'},update_kind='correction',dialogue_actions=['update','query'],
            field_evidence={'priority':'Crítica'},requested_fields=['priority'])
        self.send(text); self.assertNotIn('¿Confirmas',self.last_reply())
        self.send('Sí'); self.assertFalse(Incident.objects.exists())
        self.assertIn('¿Confirmas',self.last_reply())
        self.send('Sí'); self.assertEqual(Incident.objects.get().priority,'critical')

    def test_query_about_completed_correction_without_fields_is_brief(self):
        draft=self.ready_c()
        text='Corrige el impacto: seguimos detenidos y hay respaldo sin conectar. ¿Me confirmas que quedó actualizado? Todavía no lo registres.'
        self.extraction({**draft.extracted_data,'operational_impact':'Pruebas detenidas. Respaldo sin conectar.'},update_kind='correction',
            field_evidence={'operational_impact':'seguimos detenidos y hay respaldo sin conectar'},dialogue_actions=['update','query'],
            authorization={'kind':'none','delayed':True})
        self.send(text)
        self.assertIn('sin conectar',self.last_reply())
        self.assertNotIn('¿Confirmas',self.last_reply())
        self.assertNotIn('• Área',self.last_reply())
        self.assertFalse(Incident.objects.exists())

    def test_repeated_semantic_confirmation_returns_existing_incident(self):
        draft=self.ready_c(); text='Te autorizo a ingresar el reporte ahora'
        self.authorize(draft,text); self.send(text)
        self.assertEqual(Incident.objects.count(),1)
        self.send(text)
        self.assertEqual(Incident.objects.count(),1)
        self.assertEqual(IncidentDraft.objects.count(),1)
        self.assertIn(Incident.objects.get().code,self.last_reply())

    def test_query_retains_explicit_time_even_when_provider_omits_requested_field(self):
        draft=self.ready_c(); draft.extracted_data['description']='La cámara falla desde hace una hora.'; draft.save(update_fields=['extracted_data'])
        self.extraction({**draft.extracted_data,'priority':'critical'},dialogue_actions=['update','query'],requested_fields=['priority'],
            update_kind='correction',field_evidence={'priority':'Crítica'})
        self.send('Cambia la prioridad a Crítica. ¿Qué prioridad y antigüedad quedaron?')
        self.assertIn('una hora',self.last_reply()); self.assertIn('Crítica',self.last_reply())
        self.assertNotIn('¿Confirmas',self.last_reply())

    def test_query_about_impact_reports_its_value_even_if_provider_requests_missing(self):
        draft=self.ready_c()
        self.extraction(draft.extracted_data,dialogue_actions=['update','query'],requested_fields=['missing'],
            update_kind='correction',field_evidence={'operational_impact':'Las pruebas de grabación están completamente detenidas.'})
        self.send('Las pruebas de grabación están completamente detenidas. ¿Me confirmas que el impacto quedó así?')
        self.assertIn('detenidas',self.last_reply()); self.assertNotIn('¿Confirmas',self.last_reply())

    def test_rewrite_and_query_are_executed_together_without_full_form(self):
        draft=self.ready_c(); draft.extracted_data['description']='La cámara muestra rayas. Se sospecha del cable, sin confirmar'
        draft.save(update_fields=['extracted_data'])
        text='Ordena la descripción, conserva los hechos. No cambies el impacto ni la prioridad. Dime solo la descripción final.'
        self.extraction(draft.extracted_data,dialogue_action='rewrite',action_evidence='Ordena la descripción',
            dialogue_actions=['rewrite','query'],requested_fields=['description'],rewrite_clauses=['La cámara muestra rayas.','Se sospecha del cable, sin confirmar'])
        self.send(text); draft.refresh_from_db()
        self.assertTrue(draft.extracted_data['description'].endswith('sin confirmar.'))
        self.assertIn('sin confirmar.',self.last_reply()); self.assertNotIn('¿Confirmas',self.last_reply())
        self.assertNotIn('• Impacto',self.last_reply())

    def test_rewrite_respects_explicit_output_when_provider_omits_query(self):
        draft=self.ready_c(); draft.extracted_data['description']='La cámara muestra rayas. Se sospecha del cable, sin confirmar'
        draft.save(update_fields=['extracted_data'])
        text='Ordena la descripción. No cambies el impacto ni la prioridad. Dime solo la descripción final.'
        self.extraction(draft.extracted_data,dialogue_action='rewrite',action_evidence='Ordena la descripción',
            dialogue_actions=['rewrite'],requested_fields=[],rewrite_clauses=['La cámara muestra rayas.','Se sospecha del cable, sin confirmar'])
        self.send(text)
        self.assertIn('sin confirmar.',self.last_reply())
        self.assertNotIn('¿Confirmas',self.last_reply()); self.assertNotIn('Impacto operacional:',self.last_reply())
        self.assertNotIn('Prioridad:',self.last_reply())


class ThirdTextTests(SimpleTestCase):
    def test_mixed_real_message_is_not_a_pure_query(self):
        from incidents.services.conversation_content import is_query_only
        self.assertFalse(is_query_only(MIXED))

    def test_rewrite_request_followed_by_query_is_not_read_only(self):
        from incidents.services.conversation_content import is_query_only
        self.assertFalse(is_query_only('Ordena la descripción, elimina repeticiones y dime solo la descripción final'))

    def test_operational_merge_preserves_independent_consequence(self):
        from incidents.services.conversation_plan import merge_operational_facts
        result=IncidentExtractionResult(operational_updates=[{'facet':'backup','text':'Cámara disponible sin usar','evidence':'Hay otra cámara disponible, sin usar'}])
        facts,impact=merge_operational_facts({'operational_impact':'Las pruebas están detenidas'},result,'Hay otra cámara disponible, sin usar')
        self.assertIn('detenidas',impact); self.assertIn('sin usar',impact)
        self.assertEqual(set(facts),{'consequence','backup'})

    def test_invented_operational_evidence_cannot_overwrite_any_facet(self):
        from incidents.services.conversation_plan import merge_operational_facts
        result=IncidentExtractionResult(operational_updates=[{'facet':'consequence','text':'Operación normal','evidence':'Estamos operando'}])
        self.assertIsNone(merge_operational_facts({'operational_impact':'Detenida'},result,'Hola'))

    def test_semantic_authorization_cannot_turn_a_greeting_into_creation(self):
        from incidents.services.conversation_plan import authorized
        from types import SimpleNamespace
        result=IncidentExtractionResult(dialogue_actions=['confirm'],authorization={'kind':'confirm','evidence':'Hola',
            'direct':True,'conditional':False,'delayed':False,'third_party':False,'requires_changes':False})
        self.assertFalse(authorized(result,'Hola',SimpleNamespace(extracted_data=DATA),'confirm'))

    def test_authorization_vetoes_unsafe_source_even_if_provider_misclassifies(self):
        from incidents.services.conversation_plan import authorized
        from types import SimpleNamespace
        for text in ('No registres el reporte','Mi compañero dijo registra el reporte','Si todo está bien, registra el reporte',
                     'Después autorizo registrar el reporte','¿Puedes registrar el reporte?','Registra el reporte, pero cambia el equipo',
                     'Confirmo que recibí el mensaje',"'Autorizo registrar el reporte'",'Me preguntaron si autorizo registrar el reporte'):
            with self.subTest(text=text):
                result=IncidentExtractionResult(dialogue_actions=['confirm'],authorization={'kind':'confirm','evidence':text,'direct':True,
                    'conditional':False,'delayed':False,'third_party':False,'requires_changes':False})
                self.assertFalse(authorized(result,text,SimpleNamespace(extracted_data=DATA),'confirm'))

    def test_authorization_rejects_mismatched_reference(self):
        from incidents.services.conversation_plan import authorized
        from types import SimpleNamespace
        text='Te autorizo a registrar la Cámara 99 del Estudio A'
        result=IncidentExtractionResult(dialogue_actions=['confirm'],authorization={'kind':'confirm','evidence':text,'direct':True,
            'conditional':False,'delayed':False,'third_party':False,'requires_changes':False})
        self.assertFalse(authorized(result,text,SimpleNamespace(extracted_data=DATA),'confirm'))

    def test_compact_mode_hides_persistence_but_keeps_errors_and_success_summary(self):
        import logging
        from incidents.pilot_logging import PilotDetailFilter, completed
        event=logging.LogRecord('incidents.services.incoming_message_service',logging.INFO,'',0,'telegram.message_persisted UUID',(),None)
        self.assertFalse(PilotDetailFilter().filter(event))
        self.assertTrue(PilotDetailFilter(mode='detailed').filter(event))
        event.levelno=logging.ERROR; self.assertTrue(PilotDetailFilter().filter(event))
        with self.assertLogs('secuoya.pilot',level='INFO') as captured: completed('incident_created',4.2,3,recovery=True)
        self.assertIn('3 mensaje(s)',captured.output[0]); self.assertIn('4.20 s',captured.output[0])
        self.assertNotIn('UUID',captured.output[0])

    def test_compact_console_does_not_announce_failed_processing_as_success(self):
        from incidents.pilot_logging import completed
        with self.assertLogs('secuoya.pilot',level='INFO') as captured:
            completed('processing_failed',2,1,recovery=True)
        self.assertIn('sin completar',captured.output[0])
        self.assertNotIn('Procesamiento completado',captured.output[0])

    def test_provider_parses_multiple_actions_and_filters_unfounded_facets(self):
        import json
        from types import SimpleNamespace
        from incidents.ai.gemini_provider import GeminiAIProvider
        provider=object.__new__(GeminiAIProvider); provider.model='qa'
        response={'intent':'incident_followup','dialogue_action':'query','action_evidence':'dime cómo quedó',
            'dialogue_actions':['update','query'],'requested_fields':['backup'],
            'operational_updates':[{'facet':'backup','text':'Respaldo disponible','evidence':'hay respaldo'},
                {'facet':'consequence','text':'Operación normal','evidence':'Estamos operando'}]}
        with patch.object(provider,'_create_interaction_with_retry',return_value=SimpleNamespace(output_text=json.dumps(response))):
            result=provider.extract_incident('hay respaldo; dime cómo quedó',previous_data=DATA)
        self.assertEqual(result.dialogue_actions,['update','query'])
        self.assertEqual(len(result.operational_updates),1)
        self.assertEqual(result.operational_updates[0]['facet'],'backup')

    def test_provider_rejects_untyped_authorization_and_unknown_facet(self):
        import json
        from types import SimpleNamespace
        from incidents.ai.gemini_provider import GeminiAIProvider
        provider=object.__new__(GeminiAIProvider); provider.model='qa'
        for extra in ({'authorization':{'kind':'confirm','direct':'true'}},
                      {'operational_updates':[{'facet':'priority','text':'alta','evidence':'alta'}]},
                      {'dialogue_actions':'confirm'}):
            with self.subTest(extra=extra), patch.object(provider,'_create_interaction_with_retry',return_value=SimpleNamespace(output_text=json.dumps({'intent':'conversation',**extra}))):
                with self.assertRaises(ValueError): provider.extract_incident('alta',previous_data=DATA)

    def test_onset_correction_does_not_replace_original_fault_state(self):
        from incidents.services.conversation_plan import merge_operational_facts
        text='la falla empezó hace cerca de una hora'
        result=IncidentExtractionResult(operational_updates=[{'facet':'original_fault','text':'La falla empezó hace cerca de una hora.','evidence':text}])
        self.assertIsNone(merge_operational_facts({'operational_impact':'Pruebas detenidas'},result,text))

    def test_resumption_with_backup_supersedes_both_stale_current_states(self):
        from incidents.services.conversation_plan import merge_operational_facts
        text='Ya conectamos la otra cámara y retomamos las pruebas con el respaldo'
        previous={'_operational_facts':{'consequence':'Pruebas completamente detenidas','backup':'Respaldo disponible sin conectar','original_fault':'La cámara original sigue fallando'}}
        result=IncidentExtractionResult(operational_updates=[{'facet':'continuity','text':'Pruebas reanudadas con la cámara de respaldo.',
            'evidence':text,'state':'resumed','backup_state':'in_use'}])
        facts,impact=merge_operational_facts(previous,result,text)
        self.assertNotIn('detenidas',impact); self.assertNotIn('sin conectar',impact)
        self.assertIn('original sigue fallando',impact)
        self.assertEqual(facts['_states']['backup'],'in_use')
        self.assertEqual(len(facts['_history']),2)
        self.assertEqual(previous['_operational_facts']['backup'],'Respaldo disponible sin conectar')

    def test_possible_resumption_does_not_clear_current_interruption(self):
        from incidents.services.conversation_plan import merge_operational_facts
        text='Podríamos retomar las pruebas si conectamos el respaldo'
        result=IncidentExtractionResult(operational_updates=[{'facet':'continuity','text':text,'evidence':text,'state':'resumed','backup_state':'in_use'}])
        facts,impact=merge_operational_facts({'operational_impact':'Pruebas detenidas'},result,text)
        self.assertIn('detenidas',impact); self.assertNotIn('_states',facts)
