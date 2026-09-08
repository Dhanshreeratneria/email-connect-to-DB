# Deploying to Render

This walks through deploying the FastAPI + MCP service and its PostgreSQL database on Render, step by step.

## 1. Push the code to GitHub

Render deploys from a Git repo.

```bash
git remote add origin https://github.com/YOUR_USER/gmail-email-mcp.git
git push -u origin main
```

Confirm `.gitignore` excludes `.env` and `credentials.json` before pushing — never commit real secrets.

## 2. Create the PostgreSQL database on Render

1. Render Dashboard → **New** → **PostgreSQL**.
2. Name: `gmail-email-db` (or anything).
3. Region: pick the **same region** you'll use for the web service (keeps latency low and avoids cross-region egress).
4. Plan: Free is fine for testing; use a paid plan for anything real (Free databases expire after 90 days and are deleted).
5. Click **Create Database**.

Once it's up, open the database page and note two connection strings:

| Field | Where it's used |
|---|---|
| **Internal Database URL** | Use this in your web service's `DATABASE_URL` — it's private, faster, and free (no bandwidth charges) since the traffic stays inside Render's network. |
| **External Database URL** | Only needed if you connect from your own laptop (e.g. to run `psql` or a local `alembic upgrade head` against the prod DB). |

Render's internal URL looks like:
```
postgresql://gmail_mcp:xxxxxxxx@dpg-xxxxxxxxxxxx-a/gmail_email_db
```

This project's SQLAlchemy engine expects the `psycopg` driver, so prefix it:
```
postgresql+psycopg://gmail_mcp:xxxxxxxx@dpg-xxxxxxxxxxxx-a/gmail_email_db
```
(Copy the value straight from Render, then just add `+psycopg` after `postgresql`.)

## 3. Create the Web Service

1. Render Dashboard → **New** → **Web Service** → connect the GitHub repo.
2. Runtime: **Python 3**.
3. Build command:
   ```
   pip install -r requirements.txt && alembic upgrade head
   ```
   Running `alembic upgrade head` in the build step means every deploy automatically applies any new migrations before the new code starts serving traffic.
4. Start command:
   ```
   uvicorn app.main:app --host 0.0.0.0 --port $PORT
   ```
   Render injects `$PORT` at runtime — don't hardcode `10000` here even though the Dockerfile uses it locally.
5. Region: same as the database.
6. Plan: Free works for testing (spins down when idle — the first request after idle is slow), or a paid instance for anything with a real Pub/Sub subscription hitting it.

## 4. Set environment variables

On the web service → **Environment** tab, add each of these. Mark the sensitive ones as **secret** (Render blurs them in the dashboard and excludes them from logs).

| Key | Value | Notes |
|---|---|---|
| `DATABASE_URL` | the Internal Database URL from step 2, with `+psycopg` added | secret |
| `PUBLIC_BASE_URL` | `https://YOUR-SERVICE.onrender.com` | your Render URL, known after first deploy |
| `GOOGLE_CLIENT_SECRETS_FILE` | `credentials.json` | see step 5 |
| `GOOGLE_OAUTH_REDIRECT_URI` | `https://YOUR-SERVICE.onrender.com/auth/google/callback` | must exactly match what's registered in Google Cloud Console |
| `GOOGLE_PUBSUB_TOPIC` | `projects/YOUR_PROJECT_ID/topics/gmail-email-events` | from Google Cloud Pub/Sub |
| `GOOGLE_PUBSUB_AUDIENCE` | `https://YOUR-SERVICE.onrender.com/webhooks/google/pubsub` | |
| `TOKEN_ENCRYPTION_KEY` | output of `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` | secret — generate once, never regenerate after real tokens are stored (see step 7) |
| `MCP_API_KEY` | any long random string, e.g. `python -c "import secrets;print(secrets.token_urlsafe(32))"` | secret — used to protect `/mcp` |
| `WATCH_RENEWAL_DAYS` | `6` | matches `.env.example` default |

`app/config.py` reads these via `pydantic-settings`, so the names must match exactly (case-insensitive) — `DATABASE_URL`, `PUBLIC_BASE_URL`, etc.

## 5. Get `credentials.json` onto the server

