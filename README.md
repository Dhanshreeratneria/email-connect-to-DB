# Gmail → PostgreSQL → MCP → Claude

A Python 3.12+ service that receives Gmail mailbox changes through Gmail Watch and Google Cloud Pub/Sub, retrieves new messages with the Gmail History API, parses and stores them in PostgreSQL, then exposes **read-only** email-search tools to Claude through MCP.

## Production flow

```text
New Gmail email → Gmail Watch → Google Cloud Pub/Sub → POST /webhooks/google/pubsub
→ Gmail History API → Gmail messages.get → parser → PostgreSQL
→ MCP /mcp → Claude searches stored email data → Claude answers
```

Gmail Watch returns a current `historyId` and expiration; renew the watch before it expires. Pub/Sub notifications tell the service that the mailbox changed, while the History API provides the incremental changes to fetch. Google documents that `watch` publishes to a fully-qualified Pub/Sub topic and must be renewed before expiration. [web:76][web:77]

## Project layout

```text
app/
  main.py                 FastAPI + mounted Streamable HTTP MCP app
  config.py               environment settings
  database.py             SQLAlchemy engine/session
  models/email.py         Gmail account and email tables
  schemas/email.py        response schema
  services/               OAuth, Gmail, parsing, sync, Pub/Sub
  api/                    OAuth, Pub/Sub webhook, REST email APIs
  mcp/server.py           read-only MCP tools
alembic/                  PostgreSQL migration
```

## Windows PostgreSQL setup

1. Install PostgreSQL using the official PostgreSQL Windows installer. During installation choose a PostgreSQL superuser password and retain the default port `5432` unless you deliberately change it.
2. Open **SQL Shell (psql)** and connect as `postgres`.
3. Create an application user and database:

```sql
CREATE USER gmail_mcp WITH PASSWORD 'use-a-long-unique-password';
CREATE DATABASE gmail_email_db OWNER gmail_mcp;
GRANT ALL PRIVILEGES ON DATABASE gmail_email_db TO gmail_mcp;
```

4. Copy `.env.example` to `.env` and set:

```env
DATABASE_URL=postgresql+psycopg://gmail_mcp:use-a-long-unique-password@localhost:5432/gmail_email_db
```

5. Create tables:

```powershell
python -m venv .venv
.venv\Scriptsctivate
pip install -r requirements.txt
alembic upgrade head
```

6. Test the connection:

```powershell
python -c "from app.database import engine; from sqlalchemy import text; print(engine.connect().execute(text('select 1')).scalar())"
```

Expected output: `1`.

## Local setup

```powershell
python -m venv .venv
.venv\Scriptsctivate
pip install -r requirements.txt
copy .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
```

Use `http://localhost:8000/health` and `http://localhost:8000/docs`.

## Google Cloud and Gmail OAuth

1. In Google Cloud Console, create a project.
2. Enable **Gmail API** and **Cloud Pub/Sub API**.
3. Configure the OAuth consent screen. Add yourself as a test user during development.
4. Create **Credentials → OAuth client ID → Web application**.
5. Add this redirect URI locally:

```text
http://localhost:8000/auth/google/callback
```

6. Download the OAuth client JSON, name it `credentials.json`, and place it at the repository root. It is ignored by Git.
7. In `.env`, keep:

```env
GOOGLE_CLIENT_SECRETS_FILE=credentials.json
GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8000/auth/google/callback
```

8. Generate a Fernet encryption key for securely storing refresh tokens in PostgreSQL:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set the output as `TOKEN_ENCRYPTION_KEY` in `.env`.
9. Run the app and open `http://localhost:8000/auth/google`. Approve the readonly Gmail scope. The callback saves an encrypted token, performs initial sync, and creates the Gmail watch.

Google’s server-side authorization flow is appropriate when a server needs offline access on a user’s behalf; it exchanges the authorization code for credentials including refresh-token capability. [web:75][web:78]

## Pub/Sub configuration

