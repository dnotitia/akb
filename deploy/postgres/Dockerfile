# AKB's PostgreSQL: the pinned pgvector image plus the vchord_bm25 BM25 index
# extension, compiled here from its source with four fixes.
#
# The repository's install paths build this image where they run it: Compose,
# deploy/k8s/deploy.sh, the Helm and Native Kubernetes install commands, and the
# CI jobs. Under the default `vector_store_sparse_shape: auto`, a new database on
# it gets the `vchord` shape, where the index owns BM25 scoring. AKB also works
# without the extension, on the `posting` table the backend maintains itself,
# and a database that already serves `posting` keeps it on this image too. The
# published all-in-one image does not include the extension; see the licensing
# note below.
#
# WHY IT IS COMPILED HERE. vchord_bm25 0.3.0 has defects that AKB's default
# sparse shape runs into, and no release after 0.3.0 fixes them:
#   * 0001: building an index over existing rows (CREATE INDEX, REINDEX, and so
#     every pg_restore) saves a full block's score summary before it records the
#     block's best posting. A bounded scan then skips blocks it should read and
#     returns a worse top-k than the exact scan (akb#679). The insert and seal
#     path records first and was never affected.
#   * 0002: VACUUM subtracted a deleted document's one-byte length code instead
#     of its length, so the index's average document length inflated with every
#     update and delete (akb#684). Every side now counts the length the index
#     stores, so what VACUUM subtracts is exactly what was added.
#   * 0003: VACUUM's bulk delete held the index metapage for its whole pass, so
#     every search and insert waited for it: 3 s for a million documents. It
#     logged each delete mark on its own and the counts once at the end, so a
#     backend killed mid-pass left the counts too high for good (akb#687). It
#     now marks one delete bitmap page at a time, and logs the page's marks and
#     the counts they take off in one record.
#   * 0004: VACUUM's cleanup recounted every term id up to the largest one
#     indexed, holding the metapage, with a page write and a WAL record per id.
#     At 729M term ids that stalled search for 44 minutes, wrote 35 GiB of WAL,
#     allocated 2.7 GiB, and could not be cancelled (akb#687). It now recounts
#     a term statistic page at a time, holds no lock from one page to the next,
#     and writes only the pages whose counts changed.
# Drop a patch when an upstream release carries its fix; deploy/postgres/README.md
# says how to check.
#
# PINNED INPUTS. What decides the extension is fixed here:
#   * the pgvector base, the same digest as deploy/k8s/postgres.yaml, and the
#     PostgreSQL headers of exactly its server version;
#   * Rust 1.91.1, the compiler the upstream 0.3.0 binary was built with (its
#     embedded rustc commit is ed61e7d7);
#   * the upstream source tarball, by sha256;
#   * vchord_bm25/Cargo.lock, because 0.3.0 ships none. The crates visible in
#     the upstream binary are at the versions it used, and `--locked` refuses
#     anything else.
# The SQL install and upgrade scripts and the control file come from the same
# source tree. They are byte-identical to the upstream release's files. The
# Debian toolchain (gcc, libc, LLVM 19) follows bookworm's point releases, so a
# later build can differ in bytes, though not in source.
#
# Build it with this directory as the context:
#
#   docker buildx build -t <registry>/akb-postgres:<tag> deploy/postgres
#
# The first build compiles the extension: under a minute on a many-core host,
# several minutes on a laptop. Both base references are multi-architecture
# indexes, so `--platform linux/amd64,linux/arm64` works too, but compiling a
# platform under emulation takes much longer.
#
# LICENSING. `vchord_bm25` is published under the GNU Affero General Public
# License v3 or the Elastic License v2, at the recipient's option; see
# https://github.com/tensorchord/VectorChord-bm25. It is a separate program
# that runs inside the PostgreSQL server and is reached over the PostgreSQL
# wire protocol, so it does not change the licensing of AKB itself. The patches
# in vchord_bm25/ are offered under the extension's own terms. Building and
# running this image is use, not distribution. Distributing the built image is
# distribution of the modified extension and carries the corresponding
# obligation: an offer of its source, which is the upstream tarball plus the
# patches in this directory.

