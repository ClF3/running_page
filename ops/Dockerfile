ARG NODE_IMAGE=node:22-bookworm-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
FROM ${NODE_IMAGE} AS node
FROM ${PYTHON_IMAGE}
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client ca-certificates time libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global pnpm@8.9.0
WORKDIR /app
COPY ops/requirements.lock /app/ops/requirements.lock
RUN pip install --no-cache-dir --prefer-binary -r ops/requirements.lock
COPY package.json pnpm-lock.yaml /app/
# pnpm 8 counts os.cpus(), which ignores CPU affinity. PNPM_WORKERS is the
# number of idle CPUs, so subtracting all but two leaves one import worker.
RUN --mount=type=cache,target=/pnpm/store \
    PNPM_WORKERS="$(node -p 'Math.max(0, require("os").cpus().length - 2)')" \
    NODE_OPTIONS=--max-old-space-size=384 pnpm install --frozen-lockfile \
    --store-dir=/pnpm/store --network-concurrency=4 --child-concurrency=1
COPY . /app
# OpenSSH requires a passwd entry even when Docker already runs with this UID.
ARG RUNNER_UID=1000
ARG RUNNER_GID=1000
RUN if ! getent group "$RUNNER_GID" >/dev/null; then groupadd --gid "$RUNNER_GID" running-page; fi \
    && if ! getent passwd "$RUNNER_UID" >/dev/null; then \
         useradd --uid "$RUNNER_UID" --gid "$RUNNER_GID" --no-create-home \
           --no-log-init --home-dir /tmp --shell /usr/sbin/nologin running-page; \
       fi
ARG SOURCE_REVISION=uncommitted
ENV RUNNING_PAGE_SOURCE_REVISION=${SOURCE_REVISION} \
    PYTHONPATH=/app:/app/run_page \
    TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    GARTH_TELEMETRY_ENABLED=false \
    LOGFIRE_SEND_TO_LOGFIRE=false \
    WRANGLER_SEND_METRICS=false \
    RUNNING_PAGE_CONFIG=/config/config.json
ENTRYPOINT ["python", "-m", "run_page.automation"]
