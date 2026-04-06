# ─────────────────────────────────────────────────────────────────────────────
# gh-tf-action — GitHub Action for Terraform with rich plan visualization
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim

LABEL maintainer="subzone"
LABEL description="Terraform GitHub Action — plan visualization, PR comments, multi-cloud backends"
LABEL org.opencontainers.image.source="https://github.com/subzone/gh-tf-action"

ENV DEBIAN_FRONTEND=noninteractive

# ── System dependencies ───────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    unzip \
    git \
    ca-certificates \
    jq \
    && rm -rf /var/lib/apt/lists/*

# ── Python dependencies ───────────────────────────────────────────────────────
RUN pip install --no-cache-dir requests

# ── Copy action source ────────────────────────────────────────────────────────
WORKDIR /action
COPY src/ /action/src/
COPY entrypoint.sh /action/entrypoint.sh
RUN chmod +x /action/entrypoint.sh

ENTRYPOINT ["/action/entrypoint.sh"]
