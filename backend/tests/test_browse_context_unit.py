from app.models.document import BrowseContext, BrowseItem, BrowseResponse
from mcp_server.response_projection import browse_payload


def _response() -> BrowseResponse:
    return BrowseResponse(
        vault="reef-akb",
        path="research",
        context=BrowseContext(
            type="collection",
            uri="akb://reef-akb/coll/research",
            name="research",
            path="research",
            summary="Technical comparisons and AKB recommendations",
        ),
        items=[
            BrowseItem(
                name="designs",
                path="research/designs",
                type="collection",
                uri="akb://reef-akb/coll/research/designs",
                summary="Approved and proposed designs",
            ),
            BrowseItem(
                name="catalog.md",
                path="research/catalog.md",
                type="document",
                uri="akb://reef-akb/coll/research/doc/catalog.md",
                summary="A large resource summary",
            ),
            BrowseItem(
                name="evidence.pdf",
                path="research/evidence.pdf",
                type="file",
                uri="akb://reef-akb/coll/research/file/00000000-0000-0000-0000-000000000001",
                summary="Another large resource summary",
            ),
        ],
    )


def test_default_browse_keeps_collection_intent_but_drops_resource_summaries():
    payload = browse_payload(_response(), include_summary=False)

    assert payload["context"]["summary"] == "Technical comparisons and AKB recommendations"
    assert payload["items"][0]["summary"] == "Approved and proposed designs"
    assert "summary" not in payload["items"][1]
    assert "summary" not in payload["items"][2]


def test_include_summary_keeps_every_resource_summary():
    payload = browse_payload(_response(), include_summary=True)

    assert [item["summary"] for item in payload["items"]] == [
        "Approved and proposed designs",
        "A large resource summary",
        "Another large resource summary",
    ]
