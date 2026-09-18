# =============================================================================
# Worker Production Dockerfile - Multi-Stage Build
# =============================================================================
# Optimiert für:
# - Multi-Platform (amd64 + arm64)
# - Kleines Image (kein Poetry im Runtime)
# - Reproduzierbare Builds
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: Builder - Poetry installiert Dependencies
# -----------------------------------------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /app

# Poetry installieren
ENV POETRY_HOME="/opt/poetry" \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    POETRY_NO_INTERACTION=1

RUN pip install --no-cache-dir poetry

# Dependencies installieren (ohne Dev-Dependencies)
COPY pyproject.toml poetry.lock* ./
RUN poetry install --no-root --only=main --no-ansi

# -----------------------------------------------------------------------------
# Stage 2: Runtime - Schlankes Production Image
# -----------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

# Build arguments für Multi-Platform Support
ARG TARGETARCH
ARG TERRAFORM_VERSION=1.16.3
ARG PACKER_VERSION=1.16.0

WORKDIR /app

# System Dependencies. ``apt-get upgrade`` runs first so the base
# ``python:3.11-slim`` tag picks up Debian-security backports released
# after the upstream image was last rebuilt — that's where things like
# CVE-2026-45447 (openssl 3.5.6-1~deb13u2) come from. Trivy blocks the
# push on any HIGH/CRITICAL OS finding, so even though it enlarges the
# layer slightly we'd rather take the bytes than burn a .trivyignore
# line every time upstream debian releases a CVE-fix.
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    git \
    wget \
    unzip \
    curl \
    ca-certificates \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Upgrade base-image Python tooling (pip / setuptools / wheel) to pull
# in security fixes that the upstream `python:3.11-slim` tag hasn't
# picked up yet. Trivy scans these system site-packages — anything
# HIGH/CRITICAL here blocks the push.
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# OpenStack CLI installieren
RUN pip install --no-cache-dir python-openstackclient

# Terraform installieren (platform-aware)
RUN ARCH="${TARGETARCH:-amd64}" && \
    echo "Installing Terraform ${TERRAFORM_VERSION} for ${ARCH}" && \
    wget -q https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${ARCH}.zip && \
    unzip -qo terraform_${TERRAFORM_VERSION}_linux_${ARCH}.zip && \
    mv terraform /usr/local/bin/ && \
    rm -f terraform_${TERRAFORM_VERSION}_linux_${ARCH}.zip LICENSE.txt && \
    terraform --version

# Packer installieren (platform-aware)
RUN ARCH="${TARGETARCH:-amd64}" && \
    echo "Installing Packer ${PACKER_VERSION} for ${ARCH}" && \
    wget -q https://releases.hashicorp.com/packer/${PACKER_VERSION}/packer_${PACKER_VERSION}_linux_${ARCH}.zip && \
    unzip -qo packer_${PACKER_VERSION}_linux_${ARCH}.zip && \
    mv packer /usr/local/bin/ && \
    rm -f packer_${PACKER_VERSION}_linux_${ARCH}.zip LICENSE.txt && \
    packer --version

# Virtual Environment vom Builder kopieren
COPY --from=builder /app/.venv /app/.venv

# Drop pip from both interpreters, after the OpenStack CLI above has been
# installed with it. pip ships a CycloneDX SBOM of its vendored libraries at
# `pip/_vendor/bom.cdx.json`, and Trivy reads that as if those libraries were
# installed — reporting versions that exist only inside pip and never run.
# Upgrading pip does not help; those are the versions current pip vendors.
#
# Both paths are spelled out because PATH puts the venv first, so a bare
# `python` would only ever reach one of the two.
RUN /app/.venv/bin/python -m pip uninstall -y pip && \
    /usr/local/bin/python -m pip uninstall -y pip

# Application Code kopieren
COPY app/ ./app/

# Arbeitsverzeichnis für Worker
RUN mkdir -p /tmp/worker_repos

# Environment für .venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Health check (optional - prüft ob Celery läuft)
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD celery -A app.celery_app inspect ping -d celery@$HOSTNAME || exit 1

# Celery Worker starten
CMD ["celery", "-A", "app.celery_app", "worker", "--loglevel=info", "--autoscale=2,20", "-E", "--prefetch-multiplier=1"]
