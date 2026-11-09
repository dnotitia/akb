"""Bind public document retry keys to authenticated, normalized requests."""

from dataclasses import dataclass
import hashlib
import json
import uuid
from typing import TypedDict

from app.exceptions import AKBError, ValidationError
from app.util.errors import DOCUMENT_IDEMPOTENCY_UNSUPPORTED


class DocumentActorOptions(TypedDict, total=False):
    idempotency_actor: uuid.UUID


def document_actor_options(key: uuid.UUID | None, actor_id: str) -> DocumentActorOptions:
    return {"idempotency_actor": uuid.UUID(actor_id)} if key is not None else {}


class DocumentIdempotencyUnsupportedError(AKBError):
    def __init__(self):
        super().__init__(
            "Document idempotency keys require PostgreSQL Native revision storage",
            status_code=501, code=DOCUMENT_IDEMPOTENCY_UNSUPPORTED,
        )


@dataclass(frozen=True)
class DocumentIdempotency:
    mutation_id: uuid.UUID
    fingerprint: str
    namespace_id: uuid.UUID

    @classmethod
    def bind(cls, *, namespace_id: uuid.UUID, actor_id: uuid.UUID | None,
             key: uuid.UUID, operation: str, request: dict) -> "DocumentIdempotency":
        # actor_id comes from authentication, never a request/profile field.
        if not isinstance(actor_id, uuid.UUID):
            raise ValidationError("An authenticated actor is required for an idempotency key")
        mutation_id = uuid.uuid5(
            uuid.NAMESPACE_URL, f"akb:document-request:v1:{namespace_id}:{actor_id}:{key}",
        )
        canonical = json.dumps(
            {"version": 1, "namespace_id": str(namespace_id), "actor_id": str(actor_id),
             "operation": operation, "request": request},
            sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        )
        return cls(mutation_id, hashlib.sha256(canonical.encode("utf-8")).hexdigest(), namespace_id)
