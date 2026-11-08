# URL Shortener

A URL shortening service with click analytics, built with FastAPI and
SQLAlchemy 2.0 on top of PostgreSQL.

## Status

Work in progress. Stage 1 provides the ASGI application, local startup, a Docker
stack, API documentation and an application liveness endpoint. Existing Auth
routes are mounted, but Auth hardening, database schema alignment, shortening
links and redirects are still unfinished. This is not production-ready.

`GET /health` returns `200` with `{"status":"ok"}` without connecting to the
database. It is **not** a database or migration readiness check. Application
startup never creates tables or applies migrations automatically.

## Requirements

- Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).
- Docker with Docker Compose for the container stack. On Windows, start Docker
  Desktop with its Linux engine before running Compose commands.
- GNU Make is optional; all commands below can be run directly in PowerShell.

## Configuration

If `.env` does not already exist, copy `.env.example` to `.env`. Do not overwrite
an existing configuration.

```powershell
Copy-Item .env.example .env
```

On Linux/macOS, use `cp .env.example .env` instead. Environment variables override
the values in `.env`. Replace the example JWT secret before any real deployment;
the current application does not reject the insecure default yet.

For a local API, `POSTGRES_HOST` and `POSTGRES_PORT` select the database address.
Compose instead connects its API container to `db:5432`. `PORT` and
`POSTGRES_PORT` select the ports published on localhost; update `BASE_URL` if you
change the public API port. Defaults are `8000` and `5432`.

## Local startup

```powershell
uv sync --frozen --extra dev
uv run --frozen python -m app
```

Set `DEBUG=true` in `.env` to enable live reload. Alternatively, use the ASGI
application directly: `uv run --frozen uvicorn app.main:app --host 127.0.0.1 --port 8000`.

In another terminal:

```powershell
curl.exe -i http://localhost:8000/health
```

Use `curl` instead of `curl.exe` on Linux/macOS. With the default port:

- Swagger UI: <http://localhost:8000/docs>
- ReDoc: <http://localhost:8000/redoc>
- OpenAPI: <http://localhost:8000/openapi.json>

Local startup and `/health` do not require PostgreSQL. Database-backed requests
do require an available database with the correct migrations.

## Docker stack

After creating `.env` and starting Docker:

```powershell
docker compose config --quiet
docker compose up --build -d
docker compose ps
curl.exe -i http://localhost:8000/health
```

The stack uses PostgreSQL 17 and an API image running as a non-root user. The API
waits for PostgreSQL's healthcheck; its own healthcheck only checks `/health`.
Published ports bind to localhost. Dependencies are installed from `uv.lock`
without dev tools, and `.env` is excluded from the image build context.

For a local API with only PostgreSQL in Docker, run `docker compose up -d db`
instead of the full stack. Do not run local and container APIs on the same host
port at the same time.

```powershell
docker compose logs -f api
docker compose down
```

Stopping the stack keeps the `postgres_data` volume. Existing database data is
not reset automatically. Changing database credentials in `.env` does not
reconfigure an already initialized PostgreSQL volume.

### Migrations

The initial migration currently disagrees with the ORM's `last_login_at` type.
Schema alignment and PostgreSQL integration tests belong to Stage 3. Starting
this stack does **not** mean Auth is ready for use against its database.

When the migration work is ready, the explicit command is
`docker compose exec api alembic upgrade head` (`make migrate`). It is never part
of startup. Do not downgrade or reset an existing database to work around the
unfinished schema.

## Quality checks

Development tools use the `dev` extra, not a separate dependency group. The tests
use in-memory SQLite and do not require Docker or PostgreSQL.

```powershell
uv run --frozen --extra dev ruff check .
uv run --frozen --extra dev ruff format --check .
uv run --frozen --extra dev mypy app tests
uv run --frozen --extra dev pytest
```

To include coverage, run
`uv run --frozen --extra dev pytest --cov=app --cov-report=term-missing`.

### Make shortcuts

| Target | Purpose |
| --- | --- |
| `make install` | Install locked runtime and dev dependencies |
| `make run` | Start the local API |
| `make lint` | Check Ruff lint and formatting |
| `make format` | Apply Ruff fixes and formatting |
| `make typecheck` | Run strict Mypy |
| `make test` | Run the test suite |
| `make check` | Run lint, typing and tests |
| `make up` | Build and start Docker services without migrations |
| `make down` | Stop Docker services without removing database data |
| `make logs` | Follow API container logs |
| `make migrate` | Explicitly apply migrations; deferred until Stage 3 |
| `make revision m="message"` | Generate a migration against the configured DB |