Create a Pub/Sub topic using the exact full topic path entered in `GOOGLE_PUBSUB_TOPIC`, then create a **push subscription** with endpoint:

```text
https://YOUR-PUBLIC-DOMAIN/webhooks/google/pubsub
```

Grant Gmail’s publisher service account permission to publish to the topic, following Google’s Gmail push-notification guide. Gmail sends a base64 JSON payload containing `emailAddress` and `historyId`. The webhook decodes that payload, identifies the connected account, and calls the History API incrementally.

For a production push subscription, configure OIDC authentication at the subscription and verify its Google-issued JWT in front of this endpoint or add that verification in `api/webhook.py`. Do not expose this endpoint unauthenticated on the public internet.

## Watch renewal

Gmail watches expire. Run a daily authenticated scheduler (Render Cron Job, Cloud Scheduler, or a worker) that finds accounts with `watch_expiration` within 24 hours and calls `renew_watch()` from `app.services.sync_service`. The Gmail Watch response contains the new `historyId` and expiration. [web:76][web:77]

## REST API

```text
GET  /health
GET  /auth/google
GET  /auth/google/callback
POST /webhooks/google/pubsub
GET  /emails?q=invoice
GET  /emails/{message_id}
```

Use `/docs` for interactive local testing. Email REST endpoints should be protected with your application authentication in production.

## MCP tools

The mounted Streamable HTTP MCP endpoint is:

```text
https://YOUR-DOMAIN/mcp
```

Tools:

- `search_emails(query_text, limit)`
- `get_email(message_id)`
- `list_emails(limit)`
- `get_thread(thread_id)`
- `search_by_sender(sender, limit)`
- `search_by_subject(subject, limit)`
- `search_by_date(start_iso, end_iso, limit)`

They query PostgreSQL only and do not load or reveal Gmail OAuth credentials. The official Python MCP SDK supports Streamable HTTP and recommends it for production remote deployments. [web:82][web:83][web:87]

## Claude connection

1. Deploy the service on a public HTTPS URL.
2. Protect `/mcp` with an authenticated reverse proxy/API gateway; use a separate read-only MCP credential and never reuse Google OAuth credentials.
3. In Claude’s connector/MCP configuration, add the remote endpoint:

```text
https://YOUR-DOMAIN/mcp
```

4. Complete the connector authentication required by your gateway.
5. Ask Claude, for example: “Find the latest email from Alice about the invoice and summarize the requested action.” Claude calls a read-only tool, receives matching PostgreSQL records, and answers from those records.

## Render deployment

Create a Render PostgreSQL database. Set `DATABASE_URL` to Render’s internal PostgreSQL URL. Deploy this repository as a Python Web Service:

```text
Build command: pip install -r requirements.txt && alembic upgrade head
Start command: uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Set all values from `.env.example` as Render secret environment variables. Set:

```text
GOOGLE_OAUTH_REDIRECT_URI=https://YOUR-RENDER-SERVICE.onrender.com/auth/google/callback
GOOGLE_PUBSUB_AUDIENCE=https://YOUR-RENDER-SERVICE.onrender.com/webhooks/google/pubsub
```

Add the production callback URL to Google OAuth client authorized redirect URIs, and create the Pub/Sub push subscription with the production webhook URL. Run a separate Render Cron Job for watch renewals.

## Security notes

- Never commit `.env`, `credentials.json`, OAuth refresh tokens, or PostgreSQL passwords.
- OAuth tokens are encrypted at rest with `TOKEN_ENCRYPTION_KEY`; rotate carefully and retain key access for existing data.
- Gmail scope is readonly.
- Limit MCP results and enforce user/tenant authorization before returning email content.
- Add PostgreSQL full-text search or `pg_trgm` indexes for large mailboxes; basic `ILIKE` is included for clarity.
- This repository is a runnable foundation, but deployment-level auth, monitoring, backup, rate-limit, and scheduler configuration remain mandatory production operations.
