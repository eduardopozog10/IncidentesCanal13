"""Simulación acotada; nunca crea clientes de proveedores externos."""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from incidents.ai.base import AIProvider
from incidents.ai.gemini_provider import GeminiAIProvider
from incidents.ai.schemas import IncidentExtractionResult
from incidents.messaging.telegram_update_handler import TelegramUpdateHandler
from incidents.models import ChannelMessage, Incident, IncidentChange, IncidentDraft, IncidentNotification, Technician, TechnicianChannel
from incidents.services.incident_processor import IncidentProcessor
from incidents.services.incoming_message_service import IncomingMessageService
from incidents.test_pilot_recovery import update
from incidents.test_user_management import SETTINGS, account, initialize
from scripts.qa_adversarial_cases import BASE, cases


def evidence(name, value):
    if name == "priority": return {"critical":"crítica", "high":"alta", "medium":"media", "low":"baja"}[value]
    return value


def write_result(name, data):
    if os.environ.get("SECUOYA_QA_EVIDENCE") == "1":
        folder = Path("docs/qa/evidencia")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def handler_fixture(user=101):
    reporter = Technician.objects.create(full_name=f"QA ficticio {user}")
    TechnicianChannel.objects.create(technician=reporter, provider="telegram", external_user_id=str(user), external_chat_id=str(user))
    ai = Mock(spec=AIProvider)
    ai.extract_incident.return_value = IncidentExtractionResult(**deepcopy(BASE))
    messaging = Mock()
    counter = iter(range(10000, 100000))
    messaging.send_text.side_effect = lambda recipient, text: {"ok":True, "result":{"message_id":next(counter)}}
    handler = TelegramUpdateHandler(messaging_provider=messaging,
        incoming_service=IncomingMessageService(IncidentProcessor(ai, require_delivered_summary=True)))
    return reporter, ai, messaging, handler


@override_settings(DEBUG=False, TELEGRAM_AUTO_REGISTER=False, PILOT_MESSAGE_RETRY_SECONDS=0, **SETTINGS)
class AdversarialConversationTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.results = []

    @classmethod
    def tearDownClass(cls):
        write_result("conversaciones.json", cls.results)
        super().tearDownClass()

    def execute_case(self, case):
        reporter, ai, messaging, handler = handler_fixture()
        transcript = []
        row = {"id":case["id"], "category":case["kind"], "mode":"contrato de extracción simulado",
               "expected":case, "observed":transcript, "status":"FAIL"}
        self.results.append(row)
        def send(text):
            response = handler.handle(update(len(transcript)+1, text=text))
            draft = IncidentDraft.objects.filter(technician=reporter).order_by("pk").last()
            transcript.append({"input":text, "action":response["action"], "reply":messaging.send_text.call_args.args[1] if messaging.send_text.called else None,
                "draft_status":draft.status if draft else None, "data":deepcopy(draft.extracted_data) if draft else None,
                "incidents":Incident.objects.filter(technician=reporter).count()})
            return response
        if case["kind"] in {"security", "conversation"}:
            ai.extract_incident.return_value = IncidentExtractionResult(intent="conversation", conversation_reply="Puedo ayudarte a registrar incidencias. Revisaremos un resumen antes de confirmar.")
            send(case["text"])
            self.assertFalse(Incident.objects.exists())
            self.assertFalse(IncidentDraft.objects.exists())
        else:
            if case["kind"] in {"correction", "long"}:
                send("La Cámara 3 tiene imagen intermitente en Estudio 2. Prioridad alta.")
            if case["kind"] == "long":
                for turn in range(case["turns"]):
                    # Consulta/agradecimiento durante un borrador: no debe destruirlo
                    # ni reenviar su contenido completo al proveedor en cada turno.
                    send("gracias" if turn % 2 else "hola")
                    self.assertFalse(Incident.objects.exists())
                expected = BASE
                self.assertEqual(ai.extract_incident.call_count, 1)
            else:
                kwargs = dict(deepcopy(case["data"]))
                if case["kind"] == "correction":
                    kwargs.update(update_kind="correction", field_evidence={k:evidence(k,v) for k,v in case["changed"].items()})
                ai.extract_incident.return_value = IncidentExtractionResult(**kwargs)
                send(case["text"])
                expected = case["data"]
            self.assertFalse(Incident.objects.exists())
            draft = IncidentDraft.objects.get(technician=reporter)
            if case["kind"] in {"report", "correction", "long"}:
                self.assertEqual(draft.status, "ready", transcript[-1])
                fields = case.get("changed", expected)
                for name in fields:
                    self.assertEqual(draft.extracted_data.get(name), expected[name], (case["id"], name, transcript[-1]))
                send("confirmo")
                incident = Incident.objects.get(technician=reporter)
                for name in fields:
                    self.assertEqual(getattr(incident, name), expected[name])
                send("confirmo")
                self.assertEqual(Incident.objects.filter(technician=reporter).count(), 1)
                self.assertEqual(IncidentNotification.objects.filter(incident=incident).count(), 1)
                self.assertFalse(IncidentChange.objects.filter(incident=incident).exists())
            else:
                self.assertEqual(draft.status, "collecting")
                self.assertTrue(draft.missing_fields)
                send("confirmo")
                self.assertFalse(Incident.objects.exists())
        row["status"] = "PASS"
        row["rubric_contract_only"] = {
            "comprension_intencion":None, "extraccion_campos":5 if case.get("data") else None,
            "contexto":5 if case["kind"] in {"long","correction"} else None,
            "correcciones":5 if case["kind"] == "correction" else None,
            "naturalidad":None, "claridad":None, "no_registro_inventado":5,
            "preguntas_aclaratorias":5 if case["kind"] in {"incomplete","ambiguous"} else None,
        }


