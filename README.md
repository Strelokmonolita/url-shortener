# URL Shortener

A URL shortening service with click analytics, built with FastAPI and
SQLAlchemy 2.0 on top of PostgreSQL.

## Status

Stages 1–4 implement the working core: the ASGI application, Docker stack,
authentication, owner-scoped link creation/listing, collision-safe short codes,
public redirects and background click recording. Database-backed contracts are
tested on both SQLite and a real, Alembic-migrated PostgreSQL 17 instance.
Review the Auth and analytics limitations below before deployment; this is not
a fully hardened production deployment.

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

## Links and redirects (Stage 4)

Use an **access** token in `Authorization: Bearer <access_token>` for link
management. Refresh tokens cannot be used here.

| Method and path | Contract |
| --- | --- |
| `POST /api/v1/links` | JSON `original_url`, optional `custom_alias` and `expires_at`; link representation, `201` |
| `GET /api/v1/links?limit=20&offset=0` | Only the current user's links; `{items, total, limit, offset}`, `200` |
| `GET /{code}` | Public `302` with `Location`; unknown code, `404`; disabled or expired link, `410` |

Link representations include `id`, `user_id`, `code`, `original_url`,
`short_url`, `is_custom_alias`, `is_active`, `expires_at`, `created_at` and
`updated_at`. `short_url` uses the configured `BASE_URL`, never the request's
Host header. If the public URL includes a path prefix, configure the reverse
proxy to remove that prefix before forwarding requests to the API.

Targets must be absolute HTTP(S) URLs with a host, without embedded credentials,
control characters or backslashes. URLs are normalized (including IDNA hosts
and percent-encoded paths) and limited to 2048 characters after normalization.
Validation checks syntax only: the server does not fetch targets or resolve
their DNS, and local/private HTTP(S) destinations are allowed.

Aliases are case-sensitive and contain only ASCII letters, digits, `-` and `_`.
They must contain 1–`MAX_CUSTOM_ALIAS_LENGTH` characters (32 by default, at most
64). System paths such as `api`, `docs`, `redoc`, `health`, `openapi`, `static`,
`admin` and `metrics` are reserved **case-insensitively**. Codes are globally
unique across owners; disabled or expired links do not release their codes.
A duplicate alias returns `409` and is never replaced silently.

Generated codes use secure random base62 characters, with length selected by
`SHORT_CODE_LENGTH` (7 by default). Allocation checks the database unique index
and retries collisions, including concurrent insert races, up to 10 times.
Exhaustion returns `503` with `Retry-After: 1` rather than looping indefinitely.

`expires_at` must be a future ISO timestamp **with a timezone**; it is normalized
to UTC. At the exact expiry instant, redirects return `410`. Omitting it creates
a non-expiring link. Lists include unavailable links, ordered newest first with
an ID tie-breaker. `limit` must be 1–100 and `offset` non-negative. Link management,
redirects and domain errors use `Cache-Control: no-store` so cached responses do
not bypass expiry or retain stale link availability. No additional Stage 4
migration is needed; the existing schema already contains links and clicks.

### Create and follow the first link (PowerShell)

Start the API and apply migrations using the startup instructions above. Choose
an unused email and alias for this example; registration is needed only once.
Piping JSON through stdin avoids native argument-quoting differences between
Windows PowerShell and PowerShell 7.

```powershell
$api = "http://localhost:8000"
$credentials = '{"email":"first@example.com","password":"local-example-password"}'
$credentials | curl.exe -sS --fail-with-body "$api/api/v1/auth/register" `
  -H "Content-Type: application/json" --data-binary "@-"

$tokens = $credentials | curl.exe -sS --fail-with-body "$api/api/v1/auth/login/json" `
  -H "Content-Type: application/json" --data-binary "@-" | ConvertFrom-Json

$link = '{"original_url":"https://example.com","custom_alias":"first-link"}' |
  curl.exe -sS --fail-with-body "$api/api/v1/links" `
    -H "Authorization: Bearer $($tokens.access_token)" `
    -H "Content-Type: application/json" --data-binary "@-" | ConvertFrom-Json
$link | ConvertTo-Json

# Inspect the actual GET response: 302 and Location: https://example.com/
curl.exe -i $link.short_url

# Follow the redirect with GET, print response headers, discard the target body.
curl.exe -sS -L -D - -o NUL $link.short_url
```

Use `curl` instead of `curl.exe` and `/dev/null` instead of `NUL` on Linux/macOS.
Do not use `curl -I` for this check: the public endpoint implements `GET`, not
`HEAD`. Each of the two redirect checks above records its own click.

### Click analytics and limitations

Successful redirects enqueue an asynchronous `BackgroundTasks` operation **after
the 302 response is sent**. It opens a separate session from the same database
factory; function-scoped request sessions close before response delivery so
background writes do not wait on a connection held by their own request. It
records a click with a database-generated timestamp, link ID,
visitor hash, User-Agent (up to 512 characters) and Referer (up to 2048). Control
characters are removed from headers. Missing visitor information remains null.
Requests returning `404` or `410` do not record clicks.

Visitor addresses are canonicalized (including IPv4-mapped IPv6) and hashed with
domain-separated HMAC-SHA256 using `SECRET_KEY`; raw IP addresses are never
passed to the background task or stored in the click table. The application uses
the ASGI client address rather than trusting arbitrary `X-Forwarded-For` or
`X-Real-IP` headers. Behind a proxy, configure Uvicorn's trusted proxy allowlist
explicitly; do not trust every forwarding source. The `python -m app` entry point
disables Uvicorn access logs to avoid logging raw visitor addresses. When running
Uvicorn directly, add `--no-access-log`, and review reverse-proxy logging too.

- Background recording is **best-effort, not a durable queue**. Process termination
  or a database failure can lose events; a failed insert never changes the already
  sent redirect. Failures log only the link ID, not SQL parameters or metadata.
- Hashes are pseudonymous, not anonymous. Shared addresses can represent many
  people; changing `SECRET_KEY` changes visitor hashes and breaks continuity.
  User-Agent and Referer can also contain sensitive information; set an explicit
  retention policy before deployment.
- This stage records events but does not expose an aggregation/reporting API or
  link update/delete endpoints. Rate limiting and abuse prevention remain required
  for public production use, alongside the Auth protections listed above.

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

The PostgreSQL suite repeats the Auth, links and redirects API, repository and
model contracts against the actual Alembic schema, not `metadata.create_all()`.
It also checks fresh upgrades, schema drift (types/defaults/keys/indexes), legacy
timestamp conversion, downgrade/re-upgrade, transaction rollback on invalid legacy values, server
defaults, 64-bit identifiers, foreign-key cascades, concurrent registration and
short-code allocation races. Click tests check separate background sessions,
visitor pseudonymization, metadata bounds and analytics failure isolation.

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
