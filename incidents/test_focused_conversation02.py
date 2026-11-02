"""Regresiones de la batería manual: proveedor/canal simulados, PostgreSQL QA."""
from copy import deepcopy
from unittest.mock import patch
from django.test import TestCase, SimpleTestCase, override_settings
from incidents.ai.schemas import IncidentExtractionResult
from incidents.models import ChannelMessage, Incident, IncidentDraft
from incidents.messaging.telegram_provider import TelegramRejectedError
from incidents.test_natural_confirmation import ConfirmationFixture, DATA
from incidents.test_user_management import SETTINGS

LONG_AUTH = ('Ya revisé el reporte completo de la cámara 3 del Estudio A y está todo correcto.\n'
    'Dale, puedes ingresarlo al sistema. Te confirmo que autorizo registrar esta incidencia ahora con los datos que acabamos de revisar.\n'
    'Con eso quedamos listos, gracias.')
SUMMARY = 'Muéstrame el reporte completo con todos los datos antes de autorizarlo.'

@override_settings(DEBUG=False, TELEGRAM_AUTO_REGISTER=False, PILOT_MESSAGE_RETRY_SECONDS=0, **SETTINGS)
class FocusedConversationTests(ConfirmationFixture, TestCase):
    def interpret(self,data=None,**extra):
        self.ai.extract_incident.return_value=IncidentExtractionResult(**deepcopy(data or DATA),**extra)

    def camera_ready(self):
        self.interpret({**DATA,'equipment':'cámara 3'})
        return self.ready()

    def authorize_long(self,draft):
        self.interpret(draft.extracted_data,dialogue_action='confirm',dialogue_actions=['confirm'],
            action_evidence='autorizo registrar esta incidencia ahora',authorization={'kind':'confirm','evidence':'autorizo registrar esta incidencia ahora',
                'direct':True,'conditional':False,'delayed':False,'third_party':False,'requires_changes':False,
                'equipment':'cámara 3','location_context':'Estudio A'})

    def briefly_update(self,draft):
        self.interpret({**draft.extracted_data,'priority':'critical'},dialogue_actions=['update','query'],requested_fields=['priority'],
            field_evidence={'priority':'Crítica'})
        self.send('Cambia la prioridad a Crítica. ¿Qué prioridad quedó?')
        draft.refresh_from_db()
        self.assertNotIn('¿Confirmas',self.last_reply())

    def test_help_with_facts_overrides_incidental_query_about_missing_area(self):
        text='Consola del Estudio B con cortes. Las pruebas se detienen a ratos. Prioridad media. No sé si corresponde a Audio o Producción. ¿Me orientas para identificar el área correcta?'
        self.interpret({**DATA,'area':None,'equipment':'consola de audio','location_context':'Estudio B'},
            dialogue_actions=['update','help'],dialogue_action='help',help_field='area',action_evidence='Me orientas',requested_fields=['area'])
        self.send(text)
        draft=IncidentDraft.objects.get()
        self.assertIsNone(draft.extracted_data['area'])
        self.assertIn('Audio',self.last_reply()); self.assertIn('Producción',self.last_reply())
        self.assertIn('consola',self.last_reply()); self.assertNotIn('Área: No informado',self.last_reply())

    def test_generic_preparation_help_is_not_a_request_for_saved_fields(self):
        text='Cámara 3 en Estudio A sin imagen, Producción. Detiene grabaciones. Prioridad alta. ¿Me ayudas a dejar el reporte bien armado? Quiero revisarlo antes de registrarlo.'
        self.interpret({**DATA,'equipment':'cámara 3'},dialogue_actions=['update','query'],requested_fields=['equipment','area','priority'])
        self.send(text)
        self.assertIn('preparé',self.last_reply()); self.assertIn('cámara 3',self.last_reply())
        self.assertNotIn('Según el borrador guardado',self.last_reply())

    def test_requested_full_summary_authorizes_current_data_without_mutating_query(self):
        draft=self.camera_ready(); self.briefly_update(draft)
        before=(deepcopy(draft.extracted_data),draft.updated_at,draft.last_question)
        self.send(SUMMARY); draft.refresh_from_db()
        self.assertEqual((draft.extracted_data,draft.updated_at,draft.last_question),before)
        self.authorize_long(draft); result=self.send(LONG_AUTH)
        self.assertTrue(result.get('incident_created'),result)
        self.assertEqual(Incident.objects.get().priority,'critical')
        self.send(LONG_AUTH); self.assertEqual(Incident.objects.count(),1)

    def test_failed_full_summary_cannot_authorize_current_data(self):
        draft=self.camera_ready(); self.briefly_update(draft)
        transport=self.messaging.send_text.side_effect
        self.messaging.send_text.side_effect=TelegramRejectedError()
        self.send(SUMMARY)
        self.messaging.send_text.side_effect=transport
        self.authorize_long(draft); self.send(LONG_AUTH)
        self.assertFalse(Incident.objects.exists())

    def test_repeat_ignores_review_prose_extracted_as_new_incident_description(self):
        draft=self.camera_ready(); self.authorize_long(draft); self.send(LONG_AUTH)
        self.assertEqual(Incident.objects.count(),1)
        self.authorize_long(draft)
        extraction=self.ai.extract_incident.return_value
        extraction.description='Reporte revisado y correcto.'
        extraction.equipment='Cámara 3'
        extraction.field_evidence={'description':'Ya revisé el reporte completo','equipment':'cámara 3'}
        extraction.dialogue_actions=['confirm','update']
        calls=self.ai.extract_incident.call_count
        result=self.send(LONG_AUTH)
        self.assertEqual(result['action'],'incident_already_created')
        self.assertEqual(Incident.objects.count(),1)
        self.assertEqual(IncidentDraft.objects.count(),1)
        self.assertEqual(self.ai.extract_incident.call_count,calls)

    def test_targeted_query_cannot_replace_full_summary(self):
        draft=self.camera_ready(); self.briefly_update(draft)
        self.send('¿Qué prioridad tiene guardada?')
        self.authorize_long(draft); self.send(LONG_AUTH)
        self.assertFalse(Incident.objects.exists())

    def test_new_correction_invalidates_previously_delivered_query_summary(self):
        draft=self.camera_ready(); self.send(SUMMARY); self.briefly_update(draft)
        self.authorize_long(draft); self.send(LONG_AUTH)
        self.assertFalse(Incident.objects.exists())

    def test_stored_area_query_stays_a_query(self):
        draft=self.camera_ready(); original=deepcopy(draft.extracted_data)
        self.send('¿Qué área tiene anotada mi incidencia?')
        draft.refresh_from_db(); self.assertEqual(draft.extracted_data,original)
        self.assertIn('Área: Producción',self.last_reply())
        self.assertNotIn('no la voy a suponer',self.last_reply())

    def test_guidance_variants_do_not_turn_into_saved_area_queries(self):
        for text in ('No cacho a qué área pertenece la consola. ¿Me puedes orientar?',
                'Desconozco qué área gestiona la consola, necesito orientación.',
                'No estoy seguro si corresponde a Audio o Producción. ¿Cómo identifico el área?'):
            with self.subTest(text=text):
                IncidentDraft.objects.all().delete()
                self.interpret({**DATA,'area':None,'equipment':'consola de audio'})
                self.send('Consola sin sonido en Estudio A, impide las pruebas. Prioridad alta. No sé el área.')
                self.interpret({**DATA,'area':None,'equipment':'consola de audio'},dialogue_actions=['update','help'],
                    dialogue_action='help',help_field='area',action_evidence=text,requested_fields=['area'])
                self.send(text)
                self.assertIn('no la voy a suponer',self.last_reply()); self.assertIn('consola',self.last_reply())
                self.assertNotIn('Área: No informado',self.last_reply())

    def test_full_summary_delivered_after_authorization_is_not_valid(self):
        from datetime import timedelta
        from django.utils import timezone
        draft=self.camera_ready(); self.briefly_update(draft); self.send(SUMMARY)
        ChannelMessage.objects.filter(direction='outbound',content=self.last_reply()).update(processed_at=timezone.now()+timedelta(minutes=5))
        self.authorize_long(draft); self.send(LONG_AUTH)
        self.assertFalse(Incident.objects.exists())

