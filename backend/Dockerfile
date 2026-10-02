# Backend image: the FastAPI admin API and the Telegram bot worker run from the same image
# (docker-compose picks the command). Build context: ./backend
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini ./
COPY app ./app
COPY migrations ./migrations

# unprivileged user; /data/uploads holds the dispute screenshots (shared volume between api and bot)
RUN useradd --system --uid 10001 --home-dir /app app \
    && mkdir -p /data/uploads \
    && chown -R app:app /data
USER app

ENV UPLOAD_DIR=/data/uploads
EXPOSE 8000

CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
