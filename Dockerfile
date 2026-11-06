FROM node:22-slim AS frontend
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Sprint 4: the orchestrator host carries the platform CLIs (aws, az, gcloud, kubectl) so a
# run is never constrained by what is installed on a person's machine. Build with
# --build-arg WITH_CLOUD_CLIS=0 for a smaller image that keeps every cloud runner in plan mode.
ARG WITH_CLOUD_CLIS=1
RUN if [ "$WITH_CLOUD_CLIS" = "1" ]; then \
      set -eux; \
      apt-get update; \
      apt-get install -y --no-install-recommends unzip apt-transport-https lsb-release; \
      arch="$(uname -m)"; \
      curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${arch}.zip" -o /tmp/awscliv2.zip; \
      unzip -q /tmp/awscliv2.zip -d /tmp; /tmp/aws/install; rm -rf /tmp/aws /tmp/awscliv2.zip; \
      karch="$(dpkg --print-architecture)"; \
      curl -fsSL "https://dl.k8s.io/release/$(curl -fsSL https://dl.k8s.io/release/stable.txt)/bin/linux/${karch}/kubectl" -o /usr/local/bin/kubectl; \
      chmod 0755 /usr/local/bin/kubectl; \
      curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg; \
      echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" > /etc/apt/sources.list.d/google-cloud-sdk.list; \
      apt-get update; \
      apt-get install -y --no-install-recommends google-cloud-cli; \
      rm -rf /var/lib/apt/lists/*; \
      python -m venv /opt/azure-cli; \
      /opt/azure-cli/bin/pip install --no-cache-dir --quiet azure-cli "setuptools>=78.1.1"; \
      ln -s /opt/azure-cli/bin/az /usr/local/bin/az; \
    fi

COPY requirements.txt requirements-dev.txt ./
# The orchestrator image (WITH_CLOUD_CLIS=1) carries the dev tools it runs locally (pytest,
# pip-audit, ruff); the deployed support app (WITH_CLOUD_CLIS=0) carries its runtime only, so an
# image scan judges the application and not its tooling (ruff's packaged SBOM, for one, names
# the setuptools it was built with and scanners report it as installed; found before live run 15).
RUN pip install --no-cache-dir --upgrade pip "setuptools>=78.1.1" wheel \
    && if [ "$WITH_CLOUD_CLIS" = "1" ]; then pip install --no-cache-dir -r requirements-dev.txt; else pip install --no-cache-dir -r requirements.txt; fi

COPY app ./app
COPY mcp_server ./mcp_server
COPY orchestrator ./orchestrator
COPY scripts ./scripts
COPY resources ./resources
COPY tests ./tests
COPY pytest.ini ./
COPY Dockerfile ./
COPY stackhawk.yml ./
COPY frontend/package.json ./frontend/package.json
COPY .github ./.github
COPY --from=frontend /src/dist ./frontend/dist

# The deployed image (WITH_CLOUD_CLIS=0) is the support app alone: pip is a build-time tool there,
# and pip 26's vendored SBOM (pip/_vendor/bom.cdx.json) declares the setuptools 70.3.0 it vendors
# pkg_resources from, which image scanners report as CVE-2025-47273 although no such package is
# installed (found before live run 15). The orchestrator image keeps pip for pip-audit.
RUN if [ "$WITH_CLOUD_CLIS" = "0" ]; then pip uninstall -y pip && rm -rf /root/.cache/pip; fi

RUN useradd --create-home appuser \
    && mkdir -p /app/var /app/dist \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/status')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
