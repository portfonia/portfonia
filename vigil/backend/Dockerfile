FROM python:3.12-slim AS builder

WORKDIR /build

RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


FROM python:3.12-slim AS runtime

RUN groupadd --system vigil && useradd --system --gid vigil --create-home vigil

WORKDIR /app

COPY --from=builder /venv /venv
ENV PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

COPY vigil_app/ ./vigil_app/
COPY alembic/ ./alembic/
COPY alembic.ini pyproject.toml ./

RUN chown -R vigil:vigil /app
USER vigil

EXPOSE 8000

CMD ["uvicorn", "vigil_app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
