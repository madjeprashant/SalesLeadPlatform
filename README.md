# Northstar Sales Lead Distribution & Opportunity Management Platform

A local-first CRM built with Flask. It supports the original lead workflow plus the missing access, API, testing and deployment features: responsive UI, authentication, role-based access control, a REST API, automated tests, SQLite for local development and PostgreSQL for production.

## Run locally on Windows

Double-click `start.bat`, or run:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000. The initial database includes sample records and two local demo accounts:

- Administrator: `admin` / `ChangeMe123!`
- Sales: `sales` / `SalesDemo123!`

Change these demo passwords before using this workspace with real information. Set `ADMIN_PASSWORD`, `SALES_PASSWORD`, and `SECRET_KEY` in the environment before the first run to override the bootstrap values. Existing accounts are not overwritten on later runs.

## Roles

- **Admin:** full access, sales-team and territory/product configuration, account creation, activation and password resets, analytics, audit history, leads and opportunities.
- **Manager:** leads and opportunities plus sales analytics and audit history.
- **Sales:** dashboard, lead and opportunity work, activities. Sales users cannot open team configuration, product/territory configuration, reports, audit history or user administration.

Accounts with admin, manager and sales roles can be created, disabled and reset from **Users & access** while signed in as an administrator. All users can change their own password under **My account**. Passwords are stored as salted hashes. Authenticated browser posts require a per-session CSRF token. Sessions use HTTP-only, SameSite cookies; production mode also enables the Secure cookie flag.

## REST API

All API endpoints use JSON. Obtain a signed bearer token with the account credentials:

```http
POST /api/v1/auth/token
Content-Type: application/json

{"username":"admin","password":"your-password"}
```

The response includes `access_token`, `token_type`, `expires_in`, and the user's role. Send it on protected requests as `Authorization: Bearer <access_token>`.

| Method | Endpoint | Access |
|---|---|---|
| GET | `/api/v1/health` | Public health check |
| GET | `/api/v1/leads` | Authenticated |
| POST | `/api/v1/leads` | Authenticated |
| GET | `/api/v1/leads/{id}` | Authenticated |
| PATCH | `/api/v1/leads/{id}` | Authenticated |
| GET | `/api/v1/opportunities` | Authenticated |

Create-lead JSON accepts `name` (required), `company`, `email`, `phone`, `source`, `product_id`, `territory_id`, `budget`, `engagement` and `notes`. The API scores and routes the lead using the same engine as the browser app. Tokens are signed and expire after `API_TOKEN_TTL` seconds (one hour by default). API consumers should keep tokens private and request a new token when one expires.

## Lead scoring and routing

The lead score is capped at 100. It combines a budget contribution (up to 35 points), 35% of the engagement value, a source contribution (5–18 points), and 8 points for product interest. Scores of 75+ are High priority, 50–74 Medium, and below 50 Low.

Assignment filters active representatives to the selected territory and available lead capacity, then ranks matches using a product expertise bonus and remaining capacity. A lead without a territory can route to any territory. Leads with no eligible representative remain unassigned and can be retried after team configuration changes.

## Automated tests

```powershell
python -m unittest discover -s tests -v
```

The tests use a temporary SQLite database. They cover login, role permissions, password changes and reset revocation, bearer-token issuance, REST lead operations, CSRF enforcement, PostgreSQL SQL adaptation, and the lead-to-closed-won workflow. PostgreSQL adapter tests use a mock connection; run the Docker deployment with a live database to validate your target environment.

## Production deployment: PostgreSQL + Docker

1. Install Docker Engine/Desktop and Docker Compose.
2. Copy `.env.example` to `.env` and replace every sample secret with unique random values. For `POSTGRES_PASSWORD`, use URL-safe characters (hex is a good choice) because Compose embeds it in the PostgreSQL connection URL. Use a minimum 12-character admin and sales password. On an existing database, update bootstrap/demo passwords from **My account**; environment bootstrap values only apply when the account is first created.
3. Start the services:

   ```sh
   docker compose up --build -d
   ```

4. Open `http://localhost:8000`. The web container runs Gunicorn; the database is PostgreSQL 16 with a persistent Docker volume. Check status with `docker compose ps` and logs with `docker compose logs -f web`.
5. Back up the `northstar_pgdata` volume regularly. Put a TLS reverse proxy in front of the published service before exposing it publicly, then set `SESSION_COOKIE_SECURE=true` in `.env`. The example keeps it false so local HTTP testing works.

The app automatically selects PostgreSQL when `DATABASE_URL` starts with `postgres://` or `postgresql://`; otherwise it uses the local SQLite file. `PORT` sets the local development port. `APP_PORT` controls the Docker published port.

## Project structure

- `app.py` — Flask views, authentication, roles, REST API, schema, scoring and assignment.
- `templates/` and `static/` — responsive CRM and access-control UI.
- `tests/` — automated workflow and access tests.
- `Dockerfile` and `docker-compose.yml` — Gunicorn + PostgreSQL deployment.
- `requirements.txt` — Flask, PostgreSQL driver and Gunicorn.
- `start.bat` — Windows local setup and launch.
