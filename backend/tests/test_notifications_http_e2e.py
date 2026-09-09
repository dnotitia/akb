"""Opt-in HTTP + real worker proof, restricted to an explicit loopback runtime.

Run with AKB_NOTIFICATION_E2E_URL=http://127.0.0.1:18000. Accounts and the
private Vault are unique to this run and deleted in finally. No token is logged.
"""
import ipaddress
import os
import secrets
import time
import uuid
from urllib.parse import urlsplit

import httpx
import pytest


def _target():
    target = os.environ.get("AKB_NOTIFICATION_E2E_URL")
    if not target:
        pytest.skip("Set AKB_NOTIFICATION_E2E_URL to an isolated loopback runtime")
    parsed = urlsplit(target)
    try:
        loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        loopback = parsed.hostname == "localhost"
    if (not loopback or parsed.scheme not in {"http", "https"} or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        pytest.fail("Notification HTTP E2E requires a loopback origin without credentials or path")
    return target.rstrip("/")


def _request(client, method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    if not response.is_success:
        # Never print request headers, login bodies, or response bodies.
        pytest.fail(f"Notification E2E {method} {path} returned HTTP {response.status_code}")
    if path.startswith("/api/v1/notification"):
        assert response.headers.get("Cache-Control") == "private, no-store"
    return response.json() if response.content else {}


def _until(fetch, predicate, description, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = fetch()
        if predicate(result):
            return result
        time.sleep(0.25)
    pytest.fail(f"Notification worker did not reach {description} within {timeout}s")


def test_personal_inbox_http_and_background_worker():
    origin = _target()
    suffix = uuid.uuid4().hex[:12]
    vault = f"notify-e2e-{suffix}"
    title = f"Private notification fixture {suffix}"
    clients = []
    cleanup_errors = []
    owner = None
    vault_created = False
    with httpx.Client(base_url=origin, timeout=30, follow_redirects=False, trust_env=False) as public:
        try:
            usernames = []
            for role in ("owner", "reader"):
                username = f"notify-{role}-{suffix}"
                password = secrets.token_urlsafe(32)
                _request(public, "POST", "/api/v1/auth/register", json={
                    "username": username, "email": f"{username}@example.invalid", "password": password})
                login = _request(public, "POST", "/api/v1/auth/login", json={"username": username, "password": password})
                client = httpx.Client(base_url=origin, timeout=30, follow_redirects=False,
                                      trust_env=False, headers={"Authorization": f"Bearer {login['token']}"})
                clients.append(client)
                usernames.append(username)
                del login, password
            owner, reader = clients
            _request(owner, "POST", "/api/v1/vaults", params={"name": vault})
            vault_created = True
            created = _request(owner, "POST", "/api/v1/documents", json={
                "vault": vault, "collection": "", "title": title,
                "slug": "watched", "content": "Initial fixture body", "status": "active"})
            uri = created["uri"]
            path = f"/api/v1/documents/{vault}/{created['path']}"
            _request(owner, "POST", f"/api/v1/vaults/{vault}/grant", json={"user": usernames[1], "role": "reader"})
            def fetch():
                return _request(reader, "GET", "/api/v1/notifications")
            _until(fetch, lambda page: any(n["kind"] == "access.granted" for n in page["items"]), "grant delivery")
            assert _request(reader, "PUT", "/api/v1/notification-subscriptions", params={"uri": uri})["subscribed"]
            assert _request(reader, "GET", "/api/v1/notification-subscriptions", params={"uri": uri})["subscribed"]
            _request(owner, "PATCH", path, json={"content": "Watched update one"})
            page = _until(fetch, lambda p: any(n["kind"] == "document.update" for n in p["items"]), "watched update")
            item = next(n for n in page["items"] if n["kind"] == "document.update")
            assert item["target"]["uri"] == uri
            _request(reader, "PATCH", f"/api/v1/notifications/{item['id']}", json={"read": True, "version": item["version"]})
            assert next(n for n in fetch()["items"] if n["id"] == item["id"])["read"]
            _request(reader, "PATCH", f"/api/v1/notifications/{item['id']}", json={"read": False, "version": item["version"]})
            assert not next(n for n in fetch()["items"] if n["id"] == item["id"])["read"]
            snapshot = fetch()["snapshot"]
            _request(owner, "PATCH", path, json={"content": "Watched update two"})
            _until(fetch, lambda p: int(p["snapshot"]) > int(snapshot), "new grouped version")
            _request(reader, "POST", "/api/v1/notifications/mark-read", json={"snapshot": snapshot})
            assert _request(reader, "GET", "/api/v1/notifications/unread-count")["unread_count"] >= 1
            _request(reader, "POST", "/api/v1/notifications/mark-read", json={"snapshot": fetch()["snapshot"]})
            assert _request(reader, "GET", "/api/v1/notifications/unread-count")["unread_count"] == 0
            _request(reader, "DELETE", "/api/v1/notification-subscriptions", params={"uri": uri})
            before = fetch()["snapshot"]
            _request(owner, "PATCH", path, json={"content": "Unwatched update"})
            _until(lambda: _request(public, "GET", "/health"),
                   lambda h: h.get("notifications", {}).get("pending") == 0, "outbox drained")
            assert fetch()["snapshot"] == before
            _request(reader, "PUT", "/api/v1/notification-subscriptions", params={"uri": uri})
            _request(owner, "DELETE", path)
            page = _until(fetch, lambda p: any(n["kind"] == "document.delete" for n in p["items"]), "deletion notice")
            deleted = next(n for n in page["items"] if n["kind"] == "document.delete")
            assert deleted["target"] is None
            assert title not in str(page) and created["path"] not in str(page)
            _request(owner, "POST", f"/api/v1/vaults/{vault}/revoke", json={"user": usernames[1]})
            page = _until(fetch, lambda p: any(n["kind"] == "access.revoked" for n in p["items"]), "revocation notice")
            assert title not in str(page) and vault not in str(page)
            assert all(n["target"] is None for n in page["items"])
        finally:
            if owner and vault_created:
                for method, endpoint in (("POST", f"/api/v1/vaults/{vault}/archive"),
                                         ("DELETE", f"/api/v1/vaults/{vault}")):
                    try:
                        response = owner.request(method, endpoint)
                        if not response.is_success and response.status_code != 404:
                            cleanup_errors.append(f"Vault cleanup HTTP {response.status_code}")
                    except httpx.HTTPError:
                        cleanup_errors.append("Vault cleanup transport error")
            for client in reversed(clients):
                try:
                    response = client.delete("/api/v1/my/account")
                    if not response.is_success:
                        cleanup_errors.append(f"Account cleanup HTTP {response.status_code}")
                except httpx.HTTPError:
                    cleanup_errors.append("Account cleanup transport error")
                finally:
                    client.close()
            if cleanup_errors:
                pytest.fail("; ".join(cleanup_errors))
