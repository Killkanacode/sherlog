FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --root-user-action=ignore -r requirements.txt
COPY chaos_controller/ ./chaos_controller/

# Needs the Docker socket (root) to kill, pause and stop containers.
CMD ["uvicorn", "chaos_controller.main:app", "--host", "0.0.0.0", "--port", "8100"]
