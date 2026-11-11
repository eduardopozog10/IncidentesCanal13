"""Ejecuta las regresiones existentes vinculadas al corpus, sin duplicarlas."""
from contextlib import contextmanager
from io import StringIO
import os
from time import perf_counter
from unittest import TextTestResult, TextTestRunner
from unittest.mock import patch

from django.conf import settings
from django.test.runner import DiscoverRunner

from .core import manifest


def require_isolation():
    database = settings.DATABASES["default"]
    if not (
        settings.SETTINGS_MODULE == "config.incident_evaluation"
        and getattr(settings, "INCIDENT_EVALUATION_ONLY", False)
        and database["ENGINE"] == "django.db.backends.postgresql"
        and database["NAME"] == "secuoya_evaluation_unused"
        and database.get("TEST", {}).get("NAME") == "test_secuoya_incident_evaluation"
        and settings.EMAIL_BACKEND == "django.core.mail.backends.locmem.EmailBackend"
        and not settings.TELEGRAM_BOT_TOKEN and not settings.GEMINI_API_KEY
        and settings.AI_PROVIDER == "mock" and not settings.CONTENT_COORDINATION_ENABLED
    ):
        raise ValueError("Usar exclusivamente --settings=config.incident_evaluation; aislamiento inválido")


@contextmanager
def no_external_connections():
    """PostgreSQL usa libpq; todo socket Python y SMTP reales quedan bloqueados.

    HTTPX MockTransport y proveedores Mock siguen funcionando en memoria.
    Incluso un intento capturado por el código se registra y hace fallar el run.
    """
    attempts = []

    def blocked(*args, **kwargs):
        attempts.append("Conexión externa bloqueada")
        raise AssertionError("La evaluación offline prohíbe conexiones externas")

    with patch("socket.socket.connect", blocked), patch("socket.socket.connect_ex", blocked), \
            patch("socket.create_connection", blocked), patch("smtplib.SMTP.connect", blocked):
        yield attempts


class RecordingResult(TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcomes = {}

    def addSuccess(self, test):
        super().addSuccess(test)
        self.outcomes[test.id()] = {"status": "passed"}

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.outcomes[test.id()] = {"status": "failed", "detail": self._exc_info_to_string(err, test)}

    def addError(self, test, err):
        super().addError(test, err)
        self.outcomes[test.id()] = {"status": "error", "detail": self._exc_info_to_string(err, test)}

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.outcomes[test.id()] = {"status": "skipped", "detail": reason}

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err:
            self.outcomes[test.id()] = {"status": "failed", "detail": self._exc_info_to_string(err, test)}


class EvaluationRunner(DiscoverRunner):
    def run_suite(self, suite, **kwargs):
        self.result = TextTestRunner(stream=StringIO(), verbosity=self.verbosity, resultclass=RecordingResult).run(suite)
        return self.result


def run_backend(corpus, agent_version, *, verbosity=1):
    require_isolation()
    labels = list(dict.fromkeys(label for case in corpus["cases"] for label in case["backend_tests"]))
    runner = EvaluationRunner(interactive=False, verbosity=verbosity, parallel=1, keepdb=False)
    started = perf_counter()
    # Algunos tests históricos exportan evidencia si esta variable está activa.
    # No sobrescribir esos informes durante una evaluación reproducible.
    with no_external_connections() as attempts, patch.dict(os.environ, {"SECUOYA_QA_EVIDENCE": "0"}):
        failures = runner.run_tests(labels)
    outcomes = runner.result.outcomes
    rows = []
    for case in corpus["cases"]:
        tests = [{"test_id": label, **outcomes.get(label, {"status": "not_run"})} for label in case["backend_tests"]]
        passed = not attempts and all(test["status"] == "passed" for test in tests)
        rows.append({"case_id": case["id"], "trial": 1, "passed": passed,
                     "category": "backend_contract_passed" if passed else "backend_contract_failed",
                     "diagnostic_area": case["category"], "expected_decision": case["expected_decision"],
                     "interpretation_correct": None, "tests": tests})
    return {**manifest(corpus, agent_version, "deterministic_backend"), "rows": rows,
            "tests_run": runner.result.testsRun, "test_failures": failures,
            "duration_ms": round((perf_counter() - started) * 1000, 3),
            "skipped": len(runner.result.skipped), "external_connection_attempts": len(attempts),
            "complete": not failures and runner.result.testsRun == len(labels) and all(row["passed"] for row in rows),
            "note": "Proveedores simulados. El área orienta el diagnóstico; no atribuye automáticamente la causa raíz a Gemini."}
