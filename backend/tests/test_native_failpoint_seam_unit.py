"""Unit contract for the C5 failpoint injection seam at both composition roots.

Conformance case C5 injects a deterministic failure at a named transaction
boundary and then proves the mutation left no partial authority.  The
boundaries live inside ``NativeRevisionService``, but a C5 test never builds
that service directly in production shape — the two roots above it do.  This
file pins the seam itself, with no database:

* the registry of legal boundary names is complete and self-enforcing, so a
  typo'd injection fails loudly instead of silently never firing;
* ``NativeDocumentService`` and the M1 text-File bridge each thread an
  injected failpoint down to the service they compose;
* with nothing injected, both roots compose exactly what they composed
  before the seam existed.

The behavioural halves — a boundary actually firing mid-mutation, and the
composite File-confirm rollback — need real PostgreSQL and live in
``tests/concurrency/test_native_ledger_b_core.py`` and
``tests/test_m1_file_measurement_pg.py``.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from app.services import m1_file_measurement as m1
from app.services import m1_native_text_file_bridge as bridge
from app.services import native_revision_service as native
from app.services.native_document_service import NativeDocumentService
from app.services.native_revision_service import (
    FAILPOINT_BOUNDARIES,
    NativeRevisionService,
)


_SERVICE_SOURCE = Path(native.__file__).read_text()
_ADAPTER_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "native_revision_m1_adapter.py"
)


def _load_adapter():
    """Load the M1 harness adapter, which is a script rather than a package."""
    spec = importlib.util.spec_from_file_location("native_revision_m1_adapter", _ADAPTER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _no_leaked_bridge_registration():
    m1.reset_native_text_file_services_for_tests()
    yield
    m1.reset_native_text_file_services_for_tests()


def test_registry_is_frozen_unique_and_covers_every_dispatched_boundary():
    dispatched = set(re.findall(r'_hit\("([^"]+)"\)', _SERVICE_SOURCE))
    assert dispatched, "no _hit call sites found; the scan regex has rotted"
    assert isinstance(FAILPOINT_BOUNDARIES, tuple)
    assert len(set(FAILPOINT_BOUNDARIES)) == len(FAILPOINT_BOUNDARIES)
    # Both directions matter.  A dispatched-but-unregistered name makes the
    # registry a lie; a registered-but-undispatched name is a boundary a C5
    # case would wait on forever.
    assert set(FAILPOINT_BOUNDARIES) == dispatched


def test_m1_harness_adapter_only_asks_for_registered_boundaries():
    """The C5 harness names boundaries by hand; hold it to the registry.

    ``_hit`` raises on an unregistered name, but only once a run reaches that
    boundary — a mistyped entry in the adapter's own map would surface as a
    late measurement failure on a live database.  Compare the two statically
    instead.
    """
    adapter = _load_adapter()
    requested = set(adapter.ACTUAL_FAILPOINTS.values())
    assert requested, "the adapter's boundary map is empty; this assertion is vacuous"
    assert requested <= set(FAILPOINT_BOUNDARIES)
    # Its own key list must stay in step with the map it indexes.
    assert set(adapter.PRECOMMIT_BOUNDARIES) == set(adapter.ACTUAL_FAILPOINTS)


async def test_hit_rejects_an_unregistered_boundary_only_when_injected():
    seen: list[str] = []

    unset = NativeRevisionService(object(), repository=object(), payload_store=object())  # type: ignore[arg-type]
    # Production leaves the hook unset, so the guard must cost nothing and
    # change nothing on that path.
    assert await unset._hit("authority.after_typo") is None

    injected = NativeRevisionService(
        object(),  # type: ignore[arg-type]
        repository=object(),  # type: ignore[arg-type]
        payload_store=object(),  # type: ignore[arg-type]
        failpoint=seen.append,
    )
    with pytest.raises(ValueError, match="not registered: authority.after_typo"):
        await injected._hit("authority.after_typo")
    assert seen == []


@pytest.mark.parametrize("boundary", FAILPOINT_BOUNDARIES)
async def test_hit_dispatches_sync_and_async_failpoints_for_every_boundary(boundary: str):
    sync_seen: list[str] = []
    async_seen: list[str] = []

    async def async_failpoint(name: str) -> None:
        async_seen.append(name)

    for failpoint, seen in ((sync_seen.append, sync_seen), (async_failpoint, async_seen)):
        service = NativeRevisionService(
            object(),  # type: ignore[arg-type]
            repository=object(),  # type: ignore[arg-type]
            payload_store=object(),  # type: ignore[arg-type]
            failpoint=failpoint,
        )
        await service._hit(boundary)
        assert seen == [boundary]


async def test_document_facade_threads_the_failpoint_into_the_service_it_builds():
    pool_marker = object()

    async def failpoint(name: str) -> None:  # pragma: no cover - never invoked here
        raise AssertionError(name)

    injected = await NativeDocumentService(pool=pool_marker, failpoint=failpoint)._native()  # type: ignore[arg-type]
    assert injected.failpoint is failpoint
    assert injected.pool is pool_marker

    default = await NativeDocumentService(pool=pool_marker)._native()  # type: ignore[arg-type]
    assert default.failpoint is None


async def test_text_file_bridge_threads_the_failpoint_into_the_bound_service():
    conn_marker = object()

    async def failpoint(name: str) -> None:  # pragma: no cover - never invoked here
        raise AssertionError(name)

    injected = bridge._service_on(conn_marker, failpoint)  # type: ignore[arg-type]
    assert injected.failpoint is failpoint
    assert bridge._service_on(conn_marker).failpoint is None  # type: ignore[arg-type]


def test_installing_the_bridge_binds_the_failpoint_to_both_authority_callbacks():
    async def failpoint(name: str) -> None:  # pragma: no cover - never invoked here
        raise AssertionError(name)

    bridge.install_m1_native_text_file_bridge(failpoint=failpoint)
    assert m1._native_text_publisher.func is bridge._publish  # type: ignore[union-attr]
    assert m1._native_text_publisher.keywords == {"failpoint": failpoint}  # type: ignore[union-attr]
    assert m1._native_text_deleter.func is bridge._delete  # type: ignore[union-attr]
    assert m1._native_text_deleter.keywords == {"failpoint": failpoint}  # type: ignore[union-attr]
    # The opener reads on its own pool connection outside any File
    # transaction; it crosses no authority boundary, so it stays unbound.
    assert m1._native_text_opener is bridge._open

    bridge.install_m1_native_text_file_bridge()
    assert m1._native_text_publisher.keywords == {"failpoint": None}  # type: ignore[union-attr]
    assert m1._native_text_deleter.keywords == {"failpoint": None}  # type: ignore[union-attr]
