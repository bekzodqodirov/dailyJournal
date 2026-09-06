# syntax=docker/dockerfile:1
FROM python:3.12-slim

# PIP_DEFAULT_TIMEOUT: pip's default 15-second socket timeout gives up on a
# slow link in the middle of a wheel, and several of these are hundreds of
# megabytes. PIP_RETRIES then makes a dropped connection resume rather than
# fail the whole build.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Tashkent \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10

WORKDIR /app

# ffmpeg: video-note / voice audio extraction. tzdata: Asia/Tashkent.
# age: encrypts the nightly dump before it touches the disk (spec §10).
# postgresql-client: pg_dump for that backup.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg tzdata curl ca-certificates age postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# pg_dump refuses to dump from a *newer* server, so the client major must be
# >= the pgvector/pg16 container's. Debian's own client is whatever the base
# image's release shipped — new enough on trixie (17), too old on bookworm
# (15), where every nightly backup would fail. So try PGDG for an exactly
# matching client-16.
#
# Deliberately non-fatal: apt-get update dies on an unreachable or
# non-existent suite, and a build that fails here would take down the entire
# deployment to fix a backup. The Debian client installed above stays as the
# fallback, and a version mismatch surfaces loudly — pg_dump's own error is
# forwarded to the owner in Telegram by the backup job.
RUN set -eu; \
    key=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc; \
    . /etc/os-release; \
    if install -d /usr/share/postgresql-common/pgdg \
        && curl -fsSL --retry 3 https://www.postgresql.org/media/keys/ACCC4CF8.asc -o "$key" \
        && echo "deb [signed-by=$key] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
            > /etc/apt/sources.list.d/pgdg.list \
        && apt-get update \
        && apt-get install -y --no-install-recommends postgresql-client-16; then \
        echo "pg_dump: using postgresql-client-16 from PGDG"; \
    else \
        echo "pg_dump: PGDG unavailable, keeping the distribution client" >&2; \
        rm -f /etc/apt/sources.list.d/pgdg.list; \
    fi; \
    rm -rf /var/lib/apt/lists/*; \
    pg_dump --version

COPY requirements.txt ./

# torch comes first, from PyTorch's CPU index. The default PyPI wheel for
# Linux is the CUDA build: 527 MB of torch plus ~800 MB of nvidia-* runtime
# libraries that a CPU-only host will never load. Installing the CPU build
# first means sentence-transformers finds torch already satisfied and never
# reaches for the CUDA one — about 1.1 GB less to download.
#
# Non-fatal on purpose: if that index is unreachable, requirements.txt below
# still installs a working (merely fatter) torch rather than failing the build.
#
# The cache mount is what makes a slow connection survivable: a build that
# dies half way keeps every wheel it already fetched, so the retry resumes
# instead of starting over. It is also why PIP_NO_CACHE_DIR is not set.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install \
        --index-url https://download.pytorch.org/whl/cpu \
        --extra-index-url https://pypi.org/simple \
        torch \
    || echo "CPU torch index unreachable - falling back to the default wheel" >&2

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

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
