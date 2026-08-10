from __future__ import annotations

import pytest

from app.api.bounded_body import read_bounded_body
from app.exceptions import AKBError


class _Request:
    def __init__(self, chunks: list[bytes], content_length: str | None = None):
        self.headers = {}
        if content_length is not None:
            self.headers["content-length"] = content_length
        self._chunks = chunks

    async def stream(self):
        for chunk in self._chunks:
            yield chunk


@pytest.mark.asyncio
async def test_bounded_body_rejects_declared_and_streamed_overflow() -> None:
    with pytest.raises(AKBError) as declared:
        await read_bounded_body(
            _Request([], "5"),
            max_bytes=4,
            too_large_message="too large",
        )
    assert declared.value.status_code == 413

    with pytest.raises(AKBError) as streamed:
        await read_bounded_body(
            _Request([b"12", b"345"]),
            max_bytes=4,
            too_large_message="too large",
        )
    assert streamed.value.status_code == 413


@pytest.mark.asyncio
async def test_bounded_body_returns_exact_bytes_and_rejects_bad_length() -> None:
    body = await read_bounded_body(
        _Request([b"12", b"34"], "4"),
        max_bytes=4,
        too_large_message="too large",
    )
    assert body == b"1234"

    with pytest.raises(AKBError) as invalid:
        await read_bounded_body(
            _Request([], "not-a-number"),
            max_bytes=4,
            too_large_message="too large",
        )
    assert invalid.value.status_code == 400
