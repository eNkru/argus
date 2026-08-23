# syntax=docker/dockerfile:1
# Argus — standalone Camoufox fetch service image.
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# GTK / NSS / X11 libraries required by the Camoufox Firefox build.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        fonts-liberation \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libatspi2.0-0 \
        libcairo2 \
        libcups2 \
        libdbus-1-3 \
        libdbus-glib-1-2 \
        libdrm2 \
        libgbm1 \
        libgdk-pixbuf-2.0-0 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libx11-xcb1 \
        libxcb-shm0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        libxshmfence1 \
        libxt6 \
        wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./

# Install dependencies (incl. camoufox) and fetch the browser binary in a
# cacheable layer — this is the heavy step (~browser download) and changes
# rarely. Keep this dep list in sync with pyproject.toml [project].dependencies;
# argus itself is installed --no-deps below so code changes don't bust this layer.
RUN python -m venv /opt/argus \
    && /opt/argus/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/argus/bin/pip install --no-cache-dir \
        "camoufox==0.5.4" "fastapi>=0.115" "uvicorn[standard]>=0.30" \
        "pydantic>=2.7" "pydantic-settings>=2.3" "httpx>=0.27" \
    && /opt/argus/bin/camoufox fetch \
    # The fetched bundle carries font sets for all three fingerprint OSes it
    # can emulate (macos 569M + windows 322M + linux 41M). The image runs with
    # the linux fingerprint pinned, so prune the macos/windows TTCs — dead
    # weight that gzip poorly.
    && rm -rf /root/.cache/camoufox/browsers/official/*/fonts/macos \
    && rm -rf /root/.cache/camoufox/browsers/official/*/fonts/windows

# Install argus itself (changes often, light, no-deps so the layer above is reused).
# pyproject.toml declares `license = { file = "LICENSE" }` and `readme = "README.md"`,
# so both files must exist in /app for hatchling's metadata validation at wheel build.
COPY LICENSE README.md ./
COPY src ./src
RUN /opt/argus/bin/pip install --no-cache-dir --no-deps .

ENV PATH="/opt/argus/bin:${PATH}"

EXPOSE 8000
CMD ["uvicorn", "argus.main:app", "--host", "0.0.0.0", "--port", "8000"]
