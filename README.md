# Secuoya Chile | Gestión de Incidencias Operativas

El nombre histórico del repositorio se conserva por compatibilidad.

## Ejecución normal

```powershell
uv run python manage.py runserver
uv run python manage.py run_telegram_bot
```

Ejecutar cada proceso en su terminal, con el `.env` local existente. No ejecutar dos receptores del mismo bot simultáneamente. Ctrl+C detiene el receptor.

## Validación aislada

```powershell
uv run python manage.py check
uv run python manage.py makemigrations --check --dry-run
uv run python manage.py test incidents --settings=config.qa --noinput
git diff --check
```

`config.qa` fija PostgreSQL `qa_secuoya_20261019`, base de tests `test_qa_secuoya_20261019`, correo en memoria y Telegram deshabilitado. Usa la misma instancia y el mismo rol del proyecto; no requiere nuevas credenciales. No cambia `.env` ni apunta a la base principal.

Para preparar la base y ejecutar escenarios internos con Gemini real:

```powershell
uv run python scripts/qa_scenarios.py --prepare --round nombre-nuevo
# Repetir únicamente casos fallidos:
uv run python scripts/qa_scenarios.py --round otra-ronda --cases 11 12
```

Cada ronda conserva su evidencia; no sobrescribe informes ni elimina datos. La preparación requiere permiso local de crear bases de datos. No volver a ejecutar todos los casos reales si solo fallaron algunos.

La inspección visual usa exclusivamente QA:

```powershell
uv run python manage.py runserver 127.0.0.1:8765 --settings=config.qa --noreload
```

Para Telegram real, primero comprobar que no haya otro receptor ni webhook activo y usar únicamente una cuenta confirmada por su propietario:

```powershell
$env:SECUOYA_QA_REPORTER_ID = '<ID autorizado>'
uv run python scripts/qa_telegram_receiver.py
# Exportar evidencia sin tokens ni payloads privados:
uv run python scripts/qa_export.py nombre-nuevo
```

El receptor temporal verifica la identidad del bot Secuoya, restringe la cuenta y se detiene si recibe un mensaje de otra persona. `--timeout-next` simula una única llamada fallida de IA; las siguientes usan Gemini real. Detener con Ctrl+C al terminar; después se puede volver a ejecutar el comando normal del bot. No descartar actualizaciones pendientes de forma automática.

[Informe y evidencias de la renovación del 19 de octubre de 2026](docs/qa-2026-10-19/informe.md).
