# Build from the repo root:  docker build -t pr-worker -f worker/Dockerfile .
FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /srv
RUN pip install --no-cache-dir playwright==1.56.0
RUN playwright install --with-deps chromium && chmod -R a+rX /ms-playwright
COPY worker/__init__.py worker/run_plan.py worker/
RUN useradd -u 10001 -M -s /usr/sbin/nologin worker
USER 10001
ENV HOME=/tmp
ENTRYPOINT ["python", "-m", "worker.run_plan"]
