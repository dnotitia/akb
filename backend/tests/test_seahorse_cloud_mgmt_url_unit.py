"""Management-URL default and request shaping for the Seahorse Cloud driver.

#524: the legacy `/bff` management prefix no longer routes — every path under
it answers an unconditional 401 ("Missing authorization header") without
reading the credential — while the `/api` prefix authenticates normally.
A driver on the default therefore fails in `ensure_collection` with a message
about a missing header, which sends an operator to check their token rather
than their path. These pin the default and the three management URLs the
driver builds, without any live service: the HTTP client is faked, so what is
asserted is the prefix the driver was constructed with and the paths it joins
onto it.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.services.vector_store.seahorse_cloud import SeahorseCloudStore


def _store(management_url: str = "https://console.seahorse.dnotitia.ai/api") -> SeahorseCloudStore:
    return SeahorseCloudStore(
        management_url=management_url,
        token="t",
        tenant_uuid="ten",
        table_name="chunks",
        dense_dim=4,
    )


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {"data": []}
        self.headers = {"content-type": "application/json"}
        self.text = __import__("json").dumps(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=None  # type: ignore[arg-type]
            )


class _FakeHttp:
    def __init__(self):
        self.calls: list[dict] = []

    async def get(self, url, **kw):
        self.calls.append({"method": "GET", "url": url})
        return _Resp(200, {"data": []})

    async def post(self, url, **kw):
        self.calls.append({"method": "POST", "url": url})
        return _Resp(200, {"data": {}})


def test_default_management_url_uses_the_live_prefix():
    # The default is the whole fix: an operator who sets nothing must land on
    # the routed prefix. Assert the exact value, not just "not /bff", so a
    # future host move fails loudly here instead of at 3am in ensure_collection.
    assert settings.seahorse_cloud_management_url == "https://console.seahorse.dnotitia.ai/api"


def test_default_has_no_bff_prefix():
    assert "/bff" not in settings.seahorse_cloud_management_url


@pytest.mark.asyncio
async def test_table_lookup_by_name_hits_the_live_prefix():
    s = _store()
    s._client = _FakeHttp()
    await s._bff_get_table()
    assert s._client.calls == [
        {"method": "GET", "url": "https://console.seahorse.dnotitia.ai/api/tenants/ten/tables"}
    ]


@pytest.mark.asyncio
async def test_table_lookup_by_uuid_hits_the_live_prefix():
    s = SeahorseCloudStore(
        management_url="https://console.seahorse.dnotitia.ai/api",
        token="t",
        tenant_uuid="ten",
        table_uuid="12345678-1234-1234-1234-1234567890ab",
        dense_dim=4,
    )
    s._client = _FakeHttp()
    await s._bff_get_table()
    assert s._client.calls == [
        {
            "method": "GET",
            "url": "https://console.seahorse.dnotitia.ai/api/tenants/ten/tables/12345678-1234-1234-1234-1234567890ab",
        }
    ]


@pytest.mark.asyncio
async def test_trailing_slash_on_management_url_does_not_double_slash():
    s = _store("https://console.seahorse.dnotitia.ai/api/")
    s._client = _FakeHttp()
    await s._bff_get_table()
    assert s._client.calls[0]["url"] == "https://console.seahorse.dnotitia.ai/api/tenants/ten/tables"