for spec in cases():
    def scenario(self, case=spec): self.execute_case(case)
    setattr(AdversarialConversationTests, "test_" + spec["id"].replace("-", "_"), scenario)


@override_settings(DEBUG=False, TELEGRAM_AUTO_REGISTER=False, **SETTINGS)
class LocalLoadTests(TestCase):
    def run_load(self, count):
        reporter, ai, messaging, handler = handler_fixture()
        latencies = []
        start = time.perf_counter()
        completed = 0
        for number in range(1, count+1):
            if time.perf_counter() - start > 120: break
            tick = time.perf_counter()
            response = handler.handle(update(number, text="/start"))
            self.assertEqual(response["action"], "welcome_sent")
            latencies.append((time.perf_counter()-tick)*1000)
            completed += 1
        elapsed = time.perf_counter()-start
        sorted_ms = sorted(latencies)
        def percentile(p): return sorted_ms[max(0, int((len(sorted_ms)-1)*p))]
        sends = messaging.send_text.call_count
        duplicate = handler.handle(update(1, text="/start"))
        metrics = dict(requested=count, completed=completed, seconds=elapsed, throughput=completed/elapsed,
            p50_ms=percentile(.50), p95_ms=percentile(.95), p99_ms=percentile(.99),
            inbound=ChannelMessage.objects.filter(direction="inbound").count(), outbound=ChannelMessage.objects.filter(direction="outbound").count(),
            failed=ChannelMessage.objects.filter(status="failed").count(), duplicate_detected=duplicate["action"]=="duplicate_message",
            lost=completed-ChannelMessage.objects.filter(direction="inbound", status="processed").count(), incidents=Incident.objects.count(),
            mode="Polling simulado, PostgreSQL real temporal, /start; NO rendimiento de Gemini ni de reportes completos")
        write_result(f"carga-{count}.json", metrics)
        print(f"QA carga {count}: {json.dumps(metrics)}", flush=True)
        self.assertEqual(completed, count, "Límite local de 120 s alcanzado; volumen incompleto")
        self.assertEqual(metrics["lost"], 0)
        self.assertEqual(metrics["failed"], 0)
        self.assertEqual(metrics["inbound"], count)
        self.assertEqual(metrics["outbound"], count)
        self.assertEqual(messaging.send_text.call_count, sends)
        ai.extract_incident.assert_not_called()

    def test_load_0010(self): self.run_load(10)
    def test_load_0100(self): self.run_load(100)
    def test_load_0500(self): self.run_load(500)
    def test_load_1000(self): self.run_load(1000)


