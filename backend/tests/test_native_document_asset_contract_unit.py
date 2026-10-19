"""Parsing contract; transactional proofs live in concurrency tests."""

import uuid

import pytest

from app.services.native_document_assets import image_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["inline", "reference", "metadata", "code", "empty"])
async def test_native_asset_plan_matches_rendered_body(shape):
    asset = uuid.uuid4()
    url = f"/api/assets/{asset}"
    payload = {
        "inline": f"![photo]({url})",
        "reference": f"![photo][image]\n\n[image]: {url}",
        "metadata": f"---\nsummary: '![photo]({url})'\n---\nno image",
        "code": f"`![photo]({url})`",
        "empty": "",
    }[shape]
    assert await image_ids(payload) == ({asset} if shape in {"inline", "reference"} else set())
