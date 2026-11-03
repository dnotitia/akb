"""Directory visibility, filters and keyset traversal against an owned database."""

import importlib.util
import os
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.api.deps import get_current_user
from app.api.routes import publication_directory as route
from app.config import settings
from app.exceptions import AKBError, ForbiddenError
from app.models.vault_scope import VaultScope
from app.services import publication_directory as directory
from app.services.auth_service import AuthenticatedUser
from app.services.native_revision_service import NativeRevisionService

ROOT = Path(__file__).resolve().parents[1]
DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:5433/akb")  # pragma: allowlist secret


@pytest_asyncio.fixture
async def inventory(monkeypatch):
    try:
        admin = await asyncpg.connect(DSN, timeout=2)
    except (OSError, asyncpg.PostgresError):
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail("Required directory PostgreSQL fixture is unreachable")
        pytest.skip("Directory PostgreSQL fixture is unreachable")
    name = f"akb_public_directory_{uuid.uuid4().hex[:12]}"
    pool = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        pool = await asyncpg.create_pool(f"{DSN.rsplit('/', 1)[0]}/{name}", min_size=1, max_size=3)
        async with pool.acquire() as conn:
            await conn.execute((ROOT / "app/db/init.sql").read_text())
            for filename in ("010_external_git_mirror.py", "015_events_outbox.py", "044_vault_write_policy.py", "045_vault_write_grant_actions.py", "048_native_revision_core.py", "053_native_revision_m1_pg_body.py", "055_native_revision_m1_file_storage.py", "057_native_revision_m1_payload_placement.py", "106_native_document_publications.py", "107_native_document_asset_refs.py"):
                spec = importlib.util.spec_from_file_location(f"directory_fixture_{filename}", ROOT / "app/db/migrations" / filename)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                await module.migrate(conn=conn)
            owner, other = uuid.uuid4(), uuid.uuid4()
            for uid, username in ((owner, "owner"), (other, "other")):
                await conn.execute("INSERT INTO users(id, username, email, password_hash) VALUES($1,$2,$2 || '@example.invalid','unusable')", uid, username)
            vaults = {}
            for vault, access, status, uid in (("owned", "none", "active", owner), ("member", "none", "active", other), ("public", "reader", "active", other), ("hidden", "none", "active", other), ("archived", "none", "archived", owner), ("mirror", "none", "active", owner), ("managed", "none", "active", owner)):
                vaults[vault] = await conn.fetchval("INSERT INTO vaults(name, git_path, owner_id, public_access, status) VALUES($1,'/tmp/unused.git',$2,$3,$4) RETURNING id", vault, uid, access, status)
            await conn.execute("INSERT INTO vault_access(vault_id,user_id,role) VALUES($1,$2,'writer')", vaults["member"], owner)
            await conn.execute("INSERT INTO vault_external_git(vault_id,remote_url) VALUES($1,'https://example.invalid/repo.git')", vaults["mirror"])
            await conn.execute("INSERT INTO vault_write_policy(vault_id,managed_by,created_by) VALUES($1,'collector','fixture')", vaults["managed"])
            now = datetime.now(timezone.utc)
            for slug, vault, created, expires, cap, views, title in (
                ("active-a", "owned", now-timedelta(days=2), None, None, 0, "Guide 100%_literal"),
                ("active-b", "member", now-timedelta(days=2), now+timedelta(days=2), None, 0, "Member guide"),
                ("expired", "owned", now-timedelta(days=3), now-timedelta(days=1), None, 0, "Expired guide"),
                ("capped", "public", now-timedelta(days=4), None, 2, 2, "Capped guide"),
                ("old", "owned", now-timedelta(days=40), None, None, 0, "Old guide"),
                ("secret", "hidden", now-timedelta(hours=1), None, None, 0, "Hidden secret"),
                ("archive", "archived", now-timedelta(days=5), None, None, 0, "Archive guide"),
                ("mirror", "mirror", now-timedelta(days=6), None, None, 0, "Mirror guide"),
                ("managed", "managed", now-timedelta(days=7), None, None, 0, "Managed guide"),
            ):
                await conn.execute("""INSERT INTO publications(slug,vault_id,resource_type,resource_uri,title,created_at,expires_at,max_views,view_count,password_hash,snapshot_s3_key)
                    VALUES($1,$2,'document',$3,$4,$5,$6,$7,$8,'private-hash','private-key')""", slug, vaults[vault], f"akb://{vault}/doc/{slug}.md", title, created, expires, cap, views)
        async def get_pool():
            return pool
        monkeypatch.setattr(directory, "get_pool", get_pool)
        monkeypatch.setattr(settings, "public_base_url", "https://publication.example.invalid")
        yield pool, AuthenticatedUser(str(owner), "owner", "owner@example.invalid", None, False, "jwt"), vaults
    finally:
        if pool:
            await pool.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


