"""CLI offline. No configura Django, abre bases ni ejecuta proveedores."""
import argparse
import json
from pathlib import Path

from .core import compare_reports, grade_records, load_corpus, write_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    grade = commands.add_parser("grade", help="Calificar grabaciones JSONL existentes, sin API")
    grade.add_argument("--input", type=Path, required=True)
    grade.add_argument("--output", type=Path, required=True)
    grade.add_argument("--agent-version", required=True)
    grade.add_argument("--reviews", type=Path, help="Adjudicaciones humanas JSONL vinculadas al hash de respuesta")
    compare = commands.add_parser("compare", help="Comparar dos informes completos")
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args()
    try:
        corpus = load_corpus()
        if args.command == "grade":
            records = [json.loads(line) for line in args.input.read_text(encoding="utf-8").splitlines() if line.strip()]
            reviews = [json.loads(line) for line in args.reviews.read_text(encoding="utf-8").splitlines() if line.strip()] if args.reviews else []
            report = grade_records(records, corpus, args.agent_version, reviews=reviews)
            write_report(args.output, report)
            print(json.dumps({"complete": report["complete"], "metrics": report["metrics"],
                              "missing_cases": report["missing_cases"]}, ensure_ascii=False))
            return 0 if report["complete"] and all(row["passed"] for row in report["rows"]) else 2
        report = compare_reports(json.loads(args.baseline.read_text(encoding="utf-8")),
                                 json.loads(args.candidate.read_text(encoding="utf-8")), corpus)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["verdict"] == "PASS" else 2
    except (ValueError, KeyError, OSError) as error:
        parser.exit(3, f"Entrada inválida: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
