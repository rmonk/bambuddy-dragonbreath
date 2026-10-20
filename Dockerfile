FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
# Dependencies are pinned by hash (generated with scripts/lock.sh through
# safe-chain), so the image installs exactly what was vetted.
COPY requirements.lock .
RUN pip install --require-hashes --no-deps -r requirements.lock

COPY src ./src
ENV PYTHONPATH=/app/src BDB_CONFIG=/config/config.yaml

RUN useradd --system --uid 10001 app
USER app

ENTRYPOINT ["python", "-m", "bambuddy_dragonbreath"]
CMD ["run"]
