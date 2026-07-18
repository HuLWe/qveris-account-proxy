FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY requirements.lock ./
RUN python -m pip install --require-hashes --no-deps -r requirements.lock

FROM python:3.13-slim AS runtime

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH="/app/src" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TMPDIR=/tmp

RUN groupadd --system --gid 10001 qveris \
    && useradd --system --uid 10001 --gid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin qveris \
    && mkdir -p /app/src /data \
    && chown -R 10001:10001 /app /data

COPY --from=builder /opt/venv /opt/venv
COPY --chown=10001:10001 src /app/src
COPY --chown=10001:10001 LICENSE /app/LICENSE

USER 10001:10001
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/live', timeout=2).read()"]

CMD ["uvicorn", "qveris_proxy.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
