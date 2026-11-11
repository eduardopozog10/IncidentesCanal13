"""Bloquea transportes externos; permite HTTP MockTransport y PostgreSQL local."""
from contextlib import ExitStack
from unittest.mock import patch

from django.test.runner import DiscoverRunner


class OfflineTestRunner(DiscoverRunner):
    def run_tests(self, *args, **kwargs):
        with ExitStack() as guards:
            for target in ('httpx.HTTPTransport.handle_request',
                           'httpx.AsyncHTTPTransport.handle_async_request',
                           'urllib.request.urlopen', 'smtplib.SMTP'):
                guards.enter_context(patch(target, side_effect=AssertionError('Salida externa prohibida en tests offline.')))
            return super().run_tests(*args, **kwargs)