class FocusedContentTests(SimpleTestCase):
    def test_output_constraints_do_not_add_unrequested_backup_fields(self):
        from incidents.services.conversation_content import query_fields
        for constraint in ('sin repetir la conexión del respaldo','no me muestres el respaldo'):
            self.assertEqual(query_fields('Dime solo el impacto operacional, '+constraint+'.'),{'operational_impact'})

    def test_generic_preparation_variants_are_not_pure_queries(self):
        from incidents.services.conversation_content import report_preparation_requested,is_query_only
        for text in ('¿Me ayudas a dejar el reporte bien armado?', 'Necesito apoyo para preparar esta incidencia',
                '¿Puedes ayudarme a completar el borrador?'):
            with self.subTest(text=text):
                self.assertTrue(report_preparation_requested(text)); self.assertFalse(is_query_only(text))
    def test_backup_connection_already_in_consequence_is_not_rendered_twice(self):
        from incidents.services.conversation_plan import render_operational_facts
        facts={'consequence':'Retomamos las grabaciones tras conectar la cámara de respaldo.',
            'backup':'Recién conectamos la cámara de respaldo.', 'original_fault':'La cámara original sigue fallando.'}
        rendered=render_operational_facts(facts)
        self.assertEqual(rendered,'Retomamos las grabaciones tras conectar la cámara de respaldo. La cámara original sigue fallando.')
        self.assertIn('Recién',facts['backup'])

    def test_distinct_backup_qualifiers_are_preserved(self):
        from incidents.services.conversation_plan import render_operational_facts
        for backup in ('Conectamos la cámara de respaldo pero pierde señal.', 'Conectamos la cámara 7 de respaldo.',
                'Podríamos conectar la cámara de respaldo.', 'Conectamos la cámara de respaldo hace 5 minutos.'):
            with self.subTest(backup=backup):
                self.assertIn(backup,render_operational_facts({'consequence':'Retomamos tras conectar la cámara de respaldo.','backup':backup}))

    def test_bare_connection_is_covered_by_asserted_resumption_with_backup(self):
        from incidents.services.conversation_plan import render_operational_facts
        facts={'consequence':'Grabaciones retomadas con respaldo','backup':'Cámara de respaldo conectada',
            'original_fault':'El equipo original sigue fallando','_states':{'consequence':'resumed'}}
        self.assertEqual(render_operational_facts(facts),'Grabaciones retomadas con respaldo. El equipo original sigue fallando.')
        for backup in ('Cámara de respaldo conectada hace 5 minutos','Cámara 7 de respaldo conectada','Cámara de respaldo conectada pero inestable'):
            self.assertIn(backup,render_operational_facts({**facts,'backup':backup}))
