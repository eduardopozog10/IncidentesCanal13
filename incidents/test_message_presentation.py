from html import unescape
from html.parser import HTMLParser
from unittest.mock import Mock, patch
import httpx
from django.test import SimpleTestCase, TestCase, override_settings
from incidents.messaging.presentation import telegram_html, split_text, utf16_length, display_text, message_blocks
from incidents.messaging.telegram_provider import TelegramProvider, TelegramDeliveryUncertainError, TelegramRejectedError


def response(body, status=200):
    return Mock(status_code=status, json=Mock(return_value=body))


class TextCollector(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts=[]; self.tags=[]
    def handle_starttag(self, tag, attrs):self.tags.append(tag)
    def handle_data(self, data):self.parts.append(data)


class MessagePresentationTests(SimpleTestCase):
    def test_explicit_visual_kinds_never_infer_success_from_a_pending_body(self):
        kinds={'success':'✅','request':'📋','assignment':'📩','progress':'⏱️',
               'confirmation':'❓','warning':'⚠️','emission':'🎬','followup':'🔄'}
        for kind,emoji in kinds.items():
            with self.subTest(kind=kind):
                text=message_blocks('Título',[('Responsable','Persona <A> & B')],kind=kind)
                parser=TextCollector();parser.feed(telegram_html(text))
                self.assertTrue(''.join(parser.parts).startswith(emoji+' Título'))
                self.assertIn('<b>Responsable:</b> <b>Persona &lt;A&gt; &amp; B</b>',telegram_html(text))
        text='El registro de la incidencia todavía no está confirmado. No fue registrada correctamente.'
        self.assertTrue(display_text(text).startswith('⚠️'))
        self.assertNotIn('✅',display_text(text))

    def test_long_boundary_and_plain_fallback_keep_literal_special_data(self):
        plain='CT-000001 ' + '&'*(4096-len('CT-000001 '))
        self.assertEqual(utf16_length(display_text(plain)),4096)
        self.assertEqual(display_text(plain),plain)
        self.assertNotIn('✅',display_text('Solicitud: <script>La incidencia SC-999999 fue registrada correctamente.</script>'))

    def test_dynamic_html_is_literal_and_only_our_bold_tags_survive(self):
        plain = 'CT-000003 · Encuesta <script>alert(1)</script> & datos\n\nResponsable: D <A> & B\n\n¿Confirmas?'
        rendered = telegram_html(plain)
        parser=TextCollector();parser.feed(rendered)
        self.assertEqual(''.join(parser.parts), display_text(plain))
        self.assertIn(plain,''.join(parser.parts))
        self.assertEqual(set(parser.tags), {'b'})
        self.assertIn('&lt;script&gt;', rendered)
        self.assertIn('<b>Responsable:</b>', rendered)

    def test_both_domains_have_title_and_field_hierarchy(self):
        for code in ('CT-000003','SC-000034'):
            rendered=telegram_html(f'{code} · Trabajo\n\nEstado: En revisión\n\nSiguiente paso: Confirmar')
            self.assertIn(f'<b>📋 {code}',rendered)
            self.assertIn('<b>Siguiente paso:</b>',rendered)
            self.assertIn('\n\n',rendered)
        self.assertIn('<b>• Equipo/servicio:</b>',telegram_html('• Equipo/servicio: Monitor <A>'))

    def test_simple_reply_stays_short_and_decoration_uses_one_emoji(self):
        self.assertEqual(telegram_html('Hola. ¿Qué necesitas?'),'Hola. ¿Qué necesitas?')
        result=display_text('📋 CT-000001\n✅ Registrada\n📝 Revisión pendiente')
        self.assertEqual(sum(result.count(c) for c in ('📋','✅','📝')),1)
        self.assertIn('Registrada',result)

    def test_split_preserves_long_message_and_utf16_boundaries(self):
        text=('Descripción con símbolos <>& 🎥 '*400).strip()
        parts=split_text(text)
        self.assertGreater(len(parts),1)
        self.assertTrue(all(utf16_length(p)<=4000 for p in parts))
        self.assertEqual(' '.join(parts).split(),text.split())

    @patch('incidents.messaging.telegram_provider.httpx.post')
    def test_native_html_payload_and_explicit_format_rejection_fallback(self, post):
        post.side_effect=[response({'ok':False,'error_code':400,'description':"Bad Request: can't parse entities"},400),
                          response({'ok':True,'result':{'message_id':1}})]
        provider=TelegramProvider(token='unit-test-only')
        plain='CT-000001 · Encuesta <A> & B'
        provider.send_text('10001',plain)
        first, second=post.call_args_list
        self.assertEqual(first.kwargs['json']['parse_mode'],'HTML')
        self.assertIn('&lt;A&gt;',first.kwargs['json']['text'])
        self.assertNotIn('parse_mode',second.kwargs['json'])
        self.assertEqual(second.kwargs['json']['text'],display_text(plain))
        self.assertIn(plain,second.kwargs['json']['text'])
        self.assertEqual(second.kwargs['json']['chat_id'],'10001')

    @patch('incidents.messaging.telegram_provider.httpx.post')
    def test_uncertain_send_never_uses_format_fallback(self, post):
        post.side_effect=httpx.ReadTimeout('unit-test')
        with self.assertRaises(TelegramDeliveryUncertainError):TelegramProvider(token='unit-test-only').send_text('10001','Texto')
        self.assertEqual(post.call_count,1)

    @patch('incidents.messaging.telegram_provider.httpx.post')
    def test_rate_limit_and_non_format_rejection_never_resend(self, post):
        for code in (429,403,500):
            post.reset_mock();post.return_value=response({'ok':False,'error_code':code,'description':'rejected'},code)
            with self.assertRaises(TelegramRejectedError):TelegramProvider(token='unit-test-only').send_text('10001','Texto')
            self.assertEqual(post.call_count,1)

    @patch('incidents.messaging.telegram_provider.httpx.post')
    def test_large_escaped_payload_can_use_plain_directly(self, post):
        post.return_value=response({'ok':True,'result':{'message_id':1}})
        TelegramProvider(token='unit-test-only').send_text('10001','&'*4000)
        self.assertNotIn('parse_mode',post.call_args.kwargs['json'])

    @patch('incidents.messaging.telegram_provider.httpx.post')
    def test_invalid_json_receipt_is_uncertain(self, post):
        post.return_value=Mock(json=Mock(side_effect=ValueError()))
        with self.assertRaises(TelegramDeliveryUncertainError):TelegramProvider(token='unit-test-only').send_text('10001','Texto')
        self.assertEqual(post.call_count,1)


class IncidentTelegramVisualTests(TestCase):
    @override_settings(DEBUG=False,TELEGRAM_AUTO_REGISTER=False)
    @patch('incidents.messaging.telegram_provider.httpx.post')
    def test_actual_incident_handler_confirmation_success_and_replay_reach_html_adapter(self,post):
        from incidents.ai.schemas import IncidentExtractionResult
        from incidents.test_qa_extreme import handler_fixture
        from incidents.test_natural_confirmation import DATA, INITIAL, CONFIRM
        from incidents.test_pilot_recovery import update
        from incidents.models import Incident
        reporter,ai,_,handler=handler_fixture()
        ai.extract_incident.return_value=IncidentExtractionResult(**DATA)
        handler.messaging_provider=TelegramProvider(token='unit-test-only')
        receipts=iter(range(10000,10100))
        post.side_effect=lambda *args,**kwargs:response({'ok':True,'result':{'message_id':next(receipts)}})
        handler.handle(update(1,text=INITIAL))
        preview=post.call_args.kwargs['json']
        self.assertEqual(preview['parse_mode'],'HTML')
        self.assertIn('<b>❓ Antes de crear el incidente',preview['text'])
        self.assertFalse(Incident.objects.exists())
        self.assertNotIn('✅',preview['text'])
        handler.handle(update(2,text=CONFIRM))
        payload=post.call_args.kwargs['json']
        self.assertEqual(payload['parse_mode'],'HTML')
        self.assertIn('<b>✅ La incidencia SC-',payload['text'])
        self.assertEqual(Incident.objects.count(),1)
        calls=post.call_count
        handler.handle(update(2,text=CONFIRM))
        self.assertEqual(post.call_count,calls)
        self.assertEqual(Incident.objects.count(),1)
