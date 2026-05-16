"""Vault-template registry.

Single-source-of-truth adapter over backend/templates/vault-templates/*.yaml.
Loaded once at module import; mutating the directory at runtime requires a
process restart.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = (
    Path(__file__).parent.parent.parent / "templates" / "vault-templates"
)


@dataclass(frozen=True)
class CollectionSummary:
    path: str
    name: str


@dataclass(frozen=True)
class TemplateSummary:
    name: str
    display_name: str
    description: str
    collection_count: int
    collections: list[CollectionSummary]


# name → parsed YAML dict
_PAYLOADS: dict[str, dict] = {}
# sorted by display_name
_SUMMARIES: list[TemplateSummary] = []


def _scan() -> None:
    """Read every *.yaml in the templates dir; populate caches.

    Idempotent. Clears existing caches before re-scanning so a missing
    directory yields empty lists (matches test_missing_dir_does_not_raise).
    """
    payloads: dict[str, dict] = {}
    summaries: list[TemplateSummary] = []
    if not _TEMPLATES_DIR.exists():
        logger.warning("Vault templates dir missing: %s", _TEMPLATES_DIR)
        _PAYLOADS.clear()
        _SUMMARIES.clear()
        return
    for path in sorted(_TEMPLATES_DIR.glob("*.yaml")):
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        except yaml.YAMLError as exc:
            logger.warning("Skipping malformed template %s: %s", path.name, exc)
            continue
        if not isinstance(data, dict):
            logger.warning("Template %s is not a YAML mapping; skipping", path.name)
            continue
        name = data.get("name") or path.stem
        collections = data.get("collections") or []
        if not collections:
            logger.warning("Template %s has no collections; skipping", name)
            continue
        payloads[name] = data
        summaries.append(
            TemplateSummary(
                name=name,
                display_name=str(data.get("display_name") or name),
                description=str(data.get("description") or ""),
                collection_count=len(collections),
                collections=[
                    CollectionSummary(
                        path=c["path"],
                        name=str(c.get("name") or c["path"]),
                    )
                    for c in collections
                    if isinstance(c, dict) and "path" in c
                ],
            )
        )
    summaries.sort(key=lambda s: s.display_name)
    _PAYLOADS.clear()
    _PAYLOADS.update(payloads)
    _SUMMARIES.clear()
    _SUMMARIES.extend(summaries)


def list_summaries() -> list[TemplateSummary]:
    return list(_SUMMARIES)


def list_names() -> list[str]:
    return [s.name for s in _SUMMARIES]


def get(name: str) -> dict | None:
    return _PAYLOADS.get(name)


# Scan once at module import.
_scan()
