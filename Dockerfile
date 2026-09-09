FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN alembic upgrade head
COPY . .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "10000"]