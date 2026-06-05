"""Shared helpers for the Coral-backed drivers (REST and gRPC).

The two SeahorseDB drivers — ``seahorse_db.py`` (REST/JSONL) and
``seahorse_db_grpc.py`` — talk to the same Coral coordinator with the
same schema. The bytes they wrap differ; the *semantics* of the PK
label, the sparse-string encoding, and the SQL-WHERE UUID guard are
identical. These three helpers used to live as module-private
functions on the REST driver, and the gRPC driver reached across the
file boundary to import them by underscore name. Promoting them here
makes the cross-driver contract first-class and stops ``seahorse_db.py``
from being both "a driver" and "the home of helpers other drivers
import".

If a third Coral transport ever shows up (e.g. an Arrow-IPC streaming
ingest variant), import these from here too.
"""
from __future__ import annotations

import uuid


def chunk_id_to_label(chunk_id: str) -> int:
    """UUID -> SeahorseDB i64 label.

    First 8 bytes of the UUID's binary form, interpreted big-endian as
    **signed** i64. The signedness matters: Coral's JSONL ingest
    parses INT64 columns through Arrow, which rejects unsigned values
    > 2^63 - 1 with ``ComponentError::Arrow`` and surfaces as HTTP 500
    ``error_code 500233 "Internal error"`` with no row context. About
    half of all random UUIDs have a high bit set in their first 8
    bytes, so an unsigned variant of this function reliably 500s on
    roughly that fraction of inserts under sustained load — exactly the
    pattern we filed as SeahorseDB#433 (later resolved as a caller-side
    bug; see backend CHANGELOG 0.7.7). ``signed=True`` keeps the full
    64-bit space addressable on the i64 side and removes that failure
    mode for every Coral transport, not just the REST one.

    Collisions are birthday-paradox bounded — ~2^32 chunks per table
    before a 50% chance of any pair colliding, far beyond any realistic
    vault. Signedness has no effect on collision probability.
    """
    raw = uuid.UUID(chunk_id).bytes
    return int.from_bytes(raw[:8], "big", signed=True)


def encode_sparse_string(
    indices: list[int], values: list[float],
) -> str:
    """Encode AKB's parallel sparse arrays into Coral's
    ``"term_id:weight term_id:weight"`` string format (space-separated,
    one pair per token).

    Verified against the live Coral hybrid search request handler —
    sparse vectors arrive as a single string on this column, not as a
    list of pairs or as a JSON sub-object. The driver-aware ``raw_tf``
    vs ``pre_baked`` decision (BM25 weight convention) is made
    upstream in ``sparse_encoder``; this function just serialises
    whatever weights it gets."""
    if not indices:
        return ""
    return " ".join(f"{int(t)}:{float(w):.6g}" for t, w in zip(indices, values))


def validate_uuid_for_sql(s: str) -> str:
    """Reject anything that isn't a UUID before interpolating into a
    SQL WHERE clause. AKB source_ids are always UUIDs; this is purely
    defense in depth against any caller mistake."""
    uuid.UUID(s)
    return s
