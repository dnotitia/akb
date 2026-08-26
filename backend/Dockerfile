FROM python:3.14-slim

ARG AKB_SOURCE_REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/dnotitia/akb" \
      org.opencontainers.image.revision="${AKB_SOURCE_REVISION}"

# The hermetic external-git runner pins DNS resolution
# via git's `http.curloptResolve`, documented since git 2.37 — a code-owned
# security floor. Debian's packaged git is already well past 2.37, so we install
# the distro package (no brittle apt version pin that would break on every Debian
# bump) but ASSERT the floor at build time: if a future base-image ever ships a
# git below 2.37, the build fails here instead of producing an image whose git
# would silently defeat the pin. `app/services/external_git_capability.py`
# re-verifies this (and that curloptResolve actually works) at startup.
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/* \
    && git --version \
    && git --version | awk '{split($3, v, "."); if (v[1] < 2 || (v[1] == 2 && v[2] < 37)) { print "FATAL: git " $3 " < 2.37 (curloptResolve floor)"; exit 1 }}'

WORKDIR /app

COPY . .
RUN pip install --no-cache-dir .
RUN /usr/local/bin/python -I -B -c "import app.cli; import mcp_server"

# Create vault storage directory
RUN mkdir -p /data/vaults

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
