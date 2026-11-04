# Third-party packages come from uv.lock, never from a resolver run at build
# time: a plain `pip install .` takes whatever is newest on the build day, so
# two builds of one commit could run different versions, neither of them the
# set CI tested. This stage only turns the lock into a hash-pinned
# requirements file; uv never reaches the runtime image.
# `scripts/ci/check_image_lock.py IMAGE` compares a built image with the lock.
FROM python:3.14-slim AS requirements
RUN pip install --no-cache-dir uv==0.12.12
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN UV_PYTHON_DOWNLOADS=never uv export --quiet --locked --no-dev --no-emit-project \
      --format requirements.txt --no-header --output-file /requirements.txt

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

# The requirements file is the complete locked set, so `--no-deps` installs
# exactly it. The project install below then finds every dependency already
# satisfied and adds only the `akb` package itself.
COPY --from=requirements /requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-deps --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY . .
RUN printf '%s\n' "$AKB_SOURCE_REVISION" > /app/.akb-source-revision
RUN pip install --no-cache-dir .
RUN /usr/local/bin/python -I -B -c "import app.cli; import mcp_server"

# Create vault storage directory
RUN mkdir -p /data/vaults

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
