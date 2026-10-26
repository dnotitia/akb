"""One real decoder budget shared by document attachments and account photos.

Cancelling an asyncio thread await does not stop Pillow. Keep its slot until
the actual thread settles, including after repeated request cancellation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")
_slots = asyncio.Semaphore(2)


async def run_bounded_decode(
    function: Callable[..., T], *args: object, admission_timeout: float | None = None,
) -> T:
    async with asyncio.timeout(admission_timeout):
        await _slots.acquire()
    try:
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except BaseException:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            # Retrieve an exception even when cancellation wins the race.
            if not task.cancelled():
                task.exception()
            raise
    finally:
        _slots.release()
