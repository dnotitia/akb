"""Both document arms must do the same asset work, or images break silently.

An inline image is readable only while a live `document_asset_refs` row names
it. The Git arm publishes that row on every write; the Native arm published it
on none, because the asset subsystem was simply never wired into it. Nothing
raised — `akb_put_image` succeeded, `akb_put` succeeded, and the document
rendered a broken image — so no test, no gate and no log noticed for eight
days.

These tests are structural for the same reason the publication-contract ones
are: proving the behaviour needs a database, a payload store and a composed
service, while the defect was visible in the shape of the two modules. They
fail when an arm stops doing what the other one does, rather than when a user
opens a document.
"""

from __future__ import annotations

import ast
import pathlib

_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_SERVICES = _BACKEND / "app" / "services"
_GIT_ARM = (
    _SERVICES / "document_service.py",
    _BACKEND / "app" / "repositories" / "document_repo.py",
)
_NATIVE_ARM = _SERVICES / "native_document_service.py"

# The Native write entry points. `edit` is absent on purpose: it composes a new
# body and delegates to `_update_from_snapshot`, so that is where its assets
# are handled — a test naming `edit` here would pass for the wrong reason.
_NATIVE_WRITE_METHODS = ("put", "_update_from_snapshot", "move", "delete")


def _asset_service_calls(source: str) -> set[str]:
    """Names called as `asset_service.<name>(...)` anywhere in a module."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "asset_service"
        ):
            found.add(node.func.attr)
    return found


def _method(source: str, name: str) -> ast.AST:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found — has the write path been renamed?")


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Attribute):
                names.add(func.attr)
            elif isinstance(func, ast.Name):
                names.add(func.id)
    return names


def test_the_native_arm_uses_every_asset_entry_point_the_git_arm_uses():
    git: set[str] = set()
    for path in _GIT_ARM:
        git |= _asset_service_calls(path.read_text())
    assert git, "no asset_service call found on the Git arm — has it been renamed?"

    native = _asset_service_calls(_NATIVE_ARM.read_text())
    missing = git - native
    assert not missing, (
        "the Native document service never calls these, so the work they do "
        f"simply does not happen on a postgres_native deployment: {sorted(missing)}"
    )


def test_every_native_write_path_touches_the_asset_helpers():
    """A new write path that forgets images should fail here, not in a browser."""
    source = _NATIVE_ARM.read_text()
    helpers = {
        "_claim_body_assets",
        "_sync_body_assets",
        "_retain_body_assets_for_delete",
        # `move` carries an unchanged body, so it reads the live set directly.
        "list_live_document_asset_ids",
    }
    for name in _NATIVE_WRITE_METHODS:
        called = _called_names(_method(source, name))
        assert called & helpers, (
            f"{name} performs a Native document write without touching the "
            "asset helpers; its images would never become readable"
        )


def test_the_import_policy_argument_is_used_rather_than_discarded():
    """`allow_unavailable_asset_refs` decides whether a broken ref is fatal.

    It used to be accepted and immediately `del`-ed, which is how the Native
    arm could claim to implement the shared interface while ignoring the only
    argument that interface has about assets.
    """
    source = _NATIVE_ARM.read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Delete):
            for target in node.targets:
                assert not (
                    isinstance(target, ast.Name)
                    and target.id == "allow_unavailable_asset_refs"
                ), "the Native arm discards the import policy instead of applying it"

    put = _method(source, "put")
    used = {
        n.id for n in ast.walk(put)
        if isinstance(n, ast.Name) and n.id == "allow_unavailable_asset_refs"
    }
    assert used, "put ignores allow_unavailable_asset_refs"
