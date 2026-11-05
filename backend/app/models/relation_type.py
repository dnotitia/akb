"""Explicit relation vocabulary for API, MCP and graph validation."""

from typing import Literal, get_args

LinkRelationType = Literal[
    "depends_on",
    "related_to",
    "implements",
    "references",
    "attached_to",
    "derived_from",
]
LINK_RELATION_TYPES: tuple[str, ...] = get_args(LinkRelationType)