@override_settings(DEBUG=False, TELEGRAM_AUTO_REGISTER=False, PILOT_MESSAGE_RETRY_SECONDS=0, **SETTINGS)
class PollingConcurrencyAuditTests(TransactionTestCase):
    def test_eight_reporters_complete_independently_with_four_workers(self):
        fixtures = [handler_fixture(user=900+i) for i in range(8)]
        def run(pair):
            user, (_, ai, messaging, handler) = pair
            close_old_connections()
            try:
                handler.handle(update(1, user=user, text="La Cámara 3 falla. Prioridad alta."))
                handler.handle(update(2, user=user, text="confirmo"))
                handler.handle(update(2, user=user, text="confirmo"))
            finally: close_old_connections()
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(run, pair) for pair in zip(range(900,908), fixtures)]
            for future in futures: future.result(timeout=35)
        self.assertEqual(Incident.objects.count(), 8)
        self.assertEqual(IncidentNotification.objects.count(), 8)
        self.assertEqual(len(set(Incident.objects.values_list("pk", flat=True))), 8)
        self.assertEqual(ChannelMessage.objects.filter(direction="inbound").count(), 16)


@override_settings(**SETTINGS)
class DashboardVolumeAuditTests(TestCase):
    def test_two_thousand_incidents_and_three_hundred_history_events_are_paginated(self):
        initialize()
        user = account("Supervisor", "qa-volume")
        self.client.force_login(user)
        reporter = Technician.objects.create(full_name="Volumen QA")
        Incident.objects.bulk_create([Incident(technician=reporter, **BASE, original_message="QA volumen") for _ in range(2000)])
        incident = Incident.objects.first()
        IncidentChange.objects.bulk_create([IncidentChange(incident=incident, actor_label="Sistema QA", action="status_changed",
            field="status", old_value="reported", new_value="in_progress", source="internal") for _ in range(300)])
        records = []
        for url in ("/api/v1/incidents/?search=Cámara", "/api/v1/incidents/summary/", "/api/v1/incidents/analytics/",
                    f"/api/v1/incidents/{incident.pk}/history/?page=1", f"/api/v1/incidents/{incident.pk}/history/?page=15"):
            tick = time.perf_counter()
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                data = response.json()
            records.append(dict(url=url, milliseconds=(time.perf_counter()-tick)*1000, queries=len(queries), response_bytes=len(response.content)))
            if "results" in data:
                self.assertEqual(len(data["results"]), 20)
                self.assertLess(len(queries), 25)
                self.assertEqual(data["count"], 300 if "/history/" in url else 2000)
            else:
                self.assertEqual(data.get("total", data.get("metrics", {}).get("total")), 2000)
        write_result("dashboard-volumen.json", records)


class GeminiMalformedContractTests(TestCase):
    def test_malformed_json_types_and_enums_are_rejected_without_network(self):
        provider = object.__new__(GeminiAIProvider)
        provider.model = "qa-no-network"
        values = ["", "{", "[]", "null", "123", json.dumps({"intent":"admin"}),
                  json.dumps({"intent":"incident_report", "area":[]}),
                  json.dumps({"intent":"incident_report", "field_evidence":[]}),
                  json.dumps({"intent":"incident_report", "priority":True})]
        for value in values:
            with self.subTest(value=value), patch.object(provider, "_create_interaction_with_retry", return_value=SimpleNamespace(output_text=value)):
                with self.assertRaises(ValueError): provider.extract_incident("Datos no confiables QA")

    def test_fabricated_evidence_is_not_accepted(self):
        provider = object.__new__(GeminiAIProvider); provider.model = "qa-no-network"
        output = {**BASE, "intent":"incident_report", "field_evidence":{"equipment":"Equipo inventado"}}
        with patch.object(provider, "_create_interaction_with_retry", return_value=SimpleNamespace(output_text=json.dumps(output))):
            result = provider.extract_incident("El reporte no identifica equipo")
        self.assertEqual(result.field_evidence, {})
