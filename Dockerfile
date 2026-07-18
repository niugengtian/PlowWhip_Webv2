FROM node:22-alpine AS web-build

WORKDIR /build/web
RUN corepack enable
COPY web/package.json web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm run build

FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLOW_WHIP_EMBEDDED_CRON=1 \
    PLOW_WHIP_CONTAINER_LOOPBACK=1 \
    TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates git openssh-client tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system plowwhip \
    && useradd --system --gid plowwhip --home-dir /app --shell /usr/sbin/nologin plowwhip

ARG PLOW_WHIP_RELEASE_SHA=unknown
ARG PLOW_WHIP_PYPI_INDEX_URL=https://pypi.org/simple
LABEL org.opencontainers.image.revision="${PLOW_WHIP_RELEASE_SHA}"

WORKDIR /app
COPY pyproject.toml README.md ./
COPY backend/ ./backend/
COPY --from=web-build /build/web/dist/ ./backend/plow_whip_web/static/
RUN python -m pip install --no-cache-dir \
        --index-url "${PLOW_WHIP_PYPI_INDEX_URL}" . \
    && mkdir -p /data /projects \
    && chown -R plowwhip:plowwhip /app /data /projects

USER plowwhip
VOLUME ["/data", "/projects"]
EXPOSE 8742

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8742/health', timeout=3).read()"]

ENTRYPOINT ["python", "-m", "plow_whip_web"]
CMD ["--host", "0.0.0.0", "--port", "8742", "--data-dir", "/data", "--embedded-cron", "serve"]
