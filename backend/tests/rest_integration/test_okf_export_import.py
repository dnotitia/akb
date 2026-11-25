from io import BytesIO
from zipfile import ZipFile

import httpx
import pytest

from tests.rest_integration.helpers import register_user

pytestmark = pytest.mark.live_services


async def test_okf_json_and_zip_export_then_multipart_import(api: httpx.AsyncClient):
    owner = await register_user(api, label="okf")
    headers = {"Authorization": f"Bearer {owner.jwt}"}
    source = f"okf-src-{owner.id.hex[:12]}"
    target = f"okf-target-{owner.id.hex[:12]}"

    response = await api.post("/api/v1/vaults", headers=headers, params={"name": source})
    assert response.raise_for_status().json()["name"] == source
    for document in (
        {
            "collection": "specs",
            "slug": "api-v2",
            "title": "API v2",
            "content": "# API v2\n\nSpec body.",
            "type": "spec",
            "status": "active",
            "tags": ["api"],
        },
        {"collection": "", "slug": "readme", "title": "Readme", "content": "# Readme\n\nRoot doc.", "type": "note"},
    ):
        response = await api.post("/api/v1/documents", headers=headers, json={"vault": source, **document})
        assert response.raise_for_status().json()["uri"]
    response = await api.post(
        f"/api/v1/tables/{source}",
        headers=headers,
        json={"name": "metrics", "columns": [{"name": "region", "type": "text"}, {"name": "hits", "type": "number"}]},
    )
    assert response.raise_for_status().json()["uri"]

    export = f"/api/v1/vaults/{source}/export"
    response = await api.get(export, headers=headers, params={"format": "okf", "as": "json"})
    bundle = response.raise_for_status().json()
    assert bundle["file_count"] > 0

    response = await api.get(export, headers=headers, params={"format": "okf"})
    archive = response.raise_for_status().content
    assert archive.startswith(b"PK")
    assert response.headers["content-type"] == "application/zip"
    with ZipFile(BytesIO(archive)) as zipped:
        assert set(zipped.namelist()) == set(bundle["files"])

    response = await api.get(export, headers=headers, params={"format": "rdf"})
    assert response.status_code == 400, response.text
    assert "unsupported format" in response.text.lower()

    response = await api.post("/api/v1/vaults", headers=headers, params={"name": target})
    assert response.raise_for_status().json()["name"] == target
    response = await api.post(
        f"/api/v1/vaults/{target}/import",
        headers=headers,
        params={"format": "okf", "status": "active"},
        files={"file": ("bundle.zip", archive, "application/zip")},
    )
    assert response.raise_for_status().json()["created"] >= 2
