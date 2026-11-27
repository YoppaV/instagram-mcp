# instagram-mcp — streamable-http server for a home NAS (LAN / Tailscale only).
#
# The base image ships Chromium and its system libraries for exactly this
# Playwright version; keep the tag and requirements.txt's playwright== in step.
FROM mcr.microsoft.com/playwright/python:v1.59.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app

# Dependencies first (cached layer), then the package itself.
COPY requirements.txt ./
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install -r requirements.txt
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN /opt/venv/bin/pip install --no-deps . \
 && mkdir -p /data/sessions /data/state /data/downloads \
 && chown -R pwuser:pwuser /data

# Everything lives under /data (bind mounts); the token comes from a secret file.
ENV INSTAGRAM_MCP_TRANSPORT=streamable-http \
    INSTAGRAM_MCP_HOST=0.0.0.0 \
    INSTAGRAM_MCP_PORT=8000 \
    INSTAGRAM_MCP_BEARER_TOKEN_FILE=/run/secrets/instagram_mcp_token \
    INSTAGRAM_SESSION_DIR=/data/sessions \
    INSTAGRAM_STATE_DIR=/data/state \
    INSTAGRAM_DOWNLOADS_DIR=/data/downloads \
    BROWSER_HEADLESS=true

# Non-root. Playwright launches Chromium with --no-sandbox by default, so no
# extra capabilities are needed. The compose file may override the uid/gid.
USER pwuser

EXPOSE 8000

# /healthz answers without a token and never touches Instagram.
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python3", "-c", "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('INSTAGRAM_MCP_PORT','8000'), timeout=4)"]

ENTRYPOINT ["instagram-mcp"]
