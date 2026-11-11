"""Regresión de la infraestructura de evaluación. Ningún resultado es de IA real."""
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from incidents.ai.gemini_provider import GeminiAIProvider
from incidents.evaluation.core import (
    check_value, compare_reports, grade_record, grade_records, load_corpus,
    digest, manifest, probe_message, schema_errors, write_report,
)
from incidents.evaluation.runner import no_external_connections, require_isolation


def response_fixture():
    """Contrato sintético, no una grabación ni un baseline del modelo."""
    data = {name: None for name in GeminiAIProvider.RESPONSE_SCHEMA["required"]}
    data.update(intent="incident_report", unit="Estudio A", area="Producción", location_context="Estudio A",
                asset_category="video_cameras", equipment="Cámara 2", failure_type="Imagen intermitente",
                description="La Cámara 2 pierde imagen intermitentemente.", operational_impact="Detiene pruebas",
                priority="high", update_kind="none", dialogue_action="update",
                field_evidence={"area": "Producción", "equipment": "Cámara 2", "priority": "alta"},
                requested_fields=[], rewrite_clauses=[], dialogue_actions=["update"], operational_updates=[])
    return data


class IncidentEvaluationTests(SimpleTestCase):
    def setUp(self):
        self.corpus = load_corpus()
        self.case = self.corpus["cases"][0]

    def record(self, **changes):
        return {"case_id": self.case["id"], "trial": 1, "source": "synthetic",
                "model": "not-a-live-model", "response": response_fixture(), **changes}

    def report(self, kind="deterministic_backend", trials=1):
        return {**manifest(self.corpus, "test-only", kind), "complete": True,
                "test_failures": 0, "skipped": 0, "external_connection_attempts": 0, "rows": [
            {"case_id": case["id"], "trial": trial, "passed": True,
             "source": "synthetic", "model": "not-a-live-model", "schema_valid": True,
             "interpretation_correct": True, "category": "interpretation_correct",
             "checks": [{"view": "raw", "passed": True}], "manual_review_required": False}
            for case in self.corpus["cases"] if kind != "model_recordings" or case["model_probe"]
            for trial in range(1, trials + 1)]}

    def test_corpus_has_26_cases_and_all_bindings_resolve_to_existing_tests(self):
        self.assertEqual(len(self.corpus["cases"]), 26)
        labels = {label for case in self.corpus["cases"] for label in case["backend_tests"]}
        self.assertEqual(len(labels), 48)
        for label in labels:
            with self.subTest(label=label):
                suite = unittest.defaultTestLoader.loadTestsFromName(label)
                self.assertEqual(suite.countTestCases(), 1)
                test = list(suite)[0]
                self.assertEqual(test.id(), label)
                self.assertFalse(getattr(test, "__unittest_skip__", False))

    def test_corpus_covers_domains_sources_and_both_allowed_and_prohibited_operations(self):
        self.assertEqual({c["category"] for c in self.corpus["cases"]},
                         {"interpretation", "validation", "context", "confirmation", "persistence", "notification", "recovery"})
        for case in self.corpus["cases"]:
            self.assertTrue(case["allowed"] and case["forbidden"])
            self.assertIn("sintética", case["input_origin"])
            for evidence in case["evidence"]:
                if evidence["kind"] == "documented_historical_defect":
                    # Los informes QA históricos están ignorados por Git. Son
                    # referencias de procedencia, no fixtures exigidos en un clon.
                    self.assertTrue(evidence["reference"].startswith("docs/qa/"))

    def test_schema_subset_handles_every_keyword_and_type_in_real_contract(self):
        def visit(schema):
            self.assertLessEqual(set(schema), {"type", "enum", "properties", "required", "items", "description"})
            types = schema.get("type", [])
            types = [types] if isinstance(types, str) else types
            self.assertLessEqual(set(types), {"null", "string", "boolean", "object", "array"})
            for child in schema.get("properties", {}).values():
                visit(child)
            if "items" in schema:
                visit(schema["items"])
        visit(GeminiAIProvider.RESPONSE_SCHEMA)
        self.assertEqual(schema_errors(response_fixture()), [])

    def test_correct_synthetic_interpretation_is_graded_without_client_or_network(self):
        with no_external_connections() as attempts, patch("incidents.ai.gemini_provider.genai.Client", side_effect=AssertionError("No cliente")):
            result = grade_record(self.case, self.record())
        self.assertFalse(result["passed"])
        self.assertEqual(result["category"], "manual_review_required")
        self.assertIsNone(result["interpretation_correct"])
        self.assertEqual(attempts, [])

    def test_wrong_raw_priority_is_not_hidden_by_normalization(self):
        record = self.record()
        record["response"]["priority"] = "alta"
        result = grade_record(self.case, record)
        self.assertTrue(result["schema_valid"])
        self.assertFalse(result["interpretation_correct"])

    def test_invented_evidence_is_wrong_even_if_production_parser_filters_it(self):
        record = self.record()
        record["response"]["field_evidence"] = {"equipment": "cita ausente del mensaje"}
        result = grade_record(self.case, record)
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"][0]["passed"])

    def test_malformed_json_missing_fields_types_and_enums_are_schema_errors(self):
        for response in ("{malformed", [], {}, {**response_fixture(), "priority": 2},
                         {**response_fixture(), "intent": "approve"},
                         {**response_fixture(), "authorization": {"kind": "confirm", "direct": "true"}}):
            with self.subTest(response=response):
                result = grade_record(self.case, self.record(response=response))
                self.assertEqual(result["category"], "schema_error")
                self.assertIsNone(result["interpretation_correct"])

    def test_invalid_operational_state_uses_real_parser(self):
        record = self.record()
        record["response"]["operational_updates"] = [{"facet": "backup", "text": "Respaldo reparado",
            "evidence": "Cámara 2", "state": "repaired", "backup_state": None}]
        self.assertEqual(schema_errors(record["response"]), [])
        self.assertEqual(grade_record(self.case, record)["category"], "schema_error")

    def test_provider_errors_are_not_reported_as_semantic_failures(self):
        for code, category in ((500, "provider_http_500"), (503, "provider_http_503"), (429, "provider_http_429"),
                               (401, "provider_authentication"), (403, "provider_authorization"), (None, "provider_error")):
            with self.subTest(code=code):
                result = grade_record(self.case, self.record(error={"http_status": code}))
                self.assertEqual(result["category"], category)
                self.assertIsNone(result["interpretation_correct"])

    def test_rubric_does_not_score_opposite_texts_by_word_overlap(self):
        self.assertFalse(check_value("No confirmo", {"op": "eq", "value": "confirmo"}))
        self.assertFalse(check_value("Podríamos usar respaldo", {"op": "eq", "value": "Respaldo en uso"}))
        self.assertTrue(check_value(["update", "query"], {"op": "includes", "value": ["query"]}))
        self.assertTrue(check_value([{"facet": "backup", "state": "available"}],
                                    {"op": "has_item", "value": {"facet": "backup", "state": "available"}}))

    def test_unknown_duplicate_or_invalid_trials_are_rejected(self):
        for records in ([self.record(case_id="missing")], [self.record(), self.record()],
                        [self.record(trial=True)], [self.record(trial=0)], [self.record(source="unspecified")]):
            with self.subTest(records=records), self.assertRaises(ValueError):
                grade_records(records, self.corpus, "test")

    def test_partial_model_report_explicitly_reports_missing_coverage(self):
        report = grade_records([self.record()], self.corpus, "test")
        self.assertFalse(report["complete"])
        self.assertFalse(report["sampling_complete"])
        self.assertEqual(len(report["missing_cases"]), 15)
        self.assertEqual(report["metrics"]["interpretation_correct"], 0)
        self.assertEqual(report["metrics"]["manual_review_pending"], 1)
        self.assertIsNone(report["metrics"]["rubric_accuracy_on_valid_responses"])
        self.assertEqual(report["rows"][0]["source"], "synthetic")

    def test_wrong_interpretation_and_correct_fields_are_separately_detectable(self):
        case = self.corpus["cases"][7]
        response = {**response_fixture(), "equipment": "Cámara 4", "location_context": "Estudio 8", "priority": "critical",
                    "field_evidence": {"equipment": "Cámara 4", "location_context": "Estudio 8", "priority": "alta"}}
        self.assertEqual(grade_record(case, self.record(response=response))["category"], "interpretation_incorrect")
        response["priority"] = "high"
        self.assertEqual(grade_record(case, self.record(response=response))["category"], "manual_review_required")

    def test_record_requires_exactly_response_or_error_and_finite_duration(self):
        for record in (self.record(error={"http_status": 503}), self.record(duration_ms=float("nan")),
                       self.record(duration_ms=-1), self.record(model=True)):
            with self.subTest(record=record), self.assertRaises(ValueError):
                grade_records([record], self.corpus, "test")
        record = self.record(error={"http_status": 503})
        del record["response"]
        result = grade_records([record], self.corpus, "test")
        self.assertEqual(result["metrics"]["provider_errors"], 1)
        self.assertIsNone(result["metrics"]["rubric_accuracy_on_valid_responses"])

    def test_all_rubric_paths_exist_and_every_model_probe_can_be_graded(self):
        for case in self.corpus["cases"]:
            if case["model_probe"]:
                with self.subTest(case_id=case["id"]):
                    result = grade_record(case, self.record(response=response_fixture()))
                    self.assertTrue(result["checks"])

    def test_backend_comparison_passes_when_all_cases_pass(self):
        report = self.report()
        self.assertEqual(compare_reports(report, deepcopy(report), self.corpus)["verdict"], "PASS")

    def test_unchanged_critical_failure_still_blocks(self):
        report = self.report()
        report["rows"][-1]["passed"] = False
        result = compare_reports(report, deepcopy(report), self.corpus)
        self.assertEqual(result["verdict"], "BLOCK")
        self.assertIn("INC-EVAL-26", result["critical_failures"])

    def test_new_failure_is_named_as_regression(self):
        baseline = self.report()
        candidate = deepcopy(baseline)
        candidate["rows"][0]["passed"] = False
        self.assertIn("INC-EVAL-01", compare_reports(baseline, candidate, self.corpus)["regressions"])

    def test_global_failures_skips_or_network_attempts_block_even_with_passing_case_rows(self):
        baseline = self.report()
        for key, value in (("complete", False), ("test_failures", 1), ("skipped", 1), ("external_connection_attempts", 1)):
            with self.subTest(key=key):
                candidate = deepcopy(baseline)
                candidate[key] = value
                self.assertEqual(compare_reports(baseline, candidate, self.corpus)["verdict"], "BLOCK")

    def test_missing_extra_or_duplicate_cases_block_comparison(self):
        baseline = self.report()
        candidate = deepcopy(baseline)
        candidate["rows"].pop()
        self.assertEqual(compare_reports(baseline, candidate, self.corpus)["verdict"], "BLOCK")
        candidate = deepcopy(baseline)
        candidate["rows"].append(deepcopy(candidate["rows"][0]))
        self.assertEqual(compare_reports(baseline, candidate, self.corpus)["verdict"], "BLOCK")
        candidate["rows"][-1]["case_id"] = "unknown"
        self.assertEqual(compare_reports(baseline, candidate, self.corpus)["verdict"], "BLOCK")

    def test_model_comparison_requires_three_unique_paired_trials(self):
        report = self.report("model_recordings", trials=1)
        self.assertEqual(compare_reports(report, deepcopy(report), self.corpus)["verdict"], "BLOCK")
        report = self.report("model_recordings", trials=3)
        self.assertEqual(compare_reports(report, deepcopy(report), self.corpus)["verdict"], "PASS")
        candidate = deepcopy(report)
        candidate["rows"][0]["trial"] = 4
        self.assertEqual(compare_reports(report, candidate, self.corpus)["verdict"], "BLOCK")

    def test_incompatible_manifest_or_mixed_model_provenance_blocks(self):
        baseline = self.report("model_recordings", trials=3)
        for key in ("kind", "corpus_sha256", "contract_sha256", "evaluation_sha256", "backend_tests_sha256"):
            candidate = deepcopy(baseline)
            candidate[key] = "different"
            self.assertEqual(compare_reports(baseline, candidate, self.corpus)["verdict"], "BLOCK")
        candidate = deepcopy(baseline)
        candidate["rows"][0]["source"] = "gemini_recording"
        self.assertEqual(compare_reports(baseline, candidate, self.corpus)["verdict"], "BLOCK")

    def test_network_attempt_is_blocked_and_remains_recorded_if_caught(self):
        with no_external_connections() as attempts:
            with self.assertRaises(AssertionError):
                socket.create_connection(("example.invalid", 443))
        self.assertEqual(len(attempts), 1)

    def test_report_writer_never_overwrites_existing_evidence(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            write_report(path, {"passed": True})
            with self.assertRaises(FileExistsError):
                write_report(path, {"passed": False})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"passed": True})

    def test_command_rejects_unisolated_settings_before_running_or_opening_db(self):
        with patch("incidents.evaluation.runner.settings", SimpleNamespace(SETTINGS_MODULE="config.settings", DATABASES={"default": {}})), \
                patch("incidents.management.commands.evaluate_incidents.run_backend") as run:
            with self.assertRaises(CommandError):
                call_command("evaluate_incidents", output=Path("unused.json"), agent_version="test", stdout=StringIO())
            run.assert_not_called()

    def test_isolation_accepts_only_dedicated_test_configuration(self):
        config = SimpleNamespace(SETTINGS_MODULE="config.incident_evaluation", INCIDENT_EVALUATION_ONLY=True,
            DATABASES={"default": {"ENGINE": "django.db.backends.postgresql", "NAME": "secuoya_evaluation_unused",
                                   "TEST": {"NAME": "test_secuoya_incident_evaluation"}}},
            EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", TELEGRAM_BOT_TOKEN="", GEMINI_API_KEY="",
            AI_PROVIDER="mock", CONTENT_COORDINATION_ENABLED=False)
        with patch("incidents.evaluation.runner.settings", config):
            require_isolation()
            for name, value in (("TELEGRAM_BOT_TOKEN", "fake-token"), ("GEMINI_API_KEY", "fake-key"),
                                ("CONTENT_COORDINATION_ENABLED", True), ("AI_PROVIDER", "gemini")):
                original = getattr(config, name)
                setattr(config, name, value)
                with self.assertRaises(ValueError):
                    require_isolation()
                setattr(config, name, original)

    def reviews(self, case, record, passed=True):
        return [{"case_id": case["id"], "trial": record["trial"], "check_id": check["id"],
                 "response_sha256": digest(record["response"]), "reviewer": "QA-test-only",
                 "note": "Adjudicación sintética para verificar el mecanismo, sin modelo real.", "passed": passed}
                for check in case["model_probe"]["checks"] if check["op"] == "manual"]

    def capture_record(self, **changes):
        capture = {**{k: manifest(self.corpus, "baseline", "model_recordings")[k]
                      for k in ("corpus_sha256", "contract_sha256", "agent_sha256")},
                   "input_sha256": digest({"message": probe_message(self.case), "previous_data": self.case["model_probe"]["previous_data"]}),
                   "request_profile_sha256": "a" * 64, "agent_version": "baseline",
                   "run_id": "synthetic-capture-test", "sdk_version": "test-only"}
        return self.record(source="gemini_recording", capture={**capture, **changes})

    def test_contexts_are_coherent_and_group_uses_both_messages(self):
        for i in (7, 17, 19):
            previous = self.corpus["cases"][i]["model_probe"]["previous_data"]
            self.assertIn(previous["equipment"], previous["description"])
            self.assertEqual(previous["unit"], previous["location_context"])
        console = self.corpus["cases"][16]["model_probe"]["previous_data"]
        self.assertIsNone(console["description"])
        self.assertIsNone(console["failure_type"])
        group = self.corpus["cases"][18]
        self.assertEqual(group["model_probe"]["previous_data"], {})
        text = probe_message(group)
        self.assertEqual(text, "[Secuencia ordenada de mensajes del mismo reportante]\n" +
                         "\n\n".join(f"Mensaje {i}:\n{v}" for i, v in enumerate(group["messages"], 1)))
        self.assertLess(text.index("Prioridad alta"), text.index("prioridad: crítica"))

    def test_equivalent_entity_case_accents_spaces_and_clause_punctuation(self):
        self.assertTrue(check_value("  CÁMARA   2 ", {"op": "text_eq", "value": "Cámara 2"}))
        self.assertFalse(check_value("Cámara 12", {"op": "text_eq", "value": "Cámara 2"}))
        self.assertTrue(check_value(["Se sospecha del cable, sin confirmar.", "La cámara muestra rayas"],
                                   {"op": "clauses_equal", "value": ["La cámara muestra rayas.", "Se sospecha del cable, sin confirmar"]}))
        self.assertFalse(check_value(["El cable está averiado", "La cámara muestra rayas"],
                                    {"op": "clauses_equal", "value": ["La cámara muestra rayas.", "Se sospecha del cable, sin confirmar"]}))

    def test_empty_extraction_cannot_pass_only_by_leaving_priority_null(self):
        case = self.corpus["cases"][4]
        response = response_fixture()
        for name in ("area", "unit", "location_context", "equipment", "failure_type", "asset_category", "priority"):
            response[name] = None
        response["field_evidence"] = {}
        result = grade_record(case, self.record(response=response))
        self.assertFalse(result["interpretation_correct"])

    def test_missing_required_literal_evidence_does_not_get_credit(self):
        record = self.record()
        record["response"]["field_evidence"] = {}
        self.assertEqual(grade_record(self.case, record)["category"], "interpretation_incorrect")

    def test_correction_authorization_with_pending_changes_is_not_unconditional_permission(self):
        case = self.corpus["cases"][1]
        response = {**response_fixture(), "priority": "low", "field_evidence": {"priority": "baja"}}
        response["authorization"] = dict(kind="confirm", direct=True, conditional=False, delayed=False,
            third_party=False, requires_changes=True, equipment=None, location_context=None, evidence="Sí")
        result = grade_record(case, self.record(response=response))
        self.assertTrue(next(c for c in result["checks"] if c["path"] == "no_unconditional_creation_authorization")["passed"])
        response["authorization"]["requires_changes"] = False
        self.assertFalse(grade_record(case, self.record(response=response))["interpretation_correct"])

    def test_semantically_equivalent_symptom_requires_review_without_false_auto_failure(self):
        case = self.corpus["cases"][17]
        response = {**response_fixture(), "equipment": "Cámara 3", "failure_type": "La imagen desaparece unos segundos y vuelve",
                    "field_evidence": {"failure_type": "la imagen se va a negro por unos segundos y después vuelve"}}
        result = grade_record(case, self.record(response=response))
        self.assertEqual(result["category"], "manual_review_required")
        self.assertIsNone(result["interpretation_correct"])

    def test_manual_reviews_are_bound_to_response_and_cannot_hide_failed_automatic_checks(self):
        record = self.record()
        reviews = self.reviews(self.case, record)
        report = grade_records([record], self.corpus, "test", reviews=reviews)
        self.assertTrue(report["rows"][0]["passed"])
        self.assertEqual(report["metrics"]["manual_review_pending"], 0)
        self.assertFalse(grade_records([record], self.corpus, "test", reviews=self.reviews(self.case, record, passed=False))["rows"][0]["passed"])
        record["response"]["priority"] = "critical"
        with self.assertRaises(ValueError):
            grade_records([record], self.corpus, "test", reviews=reviews)
        report = grade_records([record], self.corpus, "test", reviews=self.reviews(self.case, record))
        self.assertFalse(report["rows"][0]["passed"])

    def test_duplicate_unknown_or_unexplained_human_review_is_rejected(self):
        record = self.record()
        review = self.reviews(self.case, record)[0]
        for reviews in ([review, review], [{**review, "check_id": "unknown"}], [{**review, "note": ""}], [{**review, "passed": "true"}]):
            with self.subTest(reviews=reviews), self.assertRaises(ValueError):
                grade_records([record], self.corpus, "test", reviews=reviews)

    def test_parser_preservation_failure_is_not_automatically_a_model_error(self):
        case = self.corpus["cases"][10]
        record = self.record(case_id=case["id"])
        record["response"].update(dialogue_actions=["update", "query"], requested_fields=["operational_impact"], field_evidence={})
        with patch("incidents.evaluation.core.normalized_response", return_value={"description": "MUTATED IN TEST"}):
            report = grade_records([record], self.corpus, "test", reviews=self.reviews(case, record))
        result = report["rows"][0]
        self.assertEqual(result["category"], "normalization_mismatch")
        self.assertTrue(result["interpretation_correct"])
        self.assertFalse(result["passed"])

    def test_real_recording_requires_frozen_capture_provenance_without_relabelling(self):
        with self.assertRaises(ValueError):
            grade_records([self.record(source="gemini_recording")], self.corpus, "baseline")
        record = self.capture_record()
        report = grade_records([record], self.corpus, "baseline")
        self.assertEqual(report["agent_sha256"], record["capture"]["agent_sha256"])
        for name in ("corpus_sha256", "contract_sha256", "input_sha256", "agent_version"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                grade_records([self.capture_record(**{name: "stale"})], self.corpus, "baseline")

    def test_mixed_models_sources_or_capture_profiles_are_rejected(self):
        for rows in ([self.record(), self.record(trial=2, model="another")],
                     [self.capture_record(), {**self.capture_record(request_profile_sha256="b" * 64), "trial": 2}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                grade_records(rows, self.corpus, "baseline")

    def test_comparator_blocks_fabricated_success_review_pending_or_parameter_drift(self):
        baseline = self.report("model_recordings", trials=3)
        for changes in ({"category": "provider_http_503"}, {"schema_valid": False}, {"manual_review_required": True}, {"checks": []}):
            candidate = deepcopy(baseline)
            candidate["rows"][0].update(changes)
            self.assertEqual(compare_reports(baseline, candidate, self.corpus)["verdict"], "BLOCK")
        candidate = deepcopy(baseline)
        candidate["request_profile_sha256"] = "different"
        self.assertEqual(compare_reports(baseline, candidate, self.corpus)["verdict"], "BLOCK")

    def test_comparator_does_not_call_pending_reviews_or_outages_semantic_regressions(self):
        baseline = self.report("model_recordings", trials=3)
        for category, field in (("manual_review_required", "pending_reviews"), ("provider_http_503", "provider_failures")):
            with self.subTest(category=category):
                candidate = deepcopy(baseline)
                candidate["rows"][0].update(passed=False, category=category, interpretation_correct=None)
                result = compare_reports(baseline, candidate, self.corpus)
                self.assertEqual(result["verdict"], "BLOCK")
                self.assertEqual(result["critical_failures"], [])
                self.assertEqual(result["regressions"], [])
                self.assertEqual(result[field], ["INC-EVAL-01"])

    def test_error_presence_not_truthiness_and_nonstandard_json_are_rejected(self):
        for record in (self.record(error={}), self.record(error=None), {**self.record(), "error": False},
                       {"case_id": "INC-EVAL-01", "trial": 1, "source": "synthetic", "model": "test", "error": {}}, []):
            with self.subTest(record=record), self.assertRaises(ValueError):
                grade_records([record], self.corpus, "test")
        self.assertEqual(grade_record(self.case, self.record(response='{"intent":NaN}'))["category"], "schema_error")

    def test_offline_cli_preserves_pending_review_and_applies_sidecar_without_network(self):
        from incidents.evaluation.__main__ import main
        with TemporaryDirectory() as folder, no_external_connections() as attempts:
            folder = Path(folder)
            source, preliminary, reviewed, reviews = (folder / name for name in ("source.jsonl", "preliminary.json", "reviewed.json", "reviews.jsonl"))
            record = self.record()
            source.write_text(json.dumps(record) + "\n", encoding="utf-8")
            reviews.write_text("\n".join(json.dumps(r) for r in self.reviews(self.case, record)), encoding="utf-8")
            argv = ["evaluation", "grade", "--input", str(source), "--agent-version", "test"]
            with patch("sys.argv", argv + ["--output", str(preliminary)]), patch("sys.stdout", StringIO()):
                self.assertEqual(main(), 2)  # Solo una de 16 sondas, revisión pendiente.
            with patch("sys.argv", argv + ["--output", str(reviewed), "--reviews", str(reviews)]), patch("sys.stdout", StringIO()):
                self.assertEqual(main(), 2)  # Una sonda adjudicada no completa el corpus.
            self.assertEqual(json.loads(preliminary.read_text(encoding="utf-8"))["rows"][0]["category"], "manual_review_required")
            self.assertTrue(json.loads(reviewed.read_text(encoding="utf-8"))["rows"][0]["passed"])
            self.assertEqual(attempts, [])
