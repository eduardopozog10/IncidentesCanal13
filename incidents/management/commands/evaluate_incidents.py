from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from incidents.evaluation.core import load_corpus, write_report
from incidents.evaluation.runner import require_isolation, run_backend


class Command(BaseCommand):
    help = "Evaluación offline del corpus de Incidencias; solo base temporal de tests."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--output", type=Path, required=True)
        parser.add_argument("--agent-version", required=True)

    def handle(self, *args, **options):
        try:
            require_isolation()
            if options["output"].exists() or not options["output"].parent.is_dir():
                raise ValueError("El destino debe ser nuevo y su carpeta debe existir")
            corpus = load_corpus()
            report = run_backend(corpus, options["agent_version"], verbosity=options["verbosity"])
            write_report(options["output"], report)
        except (ValueError, OSError) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(f"{sum(row['passed'] for row in report['rows'])}/{len(report['rows'])} casos; "
                          f"{report['tests_run']} tests; {report['skipped']} omitidos; "
                          f"{report['external_connection_attempts']} intentos externos. Modelo: NO ejecutado.")
        if not report["complete"]:
            raise CommandError(f"Evaluación fallida. Revisar {options['output']}")
