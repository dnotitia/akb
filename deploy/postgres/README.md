# PostgreSQL image with the optional BM25 index extension

AKB's sparse retrieval leg does not need this. It works against the `posting`
table the backend maintains, on the stock `pgvector/pgvector:pg16` image named
everywhere else in this repository.

This image adds [`vchord_bm25`](https://github.com/tensorchord/VectorChord-bm25),
which implements the same BM25 formula over a block-max index instead of a
relational table. An operator who wants that trade can build the image here and
get the same bytes we do.

## Build

```bash
docker buildx build -f deploy/postgres/Dockerfile \
  --platform linux/amd64,linux/arm64 \
  -t <registry>/akb-postgres:pg16-bm25 --push .
```

Both stages are pinned by multi-architecture index digest, so the build is
reproducible and enabling the extension does not move the PostgreSQL version at
the same time. To move either pin, follow the procedure in
[`../k8s/README.md`](../k8s/README.md).

## Enable

```sql
CREATE EXTENSION vchord_bm25;
```

The extension installs into its own `bm25_catalog` schema and needs no
`shared_preload_libraries` entry. A database that never runs `CREATE EXTENSION`
behaves exactly like the base image.

Note that installing the extension is not by itself enough for AKB to use it —
the sparse leg selects its implementation separately. This image only makes the
option available.

## Tokenization is not affected

The extension ships no tokenizer of its own; `vchord_bm25` 0.3.0 is the index
and the scoring, nothing else. Its vector type is built from an integer array
of term ids, which is what AKB's `bm25_vocab` already mints, so the Korean
tokenizer stays exactly where it is.

## Before moving the extension pin

The sparse query path filters on the sign of the score: a document holding any
query term scores strictly negative, one holding none scores exactly `-0`, and
the filter keeps the negatives. That holds because this extension's IDF is a
log1p variant which stays positive at every document frequency — measured at
`df = N`, all fifty matching documents still scored below zero.

Classic BM25 IDF, `log((N-df+0.5)/(df+0.5))`, turns negative once a term is in
more than half the corpus. An extension version that switched to it would make
the filter discard real matches for common terms, silently and only for the
terms most queries contain.

So when moving this pin, check that a document containing a query term still
scores below zero at high document frequency before accepting the bump.

## Licensing

`vchord_bm25` is published under the GNU Affero General Public License v3 or
the Elastic License v2, at the recipient's option. It is a separate program
that runs inside the PostgreSQL server and is reached over the PostgreSQL wire
protocol, so it does not change the licensing of AKB itself.

Building and running this image is use. **Distributing** the built image is
distribution of that extension and carries the corresponding obligation —
unmodified, that is an offer of the upstream source, which the link above
satisfies.
