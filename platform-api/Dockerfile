FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GIT_DIR=/repo.git

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory '*'

WORKDIR /app
COPY requirements.txt .
RUN pip install --root-user-action=ignore -r requirements.txt
COPY platform_api/ ./platform_api/

# Needs the Docker socket (root) to manage demo containers.
CMD ["uvicorn", "platform_api.main:app", "--host", "0.0.0.0", "--port", "8050"]
