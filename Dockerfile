FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Tashkent

WORKDIR /app

# ffmpeg: video-note / voice audio extraction. tzdata: Asia/Tashkent.
# postgresql-client: pg_dump for the nightly backup. age: encrypts that dump
# before it touches the disk (spec §10).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg tzdata curl postgresql-client age \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY miya ./miya

# Run as a non-root user; the app never needs to write to its own code.
RUN useradd --create-home --uid 10001 miya \
    && mkdir -p /data/call_recordings /data/backups \
    && chown -R miya:miya /data
USER miya

EXPOSE 8000

CMD ["uvicorn", "miya.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
