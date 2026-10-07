# TVTEL Incidentes

Guía para configurar y levantar el proyecto localmente.

## Requisitos

Instalar:

- Python 3.13
- PostgreSQL 18
- `uv`
- GitHub Desktop
- Visual Studio Code

Verificar las instalaciones:

```powershell
python --version
uv --version
psql --version
```

## 1. Descargar o actualizar el proyecto

### Primera vez

Clonar el repositorio mediante GitHub Desktop y abrirlo en Visual Studio Code.

### Si el proyecto ya está descargado

En GitHub Desktop:

1. Pulsar **Fetch origin**.
2. Pulsar **Pull origin** si existen cambios.

## 2. Instalar las dependencias

Desde la carpeta donde está `manage.py`:

```powershell
uv sync
```

## 3. Configurar PostgreSQL

Cada computador utiliza un puerto diferente:

| Computador | Puerto |
|---|---:|
| PC | `5433` |
| Notebook | `5432` |

Comprobar que PostgreSQL esté iniciado desde **Servicios** de Windows.

### Crear usuario y base de datos

Este paso se realiza solo la primera vez en cada computador.

En el PC:

```powershell
psql -h localhost -p 5433 -U postgres -d postgres
```

En el notebook:

```powershell
psql -h localhost -p 5432 -U postgres -d postgres
```

Dentro de PostgreSQL:

```sql
CREATE USER tvtel_app WITH PASSWORD 'TU_CLAVE_LOCAL';
CREATE DATABASE tvtel_db OWNER tvtel_app;
```

Salir:

```text
\q
```

## 4. Crear el archivo `.env`

El archivo `.env` es local y no se descarga desde GitHub.

Crear una copia de `.env.example`:

```powershell
Copy-Item .env.example .env
```

Abrir `.env` y completar las variables.

### Configuración del PC

```env
POSTGRES_DB=tvtel_db
POSTGRES_USER=tvtel_app
POSTGRES_PASSWORD=TU_CLAVE_LOCAL
POSTGRES_HOST=localhost
POSTGRES_PORT=5433
```

### Configuración del notebook

```env
POSTGRES_DB=tvtel_db
POSTGRES_USER=tvtel_app
POSTGRES_PASSWORD=TU_CLAVE_LOCAL
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
```

## 5. Generar la clave secreta de Django

Ejecutar:

```powershell
uv run python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

Copiar el resultado en `.env`:

```env
DJANGO_SECRET_KEY=CLAVE_GENERADA
```

No compartir ni subir esta clave a GitHub.

## 6. Configurar la inteligencia artificial

Para usar Gemini:

```env
AI_PROVIDER=gemini
GEMINI_API_KEY=TU_CLAVE_DE_GEMINI
GEMINI_MODEL=gemini-3.1-flash-lite
```

Para trabajar sin Gemini:

```env
AI_PROVIDER=mock
```

Las claves reales deben guardarse solamente en `.env`.

## 7. Aplicar las migraciones

```powershell
uv run python manage.py migrate
```

Este comando debe ejecutarse después de hacer pull si existen migraciones nuevas.

## 8. Crear un administrador

Solo la primera vez en cada base de datos local:

```powershell
uv run python manage.py createsuperuser
```

## 9. Comprobar la configuración

```powershell
uv run python manage.py check
```

El resultado esperado es:

```text
System check identified no issues (0 silenced).
```

## 10. Levantar el servidor

```powershell
uv run python manage.py runserver
```

Direcciones disponibles:

- Administrador: http://127.0.0.1:8000/admin/
- API local: http://127.0.0.1:8000/api/

La terminal del servidor debe permanecer abierta. Para ejecutar pruebas se debe utilizar una segunda terminal.

## Pasos habituales para comenzar a trabajar

Cada vez que se cambie de computador:

1. Abrir GitHub Desktop.
2. Ejecutar **Fetch origin**.
3. Ejecutar **Pull origin**.
4. Abrir Visual Studio Code.
5. Ejecutar:

```powershell
uv sync
uv run python manage.py migrate
uv run python manage.py check
uv run python manage.py runserver
```

## Pasos para terminar de trabajar

1. Revisar los cambios en GitHub Desktop.
2. Confirmar que `.env` no aparezca.
3. Crear el commit.
4. Pulsar **Push origin**.
5. Realizar el push antes de cambiar de computador.

## Importante

GitHub sincroniza el código, pero no sincroniza:

- `.env`
- `.venv`
- Contraseñas y tokens
- La base de datos PostgreSQL local
- Los incidentes creados localmente

Cada computador mantiene su propia base de datos de desarrollo.