@pytest.mark.asyncio
async def test_visibility_capabilities_and_safe_response(inventory):
    _, user, _ = inventory
    result = await directory.list_directory(user, status="all")
    rows = {p["slug"]: p for p in result["publications"]}
    assert result["total"] == 8 and result["complete"]
    assert "secret" not in rows
    assert rows["active-a"]["can_unpublish"] and rows["active-b"]["can_unpublish"]
    assert all(not rows[slug]["can_unpublish"] for slug in ("capped", "archive", "mirror", "managed"))
    assert rows["expired"]["availability"] == "expired"
    assert rows["capped"]["availability"] == "view-limit-reached"
    assert rows["active-a"]["password_protected"]
    assert not {"password_hash", "snapshot_s3_key", "created_by", "vault_id", "id"} & rows["active-a"].keys()
    readonly = await directory.list_directory(replace(user, token_scopes=frozenset({"read"})))
    assert all(not p["can_unpublish"] for p in readonly["publications"])
    with pytest.raises(ForbiddenError):
        await directory.list_directory(user, vault="hidden")


@pytest.mark.asyncio
async def test_server_filters_precede_paging_and_literal_search(inventory):
    _, user, _ = inventory
    unavailable = await directory.list_directory(user, status="unavailable", page_size=1)
    assert unavailable["total"] == 2 and len(unavailable["publications"]) == 1
    soon = await directory.list_directory(user, expiry="soon")
    assert [p["slug"] for p in soon["publications"]] == ["active-b"]
    old = await directory.list_directory(user, expiry="long-running")
    assert [p["slug"] for p in old["publications"]] == ["old"]
    literal = await directory.list_directory(user, query="100%_literal")
    assert [p["slug"] for p in literal["publications"]] == ["active-a"]
    assert (await directory.list_directory(user, vault="member", query="Member"))["total"] == 1
    assert (await directory.list_directory(user, query="Hidden"))["total"] == 0
    no_expiry = await directory.list_directory(user, expiry="none", status="all")
    assert all(p["expires_at"] is None for p in no_expiry["publications"])


@pytest.mark.asyncio
async def test_cursor_ties_binding_and_acl_recheck(inventory):
    pool, user, vaults = inventory
    first = await directory.list_directory(user, status="all", page_size=1)
    assert first["publications"][0]["slug"] == "active-b"
    cursor = first["next_cursor"]
    for options in ({"status": "active"}, {"page_size": 2}, {"vault": "owned"}):
        with pytest.raises(AKBError):
            await directory.list_directory(user, status=options.get("status", "all"), page_size=options.get("page_size", 1), vault=options.get("vault"), cursor=cursor)
    with pytest.raises(AKBError):
        await directory.list_directory(user, status="all", page_size=1, cursor=cursor[:-5]+"xxxxx")
    with pytest.raises(AKBError):
        await directory.list_directory(replace(user, user_id=str(uuid.uuid4())), status="all", page_size=1, cursor=cursor)
    slugs = [first["publications"][0]["slug"]]
    while cursor:
        page = await directory.list_directory(user, status="all", page_size=1, cursor=cursor)
        slugs.extend(p["slug"] for p in page["publications"])
        cursor = page["next_cursor"]
    assert len(slugs) == len(set(slugs)) == 8
    # An ACL revocation between requests cannot be hidden by a signed cursor.
    scoped = await directory.list_directory(user, vault="member", page_size=1)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM vault_access WHERE vault_id=$1", vaults["member"])
    with pytest.raises(ForbiddenError):
        await directory.list_directory(user, vault="member", page_size=1, cursor=scoped["next_cursor"])
    assert (await directory.list_directory(user, status="all", page_size=1, cursor=first["next_cursor"]))["total"] == 7


