from contextlib import nullcontext
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from .tests import Fixture
from .models import Message, Outbox, Conversation
from .routing import queue_update
from .telegram_polling import TelegramPollingClient, inspect_pending
from .management.commands.run_secuoya_demo import check_other_receivers


def response(body, status=200, headers=None):
    return SimpleNamespace(status_code=status, headers=headers or {}, json=lambda: body)


class Clock:
    def __init__(self):
        self.time = 0
        self.waits = []

    def now(self):
        return self.time

    def sleep(self, duration):
        self.waits.append(duration)
        self.time += duration


class PollingTests(TestCase):
    def test_competitor_blocks_start_without_stopping_processes(self):
        with patch('content_coordination.management.commands.run_secuoya_demo.subprocess.run',
                   return_value=SimpleNamespace(returncode=0, stdout='1234')):
            with self.assertRaisesMessage(CommandError, '1234'):
                check_other_receivers()

    def test_failed_process_check_blocks_start(self):
        with patch('content_coordination.management.commands.run_secuoya_demo.subprocess.run',
                   return_value=SimpleNamespace(returncode=1, stdout='')):
            with self.assertRaisesMessage(CommandError, 'No se pudo verificar'):
                check_other_receivers()

    def polling_client(self, replies):
        clock = Clock()
        http = Mock()
        http.post.side_effect = replies
        polling = TelegramPollingClient(http, 'https://example.invalid/bot', Mock(), sleep=clock.sleep, now=clock.now)
        return polling, http, clock

    def test_429_waits_retry_after_and_reuses_exact_offset(self):
        polling, http, clock = self.polling_client([
            response({'ok': False, 'parameters': {'retry_after': 7}}, 429),
            response({'ok': True, 'result': []})])
        self.assertEqual(polling.call('getUpdates', {'offset': 41975081, 'timeout': 25}), [])
        self.assertEqual(clock.time, 7)
        self.assertEqual(http.post.call_args_list[0], http.post.call_args_list[1])

    def test_long_retry_is_interruptible_in_short_segments(self):
        polling, _, clock = self.polling_client([
            response({'ok': False, 'parameters': {'retry_after': 75}}, 429),
            response({'ok': True, 'result': []})])
        polling.call('getUpdates')
        self.assertEqual(sum(clock.waits), 75)
        self.assertLessEqual(max(clock.waits), 30)

    def test_429_during_inspection_never_adds_an_offset(self):
        polling, http, _ = self.polling_client([
            response({'ok': False, 'parameters': {'retry_after': 2}}, 429),
            response({'ok': True, 'result': []})])
        polling.call('getUpdates', {'timeout': 0, 'limit': 100})
        for call in http.post.call_args_list:
            self.assertNotIn('offset', call.kwargs['json'])

    def test_repeated_429_and_malformed_delay_do_not_busy_loop(self):
        polling, http, clock = self.polling_client([
            response({'ok': False, 'parameters': {'retry_after': 'nan'}}, 429),
            response({'ok': False, 'parameters': {'retry_after': 3}}, 429),
            response({'ok': True, 'result': []})])
        polling.call('getUpdates')
        self.assertEqual(clock.time, 33)
        self.assertEqual(http.post.call_count, 3)

    def test_immediate_responses_are_throttled(self):
        polling, _, clock = self.polling_client([response({'ok': True, 'result': []})] * 2)
        polling.call('getUpdates'); polling.call('getUpdates')
        self.assertEqual(clock.time, 1)

    def test_conflicting_receiver_409_is_not_retried(self):
        polling, http, _ = self.polling_client([response({'ok': False}, 409)])
        with self.assertRaises(CommandError):
            polling.call('getUpdates')
        self.assertEqual(http.post.call_count, 1)

    def test_ctrl_c_interrupts_wait_before_another_request(self):
        polling, http, _ = self.polling_client([response({'ok': False, 'parameters': {'retry_after': 5}}, 429)])
        polling.sleep = Mock(side_effect=KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            polling.call('getUpdates')
        self.assertEqual(http.post.call_count, 1)


class PendingInspectionTests(Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.worker.telegram_id = '6690350935'
        self.worker.telegram_authorized = True
        self.worker.save()
        self.update = {'update_id': 41975081, 'message': {
            'from': {'id': 6690350935}, 'chat': {'id': 6690350935, 'type': 'private'},
            'message_id': 99, 'date': 1794222000, 'text': 'No la ingreses todavía.\n\x1b[31m'}}

    def invoke(self, **options):
        http = Mock()
        http.post.side_effect = [response({'ok': True, 'result': {'id': 777, 'username': 'secuoyachile_incidentes_bot'}}),
                                 response({'ok': True, 'result': {'url': ''}}),
                                 response({'ok': True, 'result': [self.update]})]
        module = 'content_coordination.management.commands.run_secuoya_demo'
        out = StringIO()
        with patch(module + '.ensure_demo'), patch(module + '.check_other_receivers'), \
                patch(module + '.receiver_lock', return_value=nullcontext()), \
                patch(module + '.httpx.Client') as client:
            client.return_value.__enter__.return_value = http
            call_command('run_secuoya_demo', stdout=out, **options)
        return out.getvalue(), http

    def test_inspection_is_read_only_and_escapes_terminal_controls(self):
        output, http = self.invoke(inspect_pending_id=41975081)
        self.assertIn('41975081', output)
        self.assertIn('6690350935', output)
        self.assertIn('\\u001b', output)
        self.assertNotIn('\x1b', output)
        self.assertFalse(Message.objects.exists())
        self.assertFalse(Outbox.objects.exists())
        self.assertFalse(Conversation.objects.exists())
        self.assertEqual(http.post.call_count, 3)
        self.assertEqual(http.post.call_args.kwargs['json'], {'timeout': 0, 'limit': 100})

    def test_unknown_pending_still_blocks_live_start(self):
        with self.assertRaisesMessage(CommandError, 'Revisa el contexto'):
            self.invoke(live=True)
        self.assertFalse(Message.objects.exists())

    def test_third_party_text_hidden(self):
        self.update['message']['from']['id'] = 42
        output = inspect_pending([self.update], 41975081)
        self.assertIn('false', output)
        self.assertNotIn('No la ingreses', output)

    def test_unseen_id_does_not_seek_forward(self):
        output = inspect_pending([self.update], 41975082)
        self.assertIn('No se avanzó', output)
        self.assertFalse(Message.objects.exists())

    def test_inspection_preserves_deduplication_when_explicitly_queued_later(self):
        inspect_pending([self.update], 41975081)
        first = queue_update(self.update)
        second = queue_update(self.update)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Message.objects.count(), 1)
        self.assertEqual(first.status, 'pending')

