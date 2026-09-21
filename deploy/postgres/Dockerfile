# AKB's PostgreSQL, plus an optional BM25 index extension.
#
# This is not required. AKB's sparse retrieval leg works without it, against
# the `posting` table the backend maintains itself. The extension is an
# alternative implementation of the same BM25 formula with a different index
# structure, and this image exists so that an operator who wants it can build
# the exact same bytes we do.
#
# The base is AKB's own PostgreSQL image, pinned by digest, so enabling the
# extension does not silently change the database version at the same time.
# The extension is copied out of its publisher's image rather than compiled
# here: it is a pgrx/Rust build whose toolchain we would otherwise have to pin
# and carry, and copying keeps the provenance a single digest anyone can check.
#
# Both references are multi-architecture indexes, so `docker buildx build
# --platform linux/amd64,linux/arm64` resolves each stage to the matching
# platform and the `.so` copied in is always the right one.
#
#   docker buildx build -f deploy/postgres/Dockerfile -t <registry>/akb-postgres:<tag> .
#
# LICENSING. `vchord_bm25` is published under the GNU Affero General Public
# License v3 or the Elastic License v2, at the recipient's option; see
# https://github.com/tensorchord/VectorChord-bm25. It is a separate program
# that runs inside the PostgreSQL server and is reached over the PostgreSQL
# wire protocol, so it does not change the licensing of AKB itself. Building
# and running this image is use, not distribution. Distributing the built
# image is distribution of that extension, and carries the corresponding
# obligation — unmodified, that is an offer of the upstream source.

ARG VCHORD_BM25_IMAGE=tensorchord/vchord_bm25-postgres:pg16-v0.3.0@sha256:336a1083db7940dbdc1202b71fca2382fb009d35a6bcc27e4258ffc1ea0fd27d

FROM ${VCHORD_BM25_IMAGE} AS extension

FROM pgvector/pgvector:pg16@sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b

COPY --from=extension /usr/lib/postgresql/16/lib/vchord_bm25.so /usr/lib/postgresql/16/lib/
COPY --from=extension /usr/share/postgresql/16/extension/vchord_bm25* /usr/share/postgresql/16/extension/

# The extension installs into its own `bm25_catalog` schema and needs no
# `shared_preload_libraries` entry, so a database that never runs
# `CREATE EXTENSION vchord_bm25` is byte-for-byte the base image in behaviour.
