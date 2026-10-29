FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --root-user-action=ignore -r requirements.txt \
    && useradd --uid 10001 --no-create-home sherlog
COPY sherlog/ ./sherlog/

USER sherlog
CMD ["uvicorn", "sherlog.api.app:app", "--host", "0.0.0.0", "--port", "8200"]
