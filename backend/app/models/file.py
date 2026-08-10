"""Typed REST contracts for the File surface's measurement-only reads.

The legacy File routes still return untyped dicts (a known codegen gap the
OpenAPI contract layer papers over with `AkbJsonObject`). New File routes are
typed at the source instead, so the published schema is generated from the
same declaration the handler returns.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class BodyPlacementTotals(BaseModel):
    """One placement's bounded footprint inside a vault namespace."""

    selected_placement: str = Field(
        description=(
            "Body placement identifier from the closed native set "
            "(`pg-bodystore-v1`, `m1-reference-payload-v1`). It names a "
            "storage strategy, not an address: nothing can be dereferenced "
            "from it."
        ),
    )
    bodies: int = Field(description="Verified bodies stored under this placement.")
    body_bytes: int = Field(description="Sum of the canonical byte sizes of those bodies.")
    distinct_digests: int = Field(
        description=(
            "How many distinct content digests those bodies cover. A count "
            "only — digest values are content hashes of user bodies and are "
            "never enumerated."
        ),
    )


class BodyPlacementObservation(BaseModel):
    """Namespace-level placement census used for unification-purity checks.

    A vault reporting a single `pg-bodystore-v1` row has zero reference
    residue. The converse does not hold: the census counts payload-store
    rows, and those can outlive the Revisions that referenced them (the
    manifest -> payload foreign key is ON DELETE NO ACTION and nothing
    reaps payload rows), so a fully migrated namespace still reports two
    rows if one orphaned reference-placement body is left behind. Two rows
    therefore means residue of some kind — orphaned or live — not
    necessarily that live Heads are still mixed.
    """

    vault: str
    placements: list[BodyPlacementTotals] = Field(
        default_factory=list,
        description="One row per placement present in the namespace, sorted by placement.",
    )
    total_bodies: int
    total_body_bytes: int
