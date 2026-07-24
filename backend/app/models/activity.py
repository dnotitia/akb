"""Typed REST contracts for git-backed activity and document history."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ActivityResponse(BaseModel):
    """Preserve additive git/service fields while typing the public shape."""

    model_config = ConfigDict(extra="allow")


class ActivityFileChange(ActivityResponse):
    path: str
    change: Literal["added", "deleted", "modified"]


class ActivityEntry(ActivityResponse):
    hash: str
    subject: str
    author: str
    date: datetime
    action: str
    summary: str
    agent: str
    files: list[ActivityFileChange]
    author_name: str | None = None


class RecentDocumentChange(ActivityResponse):
    doc_id: str
    vault: str
    path: str
    title: str
    type: str
    commit: str | None
    changed_at: datetime | None


class DocumentHistoryEntry(ActivityResponse):
    hash: str
    message: str
    author: str
    date: datetime
    author_name: str | None = None


class AkbActivityEnvelope(ActivityResponse):
    kind: Literal["activity"]
    vault: str
    total: int
    activity: list[ActivityEntry]


class AkbRecentChangesEnvelope(ActivityResponse):
    kind: Literal["recent_changes"]
    changes: list[RecentDocumentChange]


class AkbDocumentHistoryEnvelope(ActivityResponse):
    kind: Literal["document_history"]
    uri: str
    history: list[DocumentHistoryEntry]


class AkbDocumentDiffEnvelope(ActivityResponse):
    kind: Literal["document_diff"]
    file: str
    commit: str
    type: Literal["added", "deleted", "modified", "unknown", "unchanged"]
    diff: str
    error: str | None = None
