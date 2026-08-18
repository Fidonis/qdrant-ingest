# syntax=docker/dockerfile:1

# ---- builder: resolve and install dependencies into an isolated venv ----
FROM python:3.12-slim AS builder

# Pinned uv release so dependency resolution is reproducible across rebuilds.
COPY --from=ghcr.io/astral-sh/uv:0.9.24 /uv /usr/local/bin/uv

# UV_LINK_MODE=copy avoids hardlink warnings when the cache and target differ.
# UV_PYTHON_DOWNLOADS=never forces use of the interpreter from the base image.
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

# Copy only the dependency manifests first so this layer stays cached when
# application code changes but dependencies do not.
COPY src/pyproject.toml src/uv.lock ./

# --frozen fails the build if uv.lock is out of sync with pyproject.toml,
# guaranteeing the image is built against pinned, reproducible versions.
RUN uv sync --frozen --no-dev

# ---- assets: download single-file frontend libraries ----
#
# fidonis-brand: 1 -- this stage and the css-builder below are vendored from
# Fidonis/papaia-manager. Keep the pins in step across both interfaces.
FROM python:3.12-slim AS assets

# Versions are pinned; update deliberately (not via Dependabot, since these
# are vendored into the image rather than declared as package dependencies).
ARG HTMX_VERSION=2.0.4
ARG ALPINE_VERSION=3.14.9

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /static \
    && curl -fsSL \
        "https://unpkg.com/htmx.org@${HTMX_VERSION}/dist/htmx.min.js" \
        -o /static/htmx.min.js \
    && curl -fsSL \
        "https://unpkg.com/alpinejs@${ALPINE_VERSION}/dist/cdn.min.js" \
        -o /static/alpine.min.js

# ---- css-builder: compile Tailwind + daisyUI (Node only in build, not runtime) ----
FROM node:20-slim AS css-builder

WORKDIR /build

# Pin versions so the CSS output is reproducible.
RUN npm install --save-dev tailwindcss@3.4.17 daisyui@4.12.23 2>/dev/null

# Vendored brand fonts (self-hosted, no runtime calls to Google Fonts).
# Fontsource ships pinned, npm-versioned woff2 files + matching @font-face
# CSS, so exact file bytes are reproducible across rebuilds -- same intent as
# the pinned htmx/alpine downloads in the `assets` stage above.
# Both pins below are confirmed published versions (`npm view @fontsource/…`).
ARG FONTSOURCE_MANROPE_VERSION=5.1.1
ARG FONTSOURCE_IBM_PLEX_SANS_VERSION=5.1.1
RUN npm install --no-save \
        @fontsource/manrope@${FONTSOURCE_MANROPE_VERSION} \
        @fontsource/ibm-plex-sans@${FONTSOURCE_IBM_PLEX_SANS_VERSION} \
    && mkdir -p /static/fonts/files \
    && cp node_modules/@fontsource/manrope/files/manrope-latin-500-normal.woff2 \
          node_modules/@fontsource/manrope/files/manrope-latin-700-normal.woff2 \
          node_modules/@fontsource/manrope/files/manrope-latin-800-normal.woff2 \
          node_modules/@fontsource/ibm-plex-sans/files/ibm-plex-sans-latin-400-normal.woff2 \
          node_modules/@fontsource/ibm-plex-sans/files/ibm-plex-sans-latin-500-normal.woff2 \
          node_modules/@fontsource/ibm-plex-sans/files/ibm-plex-sans-latin-600-normal.woff2 \
          /static/fonts/files/

# Tailwind config: scan all Jinja2 templates for class names.
COPY docker/tailwind.config.js ./tailwind.config.js
COPY docker/tailwind.brand.css ./tailwind.brand.css
COPY docker/tailwind.app.css ./tailwind.app.css
COPY src/ui/templates/ ./templates/

# The brand layer and the application shell are separate files so the brand
# half can be diffed against papaia-manager; Tailwind takes one input, so they
# are concatenated here. Order matters: the @tailwind directives lead.
RUN cat tailwind.brand.css tailwind.app.css > tailwind.input.css \
    && npx tailwindcss \
        -c tailwind.config.js \
        -i tailwind.input.css \
        -o /static/app.css \
        --minify

# ---- runtime: minimal image with the venv, rclone, and the application ----
FROM python:3.12-slim AS runtime

# PATH puts the venv first; the two PYTHON* vars keep the container quiet and
# stateless (no .pyc files, unbuffered stdout for predictable log capture).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# rclone runs as a subprocess at the start of each ingestion run, inside the
# run's causal chain. Copying the pinned static binary keeps source
# synchronisation reproducible with the image itself.
COPY --from=rclone/rclone:1.71.1 /usr/local/bin/rclone /usr/local/bin/rclone

# The ingester needs no root capabilities; run it as an unprivileged user.
#
# The uid is fixed rather than system-assigned because the container now writes
# to a host bind mount: the job catalog lives in $PAPAIA_CONFIG_DIR, owned by
# the host user that ran setup. Compose passes that host uid/gid through
# `user:`, and 1000 is the common case it defaults to.
#
# The /data mount points are pre-created so named volumes inherit their
# ownership on first use, and made group-writable so a deployment whose host
# uid is not 1000 can still write to them after overriding `user:`.
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --gid ${APP_GID} app \
    && useradd --uid ${APP_UID} --gid ${APP_GID} --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /data/cache /data/state /data/local \
    && chown -R app:app /data \
    && chmod -R 0775 /data

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY src/ /app/

# The web interface's static assets, built above. Absent at development time,
# which is why asset_url() falls back to an unfingerprinted path rather than
# failing: a missing stylesheet must not take the interface down.
COPY --from=assets /static/ /app/ui/static/
COPY --from=css-builder /static/app.css /app/ui/static/app.css
COPY --from=css-builder /static/fonts/ /app/ui/static/fonts/

USER app

# Ownership marker read by the MCP Registry at publish time; the value MUST
# equal the `name` field in server.json or the publish is rejected.
LABEL io.modelcontextprotocol.server.name="de.fidonis/qdrant-ingest"

# REST control plane, MCP endpoint and web interface share one port
# (see QI_HTTP_PORT). Only /ui is meant to be reachable through a proxy.
EXPOSE 8300

# /health is auth-exempt. sh -c lets the healthcheck read QI_HTTP_PORT at
# runtime so the check stays correct when the port is changed.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["sh", "-c", "python -c 'import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen(\"http://localhost:\"+os.environ.get(\"QI_HTTP_PORT\",\"8300\")+\"/health\",timeout=3).status==200 else 1)'"]

# main.py resolves its flat imports relative to /app.
CMD ["python", "main.py"]
