# syntax=docker/dockerfile:1
FROM python:3.14-slim@sha256:22eafb370920ad8b429e0aec3fff433e0669968519f5d60469d83729c138b28b AS build
ENV COPILOT_CLI_EXTRACT_DIR=/opt/copilot
COPY requirements.lock /tmp/requirements.lock
RUN python -m venv --without-pip /opt/venv \
 && grep -Eiv '^(build|iniconfig|packaging|pluggy|pygments|pyproject_hooks|pytest(-asyncio)?|ruff)==' /tmp/requirements.lock > /tmp/runtime.lock \
 && python -m pip --python /opt/venv install --no-cache-dir --only-binary=:all: -r /tmp/runtime.lock \
 && /opt/venv/bin/python -m copilot download-runtime \
 && chmod -R a+rX /opt/copilot \
 && find /opt/venv /opt/copilot -type d -name __pycache__ -prune -exec rm -rf {} +

FROM python:3.14-slim@sha256:22eafb370920ad8b429e0aec3fff433e0669968519f5d60469d83729c138b28b
LABEL org.opencontainers.image.source="https://github.com/rcarmo/ms-durable"
ENV PATH=/opt/venv/bin:$PATH \
    COPILOT_CLI_EXTRACT_DIR=/opt/copilot \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/agent MS_DURABLE_HOST=0.0.0.0 \
    MS_DURABLE_DATA=/state MS_DURABLE_CWD=/work
RUN mkdir -p /state /work /home/agent \
 && chown 10001:10001 /state /work /home/agent
COPY --from=build /opt/venv /opt/venv
COPY --from=build /opt/copilot /opt/copilot
WORKDIR /app
COPY agent/ agent/
COPY static/ static/
COPY LICENSE ./
USER 10001:10001
EXPOSE 8015
CMD ["python", "-m", "agent"]
