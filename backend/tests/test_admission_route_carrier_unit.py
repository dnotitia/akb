"""The admission routes must be reachable by the caller that has to use them.

Everything else about admission is proven by calling the service functions: the
arrival is recorded, the approval binds, the row is cleared. None of that says
the control plane can *get* to any of it.

It could not, for one merge. The routes landed on the product-admin SSO router,
which is guarded by the dedicated admin browser session; the control plane
authenticates with an administrator credential and holds no such session, so
`AdmitArrival` would have called an endpoint it can never reach — an empty list
in the console forever, with nothing red anywhere. Both the unit tests and a
two-Keycloak fixture were green throughout, because both call the handler.

So this asserts the **carrier** rather than the handler: an endpoint proved by
calling its handler is not proved reachable by its caller.
"""

from __future__ import annotations

import pytest

from app.api.deps import get_current_user
from app.api.routes.admin_auth import (
    get_current_product_admin,
    get_product_admin_mutation,
)
from app.main import app


_ADMISSION_PATHS = {
    "/api/v1/admin/pending-admissions",
    "/api/v1/admin/pending-admissions/{admission_id}",
    "/api/v1/admin/pending-admissions/{admission_id}/approve",
}


def _admission_routes():
    return [route for route in app.routes if getattr(route, "path", None) in _ADMISSION_PATHS]


def _dependency_callables(route) -> set:
    resolved = set()
    for dependant in [route.dependant, *route.dependant.dependencies]:
        if dependant.call is not None:
            resolved.add(dependant.call)
        for nested in dependant.dependencies:
            if nested.call is not None:
                resolved.add(nested.call)
    return resolved


def test_every_admission_path_is_mounted():
    # Named exactly, so moving one and leaving the others behind is a failure
    # rather than a smaller set that still passes.
    assert {route.path for route in _admission_routes()} == _ADMISSION_PATHS


@pytest.mark.parametrize("path", sorted(_ADMISSION_PATHS))
def test_admission_routes_answer_the_credential_the_control_plane_holds(path):
    routes = [route for route in _admission_routes() if route.path == path]
    assert routes, path
    for route in routes:
        callables = _dependency_callables(route)
        assert get_current_user in callables, (
            f"{path} does not accept the ordinary administrator credential"
        )
        assert get_current_product_admin not in callables, (
            f"{path} is behind the product-admin browser session"
        )
        assert get_product_admin_mutation not in callables, (
            f"{path} is behind the product-admin browser session"
        )
