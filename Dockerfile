FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY plowwhip/ ./plowwhip/

RUN mkdir /data && chown 65534:65534 /data
USER 65534:65534

EXPOSE 8742
HEALTHCHECK --interval=2s --timeout=2s --start-period=3s --retries=10 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8742/health', timeout=1).read()"]
STOPSIGNAL SIGINT
CMD ["python", "-m", "plowwhip", "--db", "/data/plowwhip.db", "--data-root", "/data", "serve", "--host", "0.0.0.0", "--port", "8742", "--allow-non-loopback"]