@pytest.mark.asyncio
async def test_managed_grants_admin_changes_and_scoped_credentials(inventory):
    pool, user, vaults = inventory
    token = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO tokens(id,user_id,name,token_hash,token_prefix) VALUES($1,$2,'fixture','unusable-hash','fixture')", token, uuid.UUID(user.user_id))
        await conn.execute("INSERT INTO vault_write_grants(vault_id,token_id,granted_by) VALUES($1,$2,'fixture')", vaults["managed"], token)
    credential = replace(user, token_id=str(token), vault_scope=VaultScope((), frozenset({"managed"})))
    result = await directory.list_directory(credential)
    rows = {p["slug"]: p for p in result["publications"]}
    assert rows["managed"]["can_unpublish"]
    assert not rows["active-a"]["can_unpublish"]
    async with pool.acquire() as conn:
        await conn.execute("UPDATE vault_write_grants SET write_actions=ARRAY['file_upload'] WHERE token_id=$1", token)
        await conn.execute("UPDATE users SET is_admin=TRUE WHERE id=$1", uuid.UUID(user.user_id))
    result = await directory.list_directory(credential)
    assert not next(p for p in result["publications"] if p["slug"] == "managed")["can_unpublish"]
    # Fresh database authority overrides the cached account flag, with archive
    # and mirror restrictions still applying to the system administrator.
    rows = {p["slug"]: p for p in (await directory.list_directory(user))["publications"]}
    assert rows["secret"]["can_unpublish"] and rows["managed"]["can_unpublish"]
    assert not rows["archive"]["can_unpublish"] and not rows["mirror"]["can_unpublish"]
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET is_admin=FALSE WHERE id=$1", uuid.UUID(user.user_id))
        await conn.execute("UPDATE vault_write_policy SET managed_by='' WHERE vault_id=$1", vaults["managed"])
    result = await directory.list_directory(replace(user, is_admin=True), query="Hidden")
    assert result["total"] == 0
    assert not (await directory.list_directory(user, vault="managed"))["publications"][0]["can_unpublish"]


@pytest.mark.asyncio
async def test_native_identity_title_does_not_follow_reused_uri(inventory):
    pool, user, vaults = inventory
    service = NativeRevisionService(pool)
    original = await service.create_text(namespace_id=vaults["owned"], surface="document", path="moved.md", payload="# Original", actor="fixture", mutation_id=uuid.uuid4(), resource_id=uuid.uuid4())
    await service.create_text(namespace_id=vaults["owned"], surface="document", path="active-a.md", payload="# Replacement", actor="fixture", mutation_id=uuid.uuid4(), resource_id=uuid.uuid4())
    async with pool.acquire() as conn:
        await conn.execute("UPDATE publications SET native_document_id=$1,title=NULL WHERE slug='active-a'", original.resource_id)
    result = await directory.list_directory(user, query="moved.md")
    assert [p["slug"] for p in result["publications"]] == ["active-a"]
    assert result["publications"][0]["title"] == "moved.md"
    assert result["publications"][0]["resource_path"] == "moved.md"
    assert result["publications"][0]["resource_uri"] == "akb://owned/doc/moved.md"


@pytest.mark.asyncio
async def test_http_inventory_uses_real_database_and_registered_route(inventory):
    from app.api.routes.public import router

    _, user, _ = inventory
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://example.test") as client:
        response = await client.get("/api/v1/publications?vault=owned&status=all&page_size=1")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 3 and len(body["publications"]) == 1
        assert body["next_cursor"] and not body["complete"]
        assert body["publications"][0]["vault"] == "owned"
        assert "password_hash" not in body["publications"][0]


@pytest.mark.asyncio
async def test_route_is_authenticated_and_validates_filters(monkeypatch):
    app = FastAPI()
    app.include_router(route.router, prefix="/api/v1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://example.test") as client:
        assert (await client.get("/api/v1/publications")).status_code == 401
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(str(uuid.uuid4()), "test", "test@example.invalid", None, False, "jwt")
        calls = []
        async def fake(user, **kwargs):
            calls.append(kwargs)
            return {"publications": [], "total": 0, "complete": True, "next_cursor": None}
        monkeypatch.setattr(route, "list_directory", fake)
        for query in ("status=revoked", "expiry=yesterday", "page_size=101", "page_size=0"):
            assert (await client.get(f"/api/v1/publications?{query}")).status_code == 422
        assert (await client.get("/api/v1/publications?vault=owned&q=guide&expiry=soon")).status_code == 200
        assert calls[0]["vault"] == "owned" and calls[0]["query"] == "guide"