`credentials.json` is your Google OAuth client secret file — it's not an env var, it's a file the app reads at `google_client_secrets_file` path. Since it shouldn't be committed to Git, you have two options:

**Option A — Secret File (recommended):**
Render Dashboard → your web service → **Environment** → **Secret Files** → add a file named `credentials.json`, paste its JSON contents. Render mounts it at `/etc/secrets/credentials.json` at runtime, so also set:
```
GOOGLE_CLIENT_SECRETS_FILE=/etc/secrets/credentials.json
```

**Option B — Bake it into the image at build time** (only if your repo is private and you're comfortable with it living in Git): commit `credentials.json` and remove it from `.gitignore`. Not recommended for a public repo.

## 6. First deploy and verify

1. Click **Create Web Service** — Render will build, run `alembic upgrade head` against the new database, and start uvicorn.
2. Check `https://YOUR-SERVICE.onrender.com/health` → `{"status":"ok"}`.
3. Check `https://YOUR-SERVICE.onrender.com/docs` loads.
4. In Render's **Shell** tab (or a one-off job), sanity-check the DB connection:
   ```bash
   python -c "from app.database import engine; from sqlalchemy import text; print(engine.connect().execute(text('select 1')).scalar())"
   ```
   Expect `1`.

## 7. Connect Google OAuth and Pub/Sub to the live URL

1. Google Cloud Console → your OAuth client → add `https://YOUR-SERVICE.onrender.com/auth/google/callback` to **Authorized redirect URIs**.
2. Visit `https://YOUR-SERVICE.onrender.com/auth/google` once, sign in, and approve the readonly Gmail scope. This performs the initial sync and stores the encrypted refresh token in your new Postgres database.
3. Create the Pub/Sub push subscription (in Google Cloud Console) pointing at:
   ```
   https://YOUR-SERVICE.onrender.com/webhooks/google/pubsub
   ```
4. Send a test email to the connected account and confirm a row appears via `GET /emails`.

## 8. Watch renewal (separate Cron Job)

Gmail watches expire (~7 days). Add a second Render resource — **New → Cron Job** — pointed at the same repo, running something like:

```bash
python -c "
from app.database import SessionLocal
from app.models.email import GmailAccount
from app.services.oauth_service import decrypt_credentials
from app.services.gmail_service import gmail
from app.services.sync_service import renew_watch
from app.config import settings
from datetime import datetime, timedelta, timezone

db = SessionLocal()
cutoff = datetime.now(timezone.utc) + timedelta(hours=24)
for account in db.query(GmailAccount).filter(
    (GmailAccount.watch_expiration == None) | (GmailAccount.watch_expiration < cutoff)
).all():
    renew_watch(db, account, gmail(decrypt_credentials(account.encrypted_token)), settings.google_pubsub_topic)
db.close()
"
```

Give the Cron Job the **same environment variables** (`DATABASE_URL`, `TOKEN_ENCRYPTION_KEY`, etc.) as the web service — it needs to reach the same database and decrypt the same tokens. Schedule: `0 */12 * * *` (every 12 hours) is a reasonable default.

## Where your data actually lives

- **Postgres (Render-managed):** email metadata/body (`emails` table) and encrypted OAuth refresh tokens (`gmail_accounts.encrypted_token`). Encrypted at rest with `TOKEN_ENCRYPTION_KEY` before it ever reaches the DB — Postgres itself never sees a plaintext token.
- **Render env vars / Secret Files:** `TOKEN_ENCRYPTION_KEY`, `MCP_API_KEY`, `credentials.json`, `DATABASE_URL`. These never touch Git.
- **Nothing is stored on local disk on Render** beyond the ephemeral container filesystem — a redeploy wipes it, which is fine since all persistent state is in Postgres.

## Key rotation warning

If you ever regenerate `TOKEN_ENCRYPTION_KEY` after real Gmail tokens are already stored, every existing `encrypted_token` becomes undecryptable — that's exactly the `InvalidToken` case now caught by `decrypt_credentials()` and surfaced as an HTTP 500 from `/webhooks/google/pubsub`. If you must rotate the key, re-run the OAuth flow (`/auth/google`) for every connected account first so tokens are re-encrypted under the new key.