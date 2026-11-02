"""Duración de la falla y facetas operacionales: regresiones sin APIs reales."""
from copy import deepcopy
from django.test import TestCase, SimpleTestCase, override_settings
from incidents.ai.schemas import IncidentExtractionResult
from incidents.models import IncidentDraft, Incident
from incidents.test_natural_confirmation import ConfirmationFixture, DATA
from incidents.test_user_management import SETTINGS
from incidents.services.conversation_content import time_fact, replace_time, preserve_modality
from incidents.services.conversation_plan import merge_operational_facts, render_operational_facts

ELAPSED='Ya llevamos aproximadamente 35 minutos con el problema.'
TRANSITION=('Hace poco conectamos la cámara de respaldo y pudimos retomar las pruebas de grabación, '
    'pero la cámara 5 sigue perdiendo la señal de video de manera intermitente.')
UPDATE=ELAPSED+' '+TRANSITION+' Mantengamos la prioridad alta. Actualiza esa información y dime brevemente el tiempo de la falla y el impacto operacional.'

@override_settings(DEBUG=False,TELEGRAM_AUTO_REGISTER=False,PILOT_MESSAGE_RETRY_SECONDS=0,**SETTINGS)
class DurationFlowTests(ConfirmationFixture,TestCase):
    def ready_camera(self):
        self.ai.extract_incident.return_value=IncidentExtractionResult(**{**deepcopy(DATA),'equipment':'Cámara 5',
            'description':'Hace como 20 minutos la cámara 5 empezó a perder la señal. Sospechamos del cable, sin confirmar.'})
        return self.ready()

    def update(self,draft,text):
        self.ai.extract_incident.return_value=IncidentExtractionResult(**{**{k:v for k,v in draft.extracted_data.items() if not k.startswith('_')},
            'description':draft.extracted_data['description']+' '+text.split('.')[0]+'.'},update_kind='additional',
            field_evidence={'description':text.split('.')[0]},dialogue_actions=['update','query'],requested_fields=['time'])
        return self.send(text)

    def test_elapsed_update_replaces_old_duration_in_storage_and_reply(self):
        draft=self.ready_camera(); self.update(draft,UPDATE); draft.refresh_from_db()
        self.assertNotIn('20 minutos',draft.extracted_data['description'])
        self.assertIn('35 minutos',draft.extracted_data['description']); self.assertIn('35 minutos',self.last_reply())
        self.assertNotIn('20 minutos',self.last_reply()); self.assertIn('sin confirmar',draft.extracted_data['description'])
        self.assertFalse(Incident.objects.exists())

    def test_consecutive_elapsed_updates_do_not_accumulate_previous_values(self):
        draft=self.ready_camera()
        for text,expected in ((ELAPSED,'35 minutos'),('La falla lleva cerca de 50 minutos.','50 minutos'),
                ('Actualiza la duración a una hora.','una hora')):
            self.update(draft,text); draft.refresh_from_db()
            self.assertIn(expected,time_fact(draft.extracted_data['description']))
            for old in ('20 minutos','35 minutos','50 minutos'):
                if old!=expected:self.assertNotIn(old,draft.extracted_data['description'])

class DurationTextTests(SimpleTestCase):
    def test_natural_elapsed_forms(self):
        for phrase in ('llevamos aproximadamente 35 minutos','lleva ahora 45 minutos','llevan unos 50 minutos',
                'han pasado cerca de dos horas','van 40 minutos con esta falla','duración a una hora','Sí llevamos 35 minutos con la falla'):
            with self.subTest(phrase=phrase):self.assertIsNotNone(time_fact(phrase))

    def test_backup_event_is_not_the_incident_duration(self):
        self.assertIsNone(time_fact('Hace 5 minutos conectamos la cámara de respaldo.'))
        self.assertIn('35 minutos',time_fact(ELAPSED+' Hace 5 minutos conectamos el respaldo.'))

    def test_replacement_preserves_event_duration_and_explicit_start_clock(self):
        original='La falla comenzó a las 10:05, hace 20 minutos. Hace 5 minutos conectamos el respaldo.'
        result=replace_time(original,'llevamos aproximadamente 35 minutos')
        self.assertIn('10:05',result); self.assertIn('Hace 5 minutos conectamos',result)
        self.assertNotIn('20 minutos',result); self.assertIn('35 minutos',result)

    def test_start_clock_is_not_a_duration_and_is_not_calculated(self):
        self.assertIsNone(time_fact('La falla comenzó a las 10:05.'))
        self.assertEqual(replace_time('La falla comenzó a las 10:05.','llevamos 35 minutos'),'La falla comenzó a las 10:05.')

    def test_time_only_append_is_folded_but_new_technical_fact_is_kept(self):
        original='La señal falla desde hace 20 minutos. Ya llevamos aproximadamente 35 minutos con el problema.'
        result=replace_time(original,'llevamos aproximadamente 35 minutos')
        self.assertEqual(result.lower().count('35 minutos'),1)
        richer=replace_time(original+' Llevamos 35 minutos con pérdida de audio también.','llevamos 35 minutos')
        self.assertIn('pérdida de audio también',richer)

    def test_negative_and_hypothetical_times_are_not_asserted(self):
        for text in ('No lleva 35 minutos.','Podríamos llevar 35 minutos con la falla.',
                'Si llevamos 35 minutos, habrá que avisar.'):
            with self.subTest(text=text):self.assertIsNone(time_fact(text))

