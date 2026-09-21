FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Create startup script
RUN printf '%s\n' '#!/bin/bash' 'set -euo pipefail' ': "${DATABASE_URL:?DATABASE_URL must be configured with the Render PostgreSQL connection URL}"' 'case "$DATABASE_URL" in *"@localhost:"*|*"@127.0.0.1:"*) echo "DATABASE_URL points to localhost; configure Render PostgreSQL Internal Database URL" >&2; exit 1;; esac' 'alembic upgrade head' 'exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-10000}"' > /app/startup.sh && chmod +x /app/startup.sh
CMD ["/app/startup.sh"]
