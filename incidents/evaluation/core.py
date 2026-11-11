"""Corpus, rúbricas y comparación offline. Sin clientes, SQL ni llamadas de IA."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unicodedata

from incidents.ai.gemini_provider import GeminiAIProvider


ROOT = Path(__file__).resolve().parents[2]
CORPUS = Path(__file__).with_name("corpus.json")
CATEGORIES = {
    "interpretation", "validation", "context", "confirmation", "persistence",
    "notification", "recovery",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load_corpus():
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    cases = corpus["cases"]
    if corpus["version"] != 2 or not 20 <= len(cases) <= 30:
        raise ValueError("Versión o tamaño de corpus inválido")
    ids = [case["id"] for case in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("IDs de corpus duplicados")
    for case in cases:
        for key in ("objective", "messages", "initial_context", "expected", "allowed",
                    "forbidden", "criticality", "evidence", "backend_tests", "input_origin"):
            if not case.get(key):
                raise ValueError(f"{case['id']}: falta {key}")
        if case["category"] not in CATEGORIES or case["criticality"] not in {"critical", "high", "medium", "low"}:
            raise ValueError(f"{case['id']}: clasificación inválida")
        if any(not label.startswith("incidents.test") or ".test_" not in label for label in case["backend_tests"]):
            raise ValueError("Solo se admiten regresiones de Incidencias")
        probe = case.get("model_probe")
        if probe is not None:
            if not isinstance(probe.get("previous_data"), dict) or not probe.get("checks"):
                raise ValueError(f"{case['id']}: sonda incompleta")
            probe_message(case)
            for check in probe["checks"]:
                if check["view"] not in {"raw", "normalized"} or check["op"] not in {"eq", "one_of", "contains", "not_contains", "includes", "has_item", "text_eq", "text_one_of", "nonempty", "clauses_equal", "manual"}:
                    raise ValueError("Rúbrica desconocida")
                if check["op"] == "manual" and (not check.get("id") or check["view"] != "raw"):
                    raise ValueError("Revisión humana sin ID o fuera de la interpretación cruda")
    return corpus


def probe_message(case):
    probe = case["model_probe"]
    indices = probe.get("message_indices", [probe.get("message_index")])
    if (not indices or any(type(i) is not int or not 0 <= i < len(case["messages"]) for i in indices)
            or indices != sorted(set(indices))):
        raise ValueError("Índices de sonda inválidos")
    texts = [case["messages"][i] for i in indices]
    if len(texts) == 1:
        return texts[0]
    # Mismo envoltorio de TelegramMessageGroups.process_reporter, sin invocar
    # Telegram ni cargar modelos Django. Una prueba comprueba esta correspondencia.
    return "[Secuencia ordenada de mensajes del mismo reportante]\n" + "\n\n".join(
        f"Mensaje {index}:\n{value}" for index, value in enumerate(texts, 1))


def manifest(corpus, agent_version, kind):
    files = sorted(path for folder in ("ai", "services", "messaging")
                   for path in (ROOT / "incidents" / folder).glob("*.py"))
    harness = sorted(Path(__file__).parent.glob("*.py")) + [
        ROOT / "config/incident_evaluation.py", ROOT / "incidents/test_agent_evaluation.py"]
    regressions = sorted((ROOT / "incidents").glob("test*.py")) + [ROOT / "scripts/qa_adversarial_cases.py"]

    def source_digest(paths):
        return digest({str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})

    return {
        "format_version": 2, "kind": kind, "agent_version": agent_version,
        "corpus_sha256": digest(corpus),
        "contract_sha256": digest(GeminiAIProvider.RESPONSE_SCHEMA),
        "agent_sha256": source_digest(files),
        "evaluation_sha256": source_digest(harness),
        "backend_tests_sha256": source_digest(regressions),
    }


def schema_errors(value, schema=None, path="$"):
    """Subconjunto usado por RESPONSE_SCHEMA: tipos, enum, required e items.

    No es un validador JSON Schema general. La prueba de contrato impide adoptar
    silenciosamente una palabra clave nueva que esta implementación no soporte.
    """
    schema = GeminiAIProvider.RESPONSE_SCHEMA if schema is None else schema
    types = schema.get("type", [])
    types = [types] if isinstance(types, str) else types
    matches = {"null": value is None, "object": isinstance(value, dict),
               "array": isinstance(value, list), "string": isinstance(value, str),
               "boolean": type(value) is bool}
    if types and not any(matches.get(name, False) for name in types):
        return [f"{path}: tipo inválido"]
    errors = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: valor fuera de enum")
    if isinstance(value, dict):
        errors.extend(f"{path}.{name}: requerido" for name in schema.get("required", []) if name not in value)
        for name, spec in schema.get("properties", {}).items():
            if name in value:
                errors.extend(schema_errors(value[name], spec, f"{path}.{name}"))
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors.extend(schema_errors(item, schema["items"], f"{path}[{index}]"))
    return errors


def normalized_response(response, text, previous):
    # Usar el parser REAL sin __init__, SDK, credenciales ni construcción de cliente.
    provider = object.__new__(GeminiAIProvider)
    provider.model = "offline-recording"
    provider._build_prompt = lambda **kwargs: "offline"
    provider._create_interaction_with_retry = lambda **kwargs: SimpleNamespace(output_text=json.dumps(response))
    return provider.extract_incident(text, previous).to_dict()


def at_path(value, path):
    if path == "$":
        return value
    for name in path.split("."):
        if isinstance(value, dict) and name in value:
            value = value[name]
        else:
            raise ValueError(f"Ruta de rúbrica no disponible: {path}")
    return value


def check_value(actual, check):
    expected = check["value"]
    if check["op"] == "eq":
        return type(actual) is type(expected) and actual == expected
    if check["op"] == "one_of":
        return actual in expected
    if check["op"] in {"contains", "not_contains"}:
        found = isinstance(actual, str) and expected.casefold() in actual.casefold()
        return found if check["op"] == "contains" else isinstance(actual, str) and not found
    if check["op"] == "includes":
        return isinstance(actual, list) and all(value in actual for value in expected)
    if check["op"] == "has_item":
        return isinstance(actual, list) and any(isinstance(item, dict) and all(item.get(k) == v for k, v in expected.items()) for item in actual)
    if check["op"] == "nonempty":
        return isinstance(actual, str) and bool(actual.strip())
    if check["op"] == "text_eq":
        return isinstance(actual, str) and canonical_text(actual) == canonical_text(expected)
    if check["op"] == "text_one_of":
        return isinstance(actual, str) and canonical_text(actual) in {canonical_text(v) for v in expected}
    if check["op"] == "clauses_equal":
        # Solo normaliza puntuación final, mayúsculas/acentos/espacios. No quita
        # negaciones, números, palabras ni cláusulas adicionales.
        return isinstance(actual, list) and all(isinstance(v, str) for v in actual) and sorted(
            canonical_text(v).rstrip(".!? ") for v in actual) == sorted(canonical_text(v).rstrip(".!? ") for v in expected)
    if check["op"] == "manual":
        return None
    raise ValueError("Operador de rúbrica desconocido")


def canonical_text(value):
    return " ".join("".join(c for c in unicodedata.normalize("NFD", value.casefold())
                            if unicodedata.category(c) != "Mn").split())


def finalize_checks(result):
    raw = [c for c in result["checks"] if c["view"] == "raw"]
    context = [c for c in result["checks"] if c["view"] == "normalized"]
    pending = any(c["passed"] is None for c in raw)
    wrong = any(c["passed"] is False for c in raw)
    correct = False if wrong else None if pending else True
    context_ok = all(c["passed"] is True for c in context)
    category = ("interpretation_incorrect" if wrong else "manual_review_required" if pending
                else "normalization_mismatch" if not context_ok else "interpretation_correct")
    result.update(passed=correct is True and context_ok, category=category,
                  interpretation_correct=correct, normalization_checks_passed=context_ok,
                  manual_review_required=pending and not wrong)
    return result


def grade_record(case, record):
    probe = case.get("model_probe")
    if probe is None:
        raise ValueError(f"{case['id']}: no requiere una evaluación del modelo")
    if record.get("error"):
        error = record["error"]
        if not isinstance(error, dict):
            raise ValueError("error debe ser un objeto")
        code = error.get("http_status")
        category = {500: "provider_http_500", 503: "provider_http_503", 429: "provider_http_429",
                    401: "provider_authentication", 403: "provider_authorization"}.get(code, "provider_error")
        return {"passed": False, "category": category, "http_status": code,
                "schema_valid": None, "interpretation_correct": None, "checks": []}
    response = record.get("response")
    if isinstance(response, str):
        try:
            def invalid_constant(value):
                raise ValueError("Constante JSON inválida")
            response = json.loads(response, parse_constant=invalid_constant)
        except ValueError:
            return {"passed": False, "category": "schema_error", "schema_valid": False,
                    "interpretation_correct": None, "errors": ["JSON inválido"], "checks": []}
    errors = schema_errors(response)
    if errors:
        return {"passed": False, "category": "schema_error", "schema_valid": False,
                "interpretation_correct": None, "errors": errors, "checks": []}
    try:
        normalized = normalized_response(response, probe_message(case), probe["previous_data"])
    except (TypeError, ValueError) as error:
        return {"passed": False, "category": "schema_error", "schema_valid": False,
                "interpretation_correct": None, "errors": [str(error)], "checks": []}
    checks = []
    text = probe_message(case)
    quotes = list(response.get("field_evidence", {}).values())
    quotes += [item["evidence"] for item in response.get("operational_updates", [])]
    quotes += [response.get("action_evidence")]
    if response.get("authorization"):
        quotes += [response["authorization"].get("evidence")]
    checks.append({"view": "raw", "path": "literal_evidence", "passed": all(
        quote is None or (bool(quote.strip()) and quote.strip() in text) for quote in quotes)})
    for field in probe.get("evidence_fields", []):
        quote = response.get("field_evidence", {}).get(field)
        checks.append({"view": "raw", "path": f"field_evidence.{field}",
                       "passed": isinstance(quote, str) and bool(quote.strip()) and quote.strip() in text})
    auth = response.get("authorization")
    unconditional = isinstance(auth, dict) and auth.get("kind") == "confirm" and auth.get("direct") is True and all(
        auth.get(name) is False for name in ("conditional", "delayed", "third_party", "requires_changes"))
    # Ninguna de las 16 sondas solicita creación incondicional. Un confirm con
    # requires_changes=true es compatible con corregir y no equivale a permiso.
    checks.append({"view": "raw", "path": "no_unconditional_creation_authorization", "passed": not unconditional})
    for check in probe["checks"]:
        actual = at_path(response if check["view"] == "raw" else normalized, check["path"])
        checks.append({"view": check["view"], "path": check["path"], "passed": check_value(actual, check),
                       **({"id": check["id"], "criterion": check["value"]} if check["op"] == "manual" else {})})
    return finalize_checks({"schema_valid": True, "checks": checks})


def grade_records(records, corpus, agent_version, reviews=()):
    cases = {case["id"]: case for case in corpus["cases"]}
    review_index, used_reviews = {}, set()
    for review in reviews:
        if not isinstance(review, dict):
            raise ValueError("Revisión inválida")
        key = (review.get("case_id"), review.get("trial"), review.get("check_id"))
        if (any(not isinstance(v, str) or not v.strip() for v in (key[0], key[2], review.get("reviewer"), review.get("note")))
                or type(key[1]) is not int or key[1] < 1 or type(review.get("passed")) is not bool or key in review_index
                or not isinstance(review.get("response_sha256"), str)):
            raise ValueError("Revisión duplicada o sin identidad, evidencia/nota o decisión booleana")
        review_index[key] = review
    rows, seen = [], set()
    capture_profiles, captured_agents, run_ids = set(), set(), set()
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("case_id"), str):
            raise ValueError("Cada registro debe ser un objeto con case_id de texto")
        key = (record.get("case_id"), record.get("trial"))
        if key[0] not in cases or type(key[1]) is not int or key[1] < 1 or key in seen:
            raise ValueError("Caso desconocido, trial inválido o duplicado")
        if cases[key[0]].get("model_probe") is None:
            raise ValueError("Caso sin sonda de modelo")
        if record.get("source") not in {"synthetic", "gemini_recording"} or not isinstance(record.get("model"), str) or not record["model"].strip():
            raise ValueError("Faltan procedencia o modelo declarados")
        if ("response" in record) == ("error" in record):
            raise ValueError("Especificar exactamente response o error")
        if "error" in record and (not isinstance(record["error"], dict) or not record["error"]
                                  or (record["error"].get("http_status") is not None and type(record["error"].get("http_status")) is not int)):
            raise ValueError("Error de proveedor inválido")
        if record["source"] == "gemini_recording":
            capture = record.get("capture", {})
            expected = {"corpus_sha256": digest(corpus), "contract_sha256": digest(GeminiAIProvider.RESPONSE_SCHEMA),
                        "input_sha256": digest({"message": probe_message(cases[key[0]]),
                                                "previous_data": cases[key[0]]["model_probe"]["previous_data"]}),
                        "agent_version": agent_version}
            if not isinstance(capture, dict) or any(capture.get(k) != v for k, v in expected.items()):
                raise ValueError("La captura real no acredita las entradas/corpus/contrato/versión calificados")
            for name in ("agent_sha256", "request_profile_sha256"):
                value = capture.get(name)
                if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                    raise ValueError("Huella de captura inválida")
            if any(not isinstance(capture.get(name), str) or not capture[name].strip() for name in ("run_id", "sdk_version")):
                raise ValueError("Falta identificación de captura/SDK")
            captured_agents.add(capture["agent_sha256"])
            capture_profiles.add(capture["request_profile_sha256"])
            run_ids.add(capture["run_id"])
        seen.add(key)
        duration = record.get("duration_ms")
        if duration is not None and (type(duration) not in {int, float} or not 0 <= duration < float("inf")):
            raise ValueError("duration_ms inválido")
        result = grade_record(cases[key[0]], record)
        response_hash = digest(record.get("response", record.get("error")))
        for check in result["checks"]:
            if not check.get("id"):
                continue
            review_key = (*key, check["id"])
            review = review_index.get(review_key)
            if review:
                if review["response_sha256"] != response_hash:
                    raise ValueError("La revisión humana corresponde a otra respuesta")
                check.update(passed=review["passed"], reviewer=review["reviewer"], note=review["note"])
                used_reviews.add(review_key)
        if result.get("schema_valid") is True:
            finalize_checks(result)
        rows.append({"case_id": key[0], "trial": key[1], "source": record["source"],
                     "model": record["model"], "duration_ms": duration, "response_sha256": response_hash, **result})
    if set(review_index) != used_reviews:
        raise ValueError("Hay revisiones sin registro/criterio manual aplicable")
    if (len({(row["source"], row["model"]) for row in rows}) > 1
            or len(captured_agents) > 1 or len(capture_profiles) > 1 or len(run_ids) > 1):
        raise ValueError("No mezclar modelos, fuentes, versiones o ejecuciones en un informe")
    report = {**manifest(corpus, agent_version, "model_recordings"), "rows": rows}
    if captured_agents:
        report["graded_with_agent_sha256"] = report["agent_sha256"]
        report["agent_sha256"] = next(iter(captured_agents))
        report["request_profile_sha256"] = next(iter(capture_profiles))
        report["capture_run_id"] = next(iter(run_ids))
    report["expected_cases"] = [case["id"] for case in cases.values() if case.get("model_probe")]
    report["missing_cases"] = sorted(set(report["expected_cases"]) - {row["case_id"] for row in rows})
    report["under_sampled_cases"] = [case_id for case_id in report["expected_cases"]
                                      if sum(row["case_id"] == case_id for row in rows) < 3]
    report["coverage_complete"] = not report["missing_cases"] and bool(rows)
    report["sampling_complete"] = not report["under_sampled_cases"] and bool(rows)
    report["complete"] = report["coverage_complete"] and report["sampling_complete"]
    report["metrics"] = {
        "rows": len(rows), "schema_valid": sum(row["schema_valid"] is True for row in rows),
        "interpretation_correct": sum(row["interpretation_correct"] is True for row in rows),
        "provider_errors": sum(row["category"].startswith("provider_") for row in rows),
        "manual_review_pending": sum(row.get("manual_review_required", False) for row in rows),
    }
    valid = report["metrics"]["schema_valid"]
    responses = len(rows) - report["metrics"]["provider_errors"]
    report["metrics"].update(
        rubric_accuracy_on_valid_responses=(report["metrics"]["interpretation_correct"] / valid
                                            if valid and not report["metrics"]["manual_review_pending"] else None),
        schema_validity_on_responses=valid / responses if responses else None,
    )
    return report


def compare_reports(baseline, candidate, corpus):
    """Puerta conservadora, sin afirmar significancia con una muestra pequeña."""
    reasons = []
    if candidate.get("format_version") != 2 or baseline.get("format_version") != 2:
        reasons.append("Versión de informe desconocida")
    for key in ("format_version", "kind", "corpus_sha256", "contract_sha256", "evaluation_sha256", "backend_tests_sha256"):
        if baseline.get(key) != candidate.get(key):
            reasons.append(f"Manifiestos incompatibles: {key}")
    if candidate.get("corpus_sha256") != digest(corpus):
        reasons.append("El corpus actual no coincide con los resultados")
    if candidate.get("contract_sha256") != digest(GeminiAIProvider.RESPONSE_SCHEMA):
        reasons.append("El contrato actual no coincide con los resultados")
    current = manifest(corpus, "comparison", candidate.get("kind"))
    for key in ("evaluation_sha256", "backend_tests_sha256"):
        if candidate.get(key) != current[key]:
            reasons.append(f"La infraestructura de evaluación actual no coincide: {key}")
    model_mode = candidate.get("kind") == "model_recordings"
    if model_mode and baseline.get("request_profile_sha256") != candidate.get("request_profile_sha256"):
        reasons.append("Cambian los parámetros/perfil de captura")
    if candidate.get("kind") not in {"model_recordings", "deterministic_backend"}:
        reasons.append("Tipo de ejecución desconocido")
    cases = {case["id"]: case for case in corpus["cases"] if not model_mode or case.get("model_probe")}
    minimum = 3 if model_mode else 1
    indexed = []
    for name, report in (("baseline", baseline), ("candidate", candidate)):
        if report.get("complete") is not True:
            reasons.append(f"{name}: ejecución incompleta o fallida")
        if not model_mode and any(report.get(key) != 0 for key in ("test_failures", "skipped", "external_connection_attempts")):
            reasons.append(f"{name}: fallos globales, omisiones o intentos externos")
        rows = {}
        for row in report.get("rows", []):
            key = (row.get("case_id"), row.get("trial"))
            if (key[0] not in cases or type(key[1]) is not int or key[1] < 1
                    or key in rows or type(row.get("passed")) is not bool):
                reasons.append(f"{name}: fila desconocida, inválida o duplicada")
                continue
            rows[key] = row
            if model_mode and (row.get("source") not in {"synthetic", "gemini_recording"} or not isinstance(row.get("model"), str) or not row["model"].strip()):
                reasons.append(f"{name}: procedencia/modelo inválidos")
            if model_mode and row["passed"] and (row.get("category") != "interpretation_correct"
                    or row.get("schema_valid") is not True or row.get("interpretation_correct") is not True
                    or not row.get("checks") or any(c.get("passed") is not True for c in row["checks"])
                    or row.get("manual_review_required")):
                reasons.append(f"{name}: aprobación incompatible con esquema/rúbrica/revisión")
        if model_mode and len({(row.get("source"), row.get("model")) for row in rows.values()}) != 1:
            reasons.append(f"{name}: modelos o fuentes mezclados")
        if model_mode and any(row.get("source") == "gemini_recording" for row in rows.values()) and not report.get("request_profile_sha256"):
            reasons.append(f"{name}: falta perfil de captura real")
        for case_id in cases:
            if sum(key[0] == case_id for key in rows) < minimum:
                reasons.append(f"{name}: faltan trials para {case_id} (mínimo {minimum})")
        indexed.append(rows)
    first, second = indexed
    if first.keys() != second.keys():
        reasons.append("Casos/trials no pareados")
    critical_failures, regressions, pending_reviews, provider_failures = [], [], [], []
    for key, row in second.items():
        if not row["passed"]:
            undecided = model_mode and row.get("category") in {"manual_review_required"}
            unavailable = model_mode and str(row.get("category", "")).startswith("provider_")
            if undecided:
                pending_reviews.append(key[0])
            if unavailable:
                provider_failures.append(key[0])
            if not undecided and not unavailable and cases[key[0]]["criticality"] in {"critical", "high"}:
                critical_failures.append(key[0])
            if not undecided and not unavailable and first.get(key, {}).get("passed"):
                regressions.append(key[0])
        if model_mode and (row.get("source"), row.get("model")) != (first.get(key, {}).get("source"), first.get(key, {}).get("model")):
            reasons.append("Procedencia/modelo no pareados")
    if critical_failures:
        reasons.append("Fallos críticos/altos, incluso si ya fallaban en baseline")
    if any(not row["passed"] for row in second.values()):
        reasons.append("Hay casos candidatos sin aprobación; requieren revisión")
    return {"verdict": "BLOCK" if reasons else "PASS", "reasons": sorted(set(reasons)),
            "critical_failures": sorted(set(critical_failures)), "regressions": sorted(set(regressions)),
            "pending_reviews": sorted(set(pending_reviews)), "provider_failures": sorted(set(provider_failures)),
            "note": "Comparación descriptiva pareada. Sin inferencia estadística ni certificación de producción."}


def write_report(path, report):
    # No sobreescribir evidencias anteriores ni crear carpetas implícitas.
    with Path(path).open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
