"""Public models for the authenticated Vault Change Event Tail."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class EventTailModel(BaseModel):
    """Reject accidental fields in the wire-level event records."""

    model_config = ConfigDict(extra="forbid")


class ChangeEventEnvelopeV1(EventTailModel):
    version: Literal[1]
    cursor: str
    occurred_at: datetime
    vault: str
    kind: str
    resource_uri: str | None = None
    actor: str | None = None
    payload: dict[str, Any]


class TailCheckpointV1(EventTailModel):
    version: Literal[1]
    cursor: str
