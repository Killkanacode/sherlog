# One image per service: docker build --build-arg SERVICE=orders --build-arg VERSION=<ref> .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --root-user-action=ignore -r requirements.txt \
    && useradd --uid 10001 --no-create-home app

ARG SERVICE
ARG VERSION=dev
ENV SERVICE=${SERVICE} \
    SERVICE_VERSION=${VERSION}

COPY common/ ./common/
COPY db/ ./db/
COPY ${SERVICE}/ ./${SERVICE}/

USER app
HEALTHCHECK --interval=10s --timeout=4s --start-period=60s --start-interval=2s --retries=3 \
    CMD ["python", "-m", "common.healthcheck"]
CMD ["sh", "-c", "exec uvicorn \"${SERVICE}.main:app\" --host 0.0.0.0 --port \"${PORT:-8000}\" --no-access-log"]
