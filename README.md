# inbox_enhanced

LLM-classified Gmail inbox with custom buckets. FastAPI + Celery + Postgres + Redis on the back, Vite + React on the front.

## Running locally

### Prerequisites

You need these installed and (where noted) running:

- **Docker Desktop** — must be running. Provides Postgres + Redis via `docker-compose.yml`.
- **uv** — Python package/runtime manager. Install: `brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`. Manages the Python 3.13 toolchain and server deps.
- **bun** — JS runtime + package manager for the client. Install: `brew install oven-sh/bun/bun` or `curl -fsSL https://bun.sh/install | bash`.
- **A Google Cloud OAuth 2.0 Web client** — from console.cloud.google.com → APIs & Services → Credentials. Add `http://localhost:8000/auth/callback` as an authorized redirect URI. Enable the Gmail API for the project.
- **An Anthropic API key** — from console.anthropic.com. Used by the classifier and bucket-draft preview.

### 1. Configure `.env`

Copy the template and fill in the placeholders:

```bash
cp .env.example .env
```

Required values in `.env`:

| Var | How to get it |
|---|---|
| `DATABASE_URL` | Leave the default (`postgresql+psycopg://inbox:inbox@localhost:5432/inbox`) — it matches `docker-compose.yml`. |
| `REDIS_URL` | Leave the default (`redis://localhost:6379/0`) — it matches `docker-compose.yml`. |
| `SESSION_SECRET` | `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `ENCRYPTION_KEY` | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` (Fernet key — encrypts stored Gmail refresh tokens; rotating it strands every existing session). |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | From your Google OAuth web client. |
| `GOOGLE_REDIRECT_URI` | `http://localhost:8000/auth/callback` for local. Must exactly match what's registered in the Google console. |
| `ANTHROPIC_API_KEY` | From console.anthropic.com. |
| `SESSION_TTL_SECONDS` | Default `2592000` (30 days) is fine. |
| `ANTHROPIC_CLASSIFY_MODEL` | Default `claude-haiku-4-5` is fine. |
| `ANTHROPIC_CONCURRENCY` | Default `16` is fine. |

Leave `COOKIE_DOMAIN` unset for local dev.

### 2. Install dependencies

```bash
# Server (Python 3.13 toolchain + deps; uv reads server/pyproject.toml)
cd server && uv sync && cd ..

# Client
cd client && bun install && cd ..
```

### 3. Start everything

From the repo root:

```bash
./scripts/dev.sh
```

This script:

1. Boots Postgres + Redis via `docker compose up -d`.
2. Runs `alembic upgrade head` against Postgres.
3. Starts the FastAPI backend on **:8000**.
4. Starts a Celery worker (poll/reclassify/draft-preview tasks).
5. Starts Celery beat (periodic poll fan-out).
6. Starts the Vite dev server on **:5173** (proxies `/auth` and `/api` to :8000).

Ctrl-C stops all five.

### 4. Open the app

Navigate to **http://localhost:5173**. Click "Sign in with Google" — the OAuth round-trip lands you back on the inbox view.

### Ports used

`5173` (client), `8000` (API), `5432` (Postgres), `6379` (Redis). If any are taken, free them or edit `docker-compose.yml` + `vite.config.ts` + the dev script accordingly.

## Links

- https://github.com/Jmemon/inbox_concierge/
- https://googleapis.github.io/google-api-python-client/docs/dyn/gmail_v1.html
- https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.threads
- https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.history/list
- https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages
