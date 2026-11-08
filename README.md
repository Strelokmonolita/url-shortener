# URL Shortener

A URL shortening service with click analytics, built with FastAPI and
SQLAlchemy 2.0 on top of PostgreSQL.

## Status

Work in progress. Stages 1–3 provide the ASGI application, local startup, a Docker
stack, API documentation, liveness, hardened Auth routes and aligned PostgreSQL
migrations. Database-backed contracts are tested on both SQLite and a real,
Alembic-migrated PostgreSQL 17 instance. Link shortening and redirects are still
unfinished. This is not production-ready.

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
the values in `.env`. The example JWT secret is accepted only in `local` and
`test`. In `staging` and `production`, startup rejects short signing keys and
`DEBUG=true`. Generate a random secret before deployment:

```powershell
uv run --frozen python -c "import secrets; print(secrets.token_urlsafe(64))"
```

`HS256`, `HS384` and `HS512` require at least 32, 48 and 64 key bytes respectively
in deployed environments. Never reuse a development secret in deployment.

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

Apply migrations explicitly before using database-backed routes. Neither local
startup nor Compose applies them automatically:

```powershell
# For a locally running API, using its configured PostgreSQL connection:
uv run --frozen alembic upgrade head
uv run --frozen alembic check
```

For the full container stack, use `docker compose exec api alembic upgrade head`
(`make migrate`) and `docker compose exec api alembic check` instead.

The initial revision `dae2356cd32c` is unchanged. Stage 3 adds revision
`6c3f9d7a2b10`, converting `users.last_login_at` from `VARCHAR(40)` to
`TIMESTAMP WITH TIME ZONE` in place. Existing accounts, password hashes, links,
clicks and null timestamps are retained. Legacy timestamps with offsets preserve
their instants; timezone-naive values are interpreted as UTC independently of
the database session's timezone. Invalid timestamp text aborts the transaction,
leaving data and the migration revision unchanged; review and correct affected
rows deliberately instead of clearing them automatically.

The follow-up downgrade restores ISO-formatted UTC text, not necessarily the
original timestamp spelling. Back up existing databases and schedule migrations:
PostgreSQL locks the users table during the conversion. Do not reset a database
or downgrade to `base` to fix an upgrade failure.

## Authentication (Stage 2)

All paths below have the `/api/v1/auth` prefix and require a database with the
correct schema. Apply the migrations above before using Auth on PostgreSQL.

| Method and path | Contract |
| --- | --- |
| `POST /register` | JSON `email` and `password`; public user fields, `201`; duplicate email, `409` |
| `POST /login` | OAuth2 form: email in `username`, plus `password`; access/refresh pair |
| `POST /login/json` | JSON `email` and `password`; same token pair |
| `POST /refresh` | JSON `refresh_token`; new access token only |
| `GET /me` | `Authorization: Bearer <access_token>`; public user fields |
| `GET /exists?email=...` | Public registration availability check |

Emails are trimmed and lowercased for registration and both login encodings.
Registration passwords contain 8–1024 characters; login accepts 1–1024. Unicode
and passwords longer than 72 bytes are supported without truncation. Passwords
use a versioned bcrypt hash of the base64-encoded SHA-256 digest at cost 12.
Original unprefixed hashes remain verifiable and are upgraded with the login
timestamp in one transaction after successful authentication. bcrypt work runs
outside the request event loop.

Tokens include `sub`, `type`, `iat`, `exp` and `jti`. Access and refresh tokens
are not interchangeable. Defaults are 15 minutes and 30 days respectively;
`expires_in` reports the access lifetime in seconds. Invalid credentials and
tokens return `401` with `WWW-Authenticate: Bearer`; invalid request fields
return `422`. Validation details contain only `type`, `loc` and `msg`, never
submitted passwords or internal exception context. Successful Auth responses
and authentication errors disable HTTP caching.

### Auth limitations

- Refresh tokens are **not rotated or revoked**: they can be reused until expiry.
  Disabling or deleting an account prevents its existing tokens from being used.
- `/exists` and registration conflicts intentionally reveal account availability.
  Uniform login errors are not a guarantee against account enumeration.
- Rate limiting, logout/revocation and password recovery are not implemented.
  Real deployments also require HTTPS and these missing protections.

## Quality checks

Development tools use the `dev` extra, not a separate dependency group. The tests
below use in-memory SQLite and do not require Docker or PostgreSQL. Auth tests
use real bcrypt at cost 4 for speed and a test-only JWT secret, never the secret
from the developer's `.env`; runtime bcrypt cost remains 12.

```powershell
uv run --frozen --extra dev ruff check .
uv run --frozen --extra dev ruff format --check .
uv run --frozen --extra dev mypy app tests
uv run --frozen --extra dev pytest -m "not postgres"
```

To include coverage, run
`uv run --frozen --extra dev pytest -m "not postgres" --cov=app --cov-report=term-missing`.

### PostgreSQL integration tests (Stage 3)

The PostgreSQL suite repeats the Auth API, repository and model contracts against
the actual Alembic schema, not `metadata.create_all()`. It also checks fresh
upgrades, schema drift (types/defaults/keys/indexes), legacy timestamp conversion,
downgrade/re-upgrade, transaction rollback on invalid legacy values, server
defaults, 64-bit identifiers, foreign-key cascades and concurrent registration.

Use the separate disposable test stack; it does not share the application's
`postgres_data` volume or credentials:

```powershell
docker compose -f compose.test.yaml up -d --wait
$env:TEST_DATABASE_URL = "postgresql+asyncpg://shortener_test:shortener_test@127.0.0.1:5433/shortener_test"
uv run --frozen --extra dev pytest -m postgres --require-postgres
docker compose -f compose.test.yaml down
Remove-Item Env:TEST_DATABASE_URL
```

On Linux/macOS, set the URL with:

```sh
export TEST_DATABASE_URL='postgresql+asyncpg://shortener_test:shortener_test@127.0.0.1:5433/shortener_test'
```

Use `unset TEST_DATABASE_URL` after stopping the test stack.
The test stack uses port `5433` and keeps PostgreSQL data in temporary memory;
stopping it discards only test data.

Alternatively, point `TEST_DATABASE_URL` at a dedicated PostgreSQL test database
using the `postgresql+asyncpg` driver. Its role must be allowed to create schemas.
Every test creates a randomly named schema with no `public` search-path fallback
and removes only that schema, including after failures. Tests never fall back to
the application connection from `.env` and never drop the configured database or
existing public tables. An invalid URL or an unavailable configured database
fails the tests.

A plain `pytest` runs both backends when the URL is set; otherwise PostgreSQL
cases are skipped. `--require-postgres` makes a missing URL an error, preventing
an integration check from silently passing without PostgreSQL. GitHub Actions
runs lint, formatting, strict typing and the entire suite with PostgreSQL 17 and
this flag enabled.

### Make shortcuts

| Target | Purpose |
| --- | --- |
| `make install` | Install locked runtime and dev dependencies |
| `make run` | Start the local API |
| `make lint` | Check Ruff lint and formatting |
| `make format` | Apply Ruff fixes and formatting |
| `make typecheck` | Run strict Mypy |
| `make test` | Run unit and SQLite tests without external services |
| `make test-postgres` | Run integration tests with an explicit `TEST_DATABASE_URL` |
| `make check` | Run local lint, typing, unit and SQLite tests |
| `make up` | Build and start Docker services without migrations |
| `make down` | Stop Docker services without removing database data |
| `make logs` | Follow API container logs |
| `make migrate` | Explicitly apply migrations inside the API container |
| `make revision m="message"` | Generate a migration against the configured DB |
