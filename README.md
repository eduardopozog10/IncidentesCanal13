# Secuoya Chile | Gestión de Incidencias Operativas

El nombre histórico del repositorio se conserva por compatibilidad.

## Ejecución normal

```powershell
uv run python manage.py runserver
uv run python manage.py run_telegram_bot
```

Ejecutar cada proceso en su terminal, con el `.env` local existente. No ejecutar dos receptores del mismo bot simultáneamente. Ctrl+C detiene el receptor.

### Seguridad de entradas de Telegram en el piloto

- El polling es la única entrada de Telegram habilitada. El webhook HTTP devuelve 404 y no procesa actualizaciones, aunque incluyan un supuesto token secreto.
- `/api/v1/messages/test/` requiere personal autenticado y `DEBUG=True`; con sesión exige CSRF. Solo permite probar cuentas existentes: un JSON HTTP nunca habilita autorregistro.
- El autorregistro conserva su configuración actual (`DEBUG=True` y `TELEGRAM_AUTO_REGISTER=True`) y funciona desde el receptor de polling, exclusivamente en chats privados con remitente y chat coherentes. No crea usuarios de Django ni concede acceso al dashboard.
- Se ignoran grupos, canales y chats inconsistentes, también para `/start`, `/cancelar` y avisos de procesamiento. Un canal antiguo sin chat guardado admite su chat privado coherente; si tiene un chat guardado diferente, se rechaza y debe revisarse administrativamente, sin reasignarlo automáticamente.
- La marca interna `trusted_telegram` solo la proporciona el receptor desde Python. No es una credencial ni un campo aceptado desde HTTP. No se requiere configurar secretos de webhook ni cambiar credenciales para estos ajustes.

### Recuperación del piloto (Codex 2)

La migración aditiva `0011_pilot_recovery` es necesaria antes de ejecutar esta versión. No se aplica automáticamente a la base operativa: requiere autorización previa. Añade resultados/intentos/origen de mensajes, respuestas vinculadas y estado de notificación; conserva el historial existente.

El receptor de polling reanuda mensajes durables al iniciar y en cada ciclo. Un worker local de ese mismo proceso envía las notificaciones persistidas, con `EMAIL_TIMEOUT=10` por defecto. No requiere otro servicio. Hay hasta tres intentos automáticos; una entrega incierta no se reenvía automáticamente.

`uv run python manage.py recover_pilot` **solo lista IDs y estados, sin enviar**. Las opciones `--run`, `--message-id`, `--reply-id` y `--notification-id` ejecutan recuperaciones explícitas y pueden contactar servicios reales según el entorno. Ver [procedimiento, límites y validación](docs/pilot-recovery.md) antes de utilizarlas. No usar `--discard-pending` para recuperar mensajes: descarta actualizaciones aún no almacenadas.

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
