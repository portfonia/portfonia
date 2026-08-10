FROM python:3.12-slim AS builder

WORKDIR /build

# No build-essential: every dependency in requirements.txt ships a prebuilt
# manylinux/aarch64 wheel (verified against actual pip download output) —
# adding a C toolchain here only costs image size and, on memory-constrained
# build hosts, risks apt-get itself being OOM-killed for no benefit.
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


FROM python:3.12-slim AS runtime

# postgresql-client-16 (pg_dump, matching the postgres:16-alpine server in
# docker-compose.yml) + an OCI CLI, ISOLATED in its own venv at /opt/oci-cli —
# oci-cli pins cryptography<50.0.0, which conflicts with this app's
# cryptography==50.0.0 pin (Fernet holdings encryption, issue #31). Keeping
# both binaries out of /venv (the app's requirements.txt venv) avoids that
# conflict entirely; app/services/db_backup.py shells out to both via
# subprocess rather than importing either as a library. See issue #106.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg ca-certificates \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc --fail \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(. /etc/os-release && echo $VERSION_CODENAME)-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends postgresql-client-16 \
    && python3 -m venv /opt/oci-cli \
    && /opt/oci-cli/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/oci-cli/bin/pip install --no-cache-dir oci-cli==3.81.0 \
    && ln -s /opt/oci-cli/bin/oci /usr/local/bin/oci \
    && apt-get purge -y curl gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system portfonia && useradd --system --gid portfonia --create-home portfonia

WORKDIR /app

COPY --from=builder /venv /venv
ENV PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY app/ ./app/
COPY alembic/ ./alembic/
COPY alembic.ini pyproject.toml ./
COPY config/ ./config/

RUN chown -R portfonia:portfonia /app
USER portfonia

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