ARG PGVECTOR_IMAGE=pgvector/pgvector:pg16@sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b
ARG RUST_IMAGE=rust:1.91.1-slim-bookworm@sha256:8514999d4786ef12efe89239e86b3d0a021b94b9d35108c8efe6c79ca7dc1a65

FROM ${RUST_IMAGE} AS rust

FROM ${PGVECTOR_IMAGE} AS extension
ARG VCHORD_BM25_VERSION=0.3.0
ARG VCHORD_BM25_SHA256=5ef4441bf3aa7a5ec735f7d865e6ead8b1c827a1b4ecfeac2c0e1c6e7a603bf4
ENV RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo PATH=/usr/local/cargo/bin:$PATH
COPY --from=rust /usr/local/rustup /usr/local/rustup
COPY --from=rust /usr/local/cargo /usr/local/cargo
# The headers of exactly the server this image runs. PGDG's main repository
# carries the last few releases of a major; its archive carries all of them, so
# the pin keeps resolving after main drops this one. The archive is https only,
# hence ca-certificates first, and sits at priority 1 so that it supplies only
# what main lacks (its newest libpq is a beta). `--error-on=any` fails the build
# on a source that cannot be read, instead of skipping it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && . /etc/os-release \
 && echo "deb [ signed-by=/usr/local/share/keyrings/postgres.gpg.asc ] https://apt-archive.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg-archive main" \
      > /etc/apt/sources.list.d/pgdg-archive.list \
 && printf 'Package: *\nPin: origin apt-archive.postgresql.org\nPin-Priority: 1\n' \
      > /etc/apt/preferences.d/pgdg-archive \
 && apt-get update --error-on=any \
 && apt-get install -y --no-install-recommends \
        build-essential curl patch pkg-config \
        "postgresql-server-dev-${PG_MAJOR}=${PG_VERSION}" \
 && rm -rf /var/lib/apt/lists/*
# bindgen's libclang: the LLVM the PostgreSQL headers package depends on, named
# rather than searched for, so a second LLVM on the path cannot change the build.
ENV LIBCLANG_PATH=/usr/lib/llvm-19/lib
WORKDIR /src
RUN curl -fsSL "https://github.com/tensorchord/VectorChord-bm25/archive/refs/tags/${VCHORD_BM25_VERSION}.tar.gz" -o source.tar.gz \
 && echo "${VCHORD_BM25_SHA256}  source.tar.gz" | sha256sum -c - \
 && tar -xzf source.tar.gz --strip-components=1 \
 && rm source.tar.gz
COPY vchord_bm25/ /vchord_bm25/
# A patch that no longer applies exactly, or is already in the source, fails
# the build instead of being skipped or bent to fit: `set -e` stops at the first
# failure, and --forward --fuzz=0 --batch refuse to reverse, fuzz or ask.
RUN set -eu; \
    for p in /vchord_bm25/*.patch; do patch -p1 --forward --fuzz=0 --batch < "$p"; done; \
    cp /vchord_bm25/Cargo.lock Cargo.lock
RUN PGRX_PG_CONFIG_PATH="/usr/lib/postgresql/${PG_MAJOR}/bin/pg_config" \
    cargo build --locked --lib --features "pg${PG_MAJOR}" --release
RUN mkdir -p /out/lib /out/extension \
 && cp target/release/libvchord_bm25.so /out/lib/vchord_bm25.so \
 && cp "sql/install/vchord_bm25--${VCHORD_BM25_VERSION}.sql" sql/vchord_bm25--*--*.sql /out/extension/ \
 && sed -e "s/@CARGO_VERSION@/${VCHORD_BM25_VERSION}/g" vchord_bm25.control > /out/extension/vchord_bm25.control

FROM ${PGVECTOR_IMAGE}

COPY --from=extension /out/lib/ /usr/lib/postgresql/16/lib/
COPY --from=extension /out/extension/ /usr/share/postgresql/16/extension/

# The extension installs into its own `bm25_catalog` schema and needs no
# `shared_preload_libraries` entry, so a database that never runs
# `CREATE EXTENSION vchord_bm25` is byte-for-byte the base image in behaviour.
