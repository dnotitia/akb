"""The publication path may only use what every document service offers.

A publication resolves a document by asking the composed document service.
Two classes answer that call — `DocumentService` on the Git arm and
`NativeDocumentService` on the PostgreSQL one — and the second is what a
`postgres_native` deployment composes.

`NativeDocumentService` subclasses `DocumentService` but does not call
`super().__init__()`, so the `self.git` the base sets is simply absent on it.
A publication path that reached for that attribute therefore raised
`AttributeError` on every Native deployment: the document publications were
500s, and the only test covering that path used a fake carrying a `.git`, so
it modelled the legacy arm rather than the interface and stayed green.

These tests are structural on purpose. A behavioural test needs a database
and a composed service; this one reads the call sites and the two classes, so
it fails the moment a new reach is written rather than when someone opens a
publication in production.
"""

from __future__ import annotations

import ast
import pathlib

from app.services.document_service import DocumentService
from app.services.native_document_service import NativeDocumentService

_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_PUBLICATION = _BACKEND / "app" / "services" / "publication_service.py"


def _doc_service_attributes(source: str) -> set[str]:
    """Attributes the module reads off `_get_doc_service()`."""
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        value = node.value
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "_get_doc_service"
        ):
            found.add(node.attr)
    return found


def test_the_publication_path_only_uses_what_both_services_have():
    used = _doc_service_attributes(_PUBLICATION.read_text())
    assert used, "no _get_doc_service() attribute access found — has the helper been renamed?"
    missing = {
        name: [
            cls.__name__
            for cls in (DocumentService, NativeDocumentService)
            if not hasattr(cls, name)
        ]
        for name in sorted(used)
    }
    missing = {name: absent for name, absent in missing.items() if absent}
    assert not missing, (
        "the publication path reaches for attributes a composed document "
        f"service may not have: {missing}"
    )


def test_git_is_the_legacy_arms_storage_handle_not_part_of_the_interface():
    """Pins the asymmetry that produced the defect, so it cannot be assumed away.

    `git` exists on the base and not on the Native subclass. That is the
    fact every caller has to respect; a change that makes it uniform should
    have to update this test and say why, because the alternative — giving
    the Native service a Git handle it does not write through — trades a loud
    `AttributeError` for a silent write to the wrong store.
    """
    assert "git" not in _doc_service_attributes(_PUBLICATION.read_text())
    assert not hasattr(NativeDocumentService, "git")


def test_the_body_resolver_is_awaited_not_run_on_a_worker_thread():
    """The service is async; hopping to a thread would call a coroutine there.

    `asyncio.to_thread(_read_document_body_uncached, ...)` would return an
    un-awaited coroutine and the publication would render an empty body with
    no error, which is worse than the 500 this replaced.
    """
    tree = ast.parse(_PUBLICATION.read_text())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "to_thread"
        ):
            names = [a.id for a in node.args if isinstance(a, ast.Name)]
            assert "_read_document_body_uncached" not in names
            assert "_read_pinned_document_asset_ids" not in names
