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
