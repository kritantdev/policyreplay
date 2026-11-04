FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY templates templates
RUN useradd -u 10001 -M -s /usr/sbin/nologin appuser
USER 10001
# Same image for both builds: choose with -e BUG_PROFILE=buggy|correct.
# Root filesystem is read-only at run time; the SQLite DB lives on a tmpfs at /data.
ENV BUG_PROFILE=correct DB_PATH=/data/app.db
EXPOSE 9000
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9000", "--log-level", "warning"]
