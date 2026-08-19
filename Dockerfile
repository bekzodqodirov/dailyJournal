FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Tashkent

WORKDIR /app

# ffmpeg: video-note / voice audio extraction. tzdata: Asia/Tashkent.
# age: encrypts the nightly dump before it touches the disk (spec §10).
# postgresql-client-16 comes from PGDG, pinned to the server's major:
# Debian's own postgresql-client is v15 here, and pg_dump refuses to dump
# from a *newer* server — the stock package would fail every nightly backup
# against the pgvector/pg16 container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg tzdata curl ca-certificates age \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && . /etc/os-release \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc]" \
        "https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY miya ./miya

# Run as a non-root user; the app never needs to write to its own code.
# The huggingface cache dir must exist *in the image*, owned by miya, so the
# named volume mounted there inherits that ownership — otherwise Docker
# creates it root-owned and the bge-m3 download fails (or, mounted at
# /root/.cache, is simply never used and ~2 GB re-downloads every rebuild).
RUN useradd --create-home --uid 10001 miya \
    && mkdir -p /data/call_recordings /data/backups \
        /home/miya/.cache/huggingface \
    && chown -R miya:miya /data /home/miya/.cache
USER miya

EXPOSE 8000

CMD ["uvicorn", "miya.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
