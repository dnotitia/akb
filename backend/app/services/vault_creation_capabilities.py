"""Capabilities shared by vault creation discovery surfaces.

The selected revision backend is immutable during a process lifetime. Keep
request validation in the document service so clients with older catalogs
receive the same stable unsupported-feature error.
"""

from dataclasses import dataclass

from app.config import settings


@dataclass(frozen=True)
class VaultCreationCapabilities:
    templates: bool
    external_git: bool


def get_vault_creation_capabilities() -> VaultCreationCapabilities:
    legacy = settings.document_revision_backend in {"bare_git", "bare_git_current"}
    return VaultCreationCapabilities(templates=legacy, external_git=legacy)
