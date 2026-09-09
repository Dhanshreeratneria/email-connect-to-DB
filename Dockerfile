FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Create startup script
RUN echo '#!/bin/bash\nif [ ! -z "$DATABASE_URL" ]; then alembic upgrade head; fi\nuvicorn app.main:app --host 0.0.0.0 --port 10000' > /app/startup.sh && chmod +x /app/startup.sh
CMD ["/app/startup.sh"]
