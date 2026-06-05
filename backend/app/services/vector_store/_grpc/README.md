# Generated Coral gRPC stubs

This directory holds the proto files and Python stubs that the
`seahorse-db-grpc` driver imports. They are vendored from the
SeahorseDB monorepo so the AKB repo can build without network
access to a private upstream.

## Source

- Upstream:  `github.com/dn-inc/SeahorseDB` (private), branch
  `SDDEV-244/monorepo-coral-sparse`
- Vendored at commit:  `e1364f27158a0df69b8f528c3f701635f777fdaf`
  (`[SDDEV-244] ci: fix seahorse smoke deploy`)
- Files copied from:  `coral/proto/coral/**/v1/*.proto`

## Regenerate after an upstream proto bump

When SeahorseDB lands a wire change the driver consumes — new field
on `HybridSearchRequest`, new enum value on `ScalarType`, etc. —
regenerate both the `.proto` copies and the `_pb2*.py` stubs in one
pass so they stay in lockstep with what Coral actually serves.

```bash
# 1. update source — point at the SeahorseDB checkout you want to
#    track. (For dn-inc engineers: the same monorepo branch the
#    Coral image was built from.)
SEAHORSE_REPO=/path/to/SeahorseDB

# 2. refresh the vendored .proto tree
rsync -a --delete \
    "$SEAHORSE_REPO/coral/proto/coral/" \
    backend/app/services/vector_store/_grpc/proto/coral/

# 3. recompile the Python stubs in place. Only the protos the driver
#    actually imports need stubs; the others are kept on disk for
#    reference and future use.
cd backend
source .venv/bin/activate
pip install "grpcio==1.81.0" "grpcio-tools==1.81.0" "protobuf==6.33.6"
PROTO_DIR=app/services/vector_store/_grpc/proto
python -m grpc_tools.protoc \
    -I"$PROTO_DIR" \
    --python_out="$PROTO_DIR" \
    --grpc_python_out="$PROTO_DIR" \
    "$PROTO_DIR/coral/common/v1/common.proto" \
    "$PROTO_DIR/coral/storage/v1/storage.proto" \
    "$PROTO_DIR/coral/catalog/v1/catalog.proto" \
    "$PROTO_DIR/coral/segment/v1/segment.proto" \
    "$PROTO_DIR/coral/health/v1/health.proto" \
    "$PROTO_DIR/coral/ingest/v1/ingest.proto" \
    "$PROTO_DIR/coral/table/v1/table.proto" \
    "$PROTO_DIR/coral/query/v1/query.proto"

# 4. update the commit hash at the top of this file. Then run the
#    driver's unit tests and the local 25-scenario hybrid e2e to
#    confirm nothing on AKB's side regressed.
bash scripts/check.sh
AKB_URL=http://localhost:8000 \
    bash backend/tests/test_hybrid_search_e2e.sh
```

## Known caveats

- The stubs use absolute imports (`from coral.foo.v1 import …`)
  because that's what `grpc_tools.protoc` emits by default. The
  driver works around this by inserting this directory onto
  `sys.path` for the import block and removing it after — see
  `seahorse_db_grpc.py` for the workaround and a TODO for the
  proper relative-import rewrite.
- Stubs are ruff-excluded via `pyproject.toml` (`extend-exclude`).
  Hand-editing them is a regression magnet; always regenerate.
