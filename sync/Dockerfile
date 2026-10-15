FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sync.py .

# Runs as an unprivileged user. Match UID/GID to the owner of the state,
# outbox and certs folders on the host (see SETUP.md).
ARG UID=1000
ARG GID=1000
RUN groupadd -g ${GID} sync && useradd -u ${UID} -g ${GID} -M -s /usr/sbin/nologin sync
USER sync

ENTRYPOINT ["python", "sync.py"]
CMD ["run"]