class OperationalCompositionTests(SimpleTestCase):
    def result(self):
        return IncidentExtractionResult(operational_updates=[
            {'facet':'consequence','text':'Se retoman las pruebas de grabación usando la cámara de respaldo.',
             'evidence':TRANSITION,'state':'resumed'},
            {'facet':'backup','text':'Cámara de respaldo en uso.','evidence':TRANSITION,'state':'in_use'},
            {'facet':'original_fault','text':'La cámara 5 sigue perdiendo la señal de video de manera intermitente.',
             'evidence':'la cámara 5 sigue perdiendo la señal de video de manera intermitente','state':'faulty'}])

    def test_resumption_evidence_does_not_expand_backup_into_entire_paragraph(self):
        text=preserve_modality('Cámara de respaldo en uso.',TRANSITION)
        self.assertNotIn('pero',text); self.assertNotIn('cámara 5',text)

    def test_merge_keeps_history_and_independent_facts_without_semantic_duplicates(self):
        previous={'_operational_facts':{'consequence':'Interrupciones en las pruebas de grabación.',
            'backup':'Cámara de respaldo disponible, sin conectar.'}}
        facts,impact=merge_operational_facts(previous,self.result(),TRANSITION)
        self.assertEqual(impact.lower().count('retom'),1)
        self.assertEqual(impact.lower().count('sigue perdiendo'),1)
        self.assertIn('intermitente',impact)
        self.assertIn('Interrupciones',str(facts['_history'])); self.assertIn('sin conectar',str(facts['_history']))
        self.assertEqual(facts['_states']['backup'],'in_use')

    def test_similar_words_do_not_remove_different_equipment_or_qualifiers(self):
        facts={'consequence':'Pruebas retomadas usando la cámara de respaldo.', '_states':{'consequence':'resumed'},
            'backup':'La cámara 7 de respaldo está en uso, con pérdida de audio.',
            'original_fault':'La cámara 5 sigue perdiendo video intermitentemente.'}
        impact=render_operational_facts(facts)
        for fragment in ('cámara 7','pérdida de audio','cámara 5','video intermitentemente'):self.assertIn(fragment,impact)

    def test_inferred_current_consequence_does_not_keep_its_stale_state(self):
        previous={'_operational_facts':{'consequence':'Pruebas detenidas','_states':{'consequence':'stopped'}}}
        extraction=IncidentExtractionResult(operational_updates=[{'facet':'continuity','text':'Pruebas reanudadas con respaldo',
            'evidence':'retomamos las pruebas con el respaldo','state':'resumed','backup_state':'in_use'}])
        facts,impact=merge_operational_facts(previous,extraction,'retomamos las pruebas con el respaldo')
        self.assertEqual(facts['_states']['consequence'],'resumed')
        self.assertIn('Pruebas detenidas',str(facts['_history'])); self.assertNotIn('detenidas',impact)

    def test_available_or_possible_backup_does_not_mean_operation_resumed(self):
        for source in ('Podríamos conectar el respaldo y retomar las pruebas.','Conectamos el respaldo, pero no pudimos retomar las pruebas.'):
            self.assertEqual(preserve_modality('Cámara de respaldo en uso.',source),source)
