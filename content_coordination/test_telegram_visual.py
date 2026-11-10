"""Salida real del outbox → TelegramProvider → HTTP simulado, sin Bot API real."""
from html.parser import HTMLParser
from unittest.mock import Mock, patch
import httpx

from django.test import TestCase, override_settings

from incidents.messaging.presentation import message_blocks, display_text, utf16_length
from incidents.messaging.telegram_provider import TelegramProvider
from .tests import Fixture, DATA
from .models import Conversation, Outbox, ProgressQuery
from .delivery import deliver_one


class VisibleHTML(HTMLParser):
    def __init__(self):
        super().__init__();self.text=[];self.stack=[];self.tags=[]
    def handle_starttag(self,tag,attrs):
        assert tag=='b' and not attrs
        self.stack.append(tag);self.tags.append(tag)
    def handle_endtag(self,tag):
        assert self.stack and self.stack.pop()==tag
    def handle_data(self,text):self.text.append(text)


@override_settings(CONTENT_LIVE_TELEGRAM=True)
class TelegramVisualTests(Fixture,TestCase):
    def setUp(self):
        super().setUp()
        for actor in [self.journalist,self.coordinator,self.worker,self.reviewer,self.editor,self.switch]:
            self.authorize_telegram(actor)
            Conversation.objects.create(participant=actor,module='content')

    def post(self):
        receipts=iter(range(10000,11000))
        def receipt(*args,**kwargs):
            return Mock(status_code=200,json=Mock(return_value={'ok':True,'result':{'message_id':next(receipts)}}))
        return patch('incidents.messaging.telegram_provider.httpx.post',side_effect=receipt)

    def deliver(self,out,post):
        # Seleccionar un aviso sintético sin borrar los demás registros.
        Outbox.objects.exclude(pk=out.pk).filter(status__in=['pending','retry']).update(status='local')
        Outbox.objects.filter(pk=out.pk).update(status='pending')
        post.reset_mock()
        result=deliver_one(TelegramProvider(token='unit-test-only'))
        self.assertEqual(result.pk,out.pk);self.assertEqual(result.status,'sent')
        return [call.kwargs['json'] for call in post.call_args_list]

    def reply(self,msg,post):
        return self.deliver(Outbox.objects.get(key=f'reply:{msg.pk}'),post)[0]

    def rendered(self,payload):
        self.assertEqual(payload.get('parse_mode'),'HTML')
        parser=VisibleHTML();parser.feed(payload['text']);parser.close()
        self.assertFalse(parser.stack)
        return ''.join(parser.text)

    def test_creation_and_distribution_are_distinct_not_duplicate_notifications(self):
        text='Necesito una gráfica de encuesta para Prensa, Gráfica, sin plazo'
        _,prompt=self.plan(self.journalist,text,[{'action':'create','evidence':text,'data':DATA}],channel='telegram')
        with self.post() as post:
            preview=self.reply(prompt,post)
            self.assertTrue(self.rendered(preview).startswith('❓'))
            self.assertNotIn('✅',self.rendered(preview))
            _,created=self.plan(self.journalist,'confirmo',channel='telegram')
            receipt=self.reply(created,post)
            self.assertTrue(self.rendered(receipt).startswith('✅ Solicitud registrada'))
            new=self.deliver(Outbox.objects.get(key__startswith='new:'),post)[0]
            self.assertTrue(self.rendered(new).startswith('📋 Nueva solicitud pendiente'))
            self.assertEqual(receipt['chat_id'],self.journalist.telegram_id)
            self.assertEqual(new['chat_id'],self.coordinator.telegram_id)
            self.assertNotEqual(new['chat_id'],receipt['chat_id'])

    def test_assignment_confirmation_delivery_and_start_reach_adapter_with_correct_semantics(self):
        r=self.request('Gráfica <Clima> & Noticias')
        text=f'Asigna {r.code} a Daniela'
        _,prompt=self.plan(self.coordinator,text,[{'action':'assign','evidence':text,'data':{}}],channel='telegram')
        with self.post() as post:
            preview=self.reply(prompt,post)
            self.assertTrue(self.rendered(preview).startswith('❓ ¿Confirmas esta asignación?'))
            self.assertIn('<b>Responsable:</b> <b>Daniela</b>',preview['text'])
            self.assertIsNone(r.tasks.get().assignee)
            _,confirmed=self.plan(self.coordinator,'confirmoS',channel='telegram')
            success=self.reply(confirmed,post)
            self.assertTrue(self.rendered(success).startswith('✅ Diseño asignado'))
            self.assertNotIn('recibió la notificación',self.rendered(success))
            notice=Outbox.objects.get(key__startswith='assign:')
            payload=self.deliver(notice,post)[0]
            self.assertTrue(self.rendered(payload).startswith('📩 Nueva tarea asignada'))
            self.assertEqual(payload['chat_id'],self.worker.telegram_id)
            self.assertIn('&lt;Clima&gt; &amp; Noticias',payload['text'])
            self.assertIn(f'<b>{r.code} · Gráfica &lt;Clima&gt; &amp; Noticias</b>',payload['text'])
            self.assertIn('<b>Plazo:</b>',payload['text'])
            self.assertIsNone(deliver_one(TelegramProvider(token='unit-test-only')))
            self.assertEqual(post.call_count,1)
            text=f'Ya recibí {r.code} y voy a comenzar el diseño'
            _,started=self.plan(self.worker,text,[{'action':'receive','evidence':text,'data':{}}],channel='telegram')
            self.assertTrue(self.rendered(self.reply(started,post)).startswith('✅ Diseño iniciado'))

    def test_followup_queue_and_partial_response_have_distinct_honest_indicators(self):
        r=self.request(worker=self.worker);self.action(self.worker,r,'start')
        text=f'¿Cómo va Daniela con {r.code}?'
        _,offer=self.plan(self.coordinator,text,channel='telegram')
        with self.post() as post:
            payload=self.reply(offer,post)
            self.assertTrue(self.rendered(payload).startswith('🔄'))
            self.assertFalse(ProgressQuery.objects.exists())
            _,confirmed=self.plan(self.coordinator,'Sí, pregúntale',channel='telegram')
            queued=self.rendered(self.reply(confirmed,post))
            self.assertTrue(queued.startswith('🔄 Seguimiento pendiente de envío'))
            self.assertNotIn('✅',queued);self.assertNotIn('ya recibió',queued)
            q=ProgressQuery.objects.get()
            notice=self.deliver(q.notification,post)[0]
            self.assertTrue(self.rendered(notice).startswith('🔄 Consulta de seguimiento'))
            text='Me faltan 15 minutos'
            _,progress=self.plan(self.worker,text,[{'action':'progress','evidence':text,'data':{
                'progress':text,'eta_minutes':15,'reply_to_followup':True}}],channel='telegram')
            reply=self.rendered(self.reply(progress,post))
            self.assertTrue(reply.startswith('⏱️ Avance registrado'))
            self.assertIn('En producción',reply)
            answer=self.deliver(Outbox.objects.get(key__startswith='query-answer:'),post)[0]
            self.assertEqual(answer['chat_id'],self.coordinator.telegram_id)
            self.assertTrue(self.rendered(answer).startswith('⏱️ Nuevo avance de'))
            self.assertIn('<b>Estimación informada:</b>',answer['text'])
            self.assertIn('15 minutos',self.rendered(answer))
            self.assertIsNone(r.tasks.get().completed_at)

    def test_review_editing_switch_and_emission_use_one_shared_formatter(self):
        r=self.review()
        with self.post() as post:
            for actor,action,text in [
                (self.reviewer,'approve',f'Apruebo la versión 1.0 de {r.code}'),
                (self.editor,'edit',f'Incorporé la versión 1.0 de {r.code} a la nota'),
                (self.switch,'receive',f'Switch recibió la versión 1.0 aprobada de {r.code}'),
                (self.switch,'emit',f'Confirma la emisión de la versión 1.0 de {r.code}')]:
                _,msg=self.plan(actor,text,[{'action':action,'evidence':text,'data':{'version':'1.0'}}],channel='telegram')
                payload=self.reply(msg,post)
                if action in {'approve','emit'}:
                    self.assertTrue(self.rendered(payload).startswith('❓'))
                    self.assertNotIn('✅',self.rendered(payload))
                    _,msg=self.plan(actor,'confirmo',channel='telegram')
                    payload=self.reply(msg,post)
                visible=self.rendered(payload)
                self.assertTrue(visible.startswith('🎬 Emisión confirmada' if action=='emit' else '✅'))
                self.assertIn('<b>Versión:</b>',payload['text'])
                if action=='approve':
                    self.assertTrue(self.rendered(self.deliver(Outbox.objects.get(key__startswith='approved:'),post)[0]).startswith('📩'))
                if action=='edit':
                    self.assertTrue(self.rendered(self.deliver(Outbox.objects.get(key__startswith='edited:'),post)[0]).startswith('📩'))
                if action=='receive':self.assertIsNone(r.versions.get().emitted_at)

    def test_real_restriction_and_ambiguity_do_not_look_like_success(self):
        r=self.request(worker=self.worker)
        with self.post() as post:
            text=f'Switch recibió la versión 1.0 aprobada de {r.code}'
            _,msg=self.plan(self.worker,text,[{'action':'receive','evidence':text,'data':{'version':'1.0'}}],channel='telegram')
            denied=self.rendered(self.reply(msg,post))
            self.assertTrue(denied.startswith('⚠️ Acción no autorizada'))
            self.assertNotIn('✅',denied)
            text=f'Ya recibí {r.code}'
            _,msg=self.plan(self.worker,text,[{'action':'receive','evidence':text,'data':{}}],channel='telegram')
            ambiguous=self.rendered(self.reply(msg,post))
            self.assertTrue(ambiguous.startswith('❓ Necesito una aclaración'))
            self.assertNotIn('✅',ambiguous)

    def test_long_special_data_is_safe_in_every_fragment_and_preserves_information(self):
        detail=('Datos <script>alert("x")</script> & señal 🎥\n'*230).strip()
        text=message_blocks('Información de la solicitud',[('Descripción',detail)],kind='request')
        out=Outbox.objects.create(recipient=self.worker,key='long-visual',text=text)
        with self.post() as post:
            payloads=self.deliver(out,post)
        self.assertGreater(len(payloads),1)
        pieces=[self.rendered(p) for p in payloads]
        self.assertTrue(all(utf16_length(p)<=4000 for p in pieces))
        self.assertEqual(' '.join(pieces).split(),display_text(text).split())
        for p in payloads:self.assertNotIn('<script>',p['text'])
        out.refresh_from_db();self.assertEqual(len(out.delivery_parts),len(payloads))

    def test_format_fallback_has_one_receipt_and_never_duplicates_notification(self):
        text=message_blocks('Nueva tarea asignada',[('Responsable','Daniela <A> & B')],kind='assignment')
        out=Outbox.objects.create(recipient=self.worker,key='fallback-visual',text=text)
        failure=Mock(status_code=400,json=Mock(return_value={'ok':False,'error_code':400,'description':"can't parse entities"}))
        success=Mock(status_code=200,json=Mock(return_value={'ok':True,'result':{'message_id':12}}))
        with patch('incidents.messaging.telegram_provider.httpx.post',side_effect=[failure,success]) as post:
            payloads=self.deliver(out,post)
            self.assertEqual(payloads[0]['parse_mode'],'HTML')
            self.assertNotIn('parse_mode',payloads[1])
            self.assertEqual(payloads[1]['text'],text)
            self.assertIsNone(deliver_one(TelegramProvider(token='unit-test-only')))
            self.assertEqual(post.call_count,2)
        out.refresh_from_db();self.assertEqual(len(out.delivery_parts),1)

    def test_timeout_never_triggers_a_plain_resend(self):
        out=Outbox.objects.create(recipient=self.worker,key='uncertain-visual',text=message_blocks('Seguimiento pendiente',kind='followup'))
        with patch('incidents.messaging.telegram_provider.httpx.post',side_effect=httpx.ReadTimeout('synthetic')) as post:
            Outbox.objects.exclude(pk=out.pk).update(status='local')
            result=deliver_one(TelegramProvider(token='unit-test-only'))
            self.assertEqual(result.status,'uncertain')
            self.assertIsNone(deliver_one(TelegramProvider(token='unit-test-only')))
            self.assertEqual(post.call_count,1)
