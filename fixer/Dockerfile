# Sherlog's fixer. Two users: root runs the service (it holds the GitHub
# token); `agent` runs Muse Code and every test, with a clean environment,
# inside Muse Code's bubblewrap sandbox (no network for its commands).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10002 agent

# The shop's dependencies plus pytest, so tests run the way the shop does.
COPY --from=demoapp requirements.txt /tmp/demo-app-requirements.txt
RUN pip install --root-user-action=ignore -r /tmp/demo-app-requirements.txt pytest==8.4.2 pytest-asyncio==1.2.0

# Muse Code, installed for the agent user with Meta's official installer.
USER agent
RUN curl -fsSL https://dev.meta.ai/install.sh | MUSE_NO_MODIFY_PATH=1 bash \
    && ~/.local/bin/muse --version
USER root

WORKDIR /srv
COPY requirements.txt .
RUN pip install --root-user-action=ignore -r requirements.txt
COPY fixer/ ./fixer/
RUN mkdir -p /work && chmod 711 /work

# asyncio, not uvloop: uvloop cannot start the agent's processes as another user.
CMD ["uvicorn", "fixer.app:app", "--host", "0.0.0.0", "--port", "8400", "--loop", "asyncio"]
