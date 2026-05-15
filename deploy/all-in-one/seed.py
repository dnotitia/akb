"""All-in-one demo seed.

Idempotent: registers the demo user, creates the demo vault, and upserts
a fixed PAT into the tokens table so Glama (and other MCP clients) have
a stable token to introspect with.

Env vars (set by entrypoint.sh):
  DEMO_USERNAME, DEMO_EMAIL, DEMO_PASSWORD, DEMO_VAULT, DEMO_PAT
  POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys

import asyncpg
import httpx

BACKEND = os.environ.get("BACKEND_URL", "http://127.0.0.1:8000")
DEMO_USERNAME = os.environ["DEMO_USERNAME"]
DEMO_EMAIL = os.environ["DEMO_EMAIL"]
DEMO_PASSWORD = os.environ["DEMO_PASSWORD"]
DEMO_VAULT = os.environ["DEMO_VAULT"]
DEMO_PAT = os.environ["DEMO_PAT"]

PG = dict(
    host=os.environ.get("DB_HOST", "127.0.0.1"),
    port=int(os.environ.get("DB_PORT", "5432")),
    user=os.environ["POSTGRES_USER"],
    password=os.environ["POSTGRES_PASSWORD"],
    database=os.environ["POSTGRES_DB"],
)


async def wait_ready(c: httpx.AsyncClient, tries: int = 120) -> None:
    for _ in range(tries):
        try:
            r = await c.get("/readyz")
            if r.status_code == 200:
                return
        except Exception:
            pass
        await asyncio.sleep(2)
    sys.exit("backend never became ready")


async def main() -> None:
    async with httpx.AsyncClient(base_url=BACKEND, timeout=30) as c:
        await wait_ready(c)

        r = await c.post(
            "/api/v1/auth/register",
            json={
                "username": DEMO_USERNAME,
                "email": DEMO_EMAIL,
                "password": DEMO_PASSWORD,
                "display_name": "AKB Demo",
            },
        )
        print(f"register: {r.status_code}", flush=True)
        if r.status_code not in (200, 201, 400, 409):
            sys.exit(f"register failed: {r.text}")

        r = await c.post(
            "/api/v1/auth/login",
            json={"username": DEMO_USERNAME, "password": DEMO_PASSWORD},
        )
        r.raise_for_status()
        jwt = r.json()["token"]
        hdr = {"Authorization": f"Bearer {jwt}"}

        r = await c.post(
            "/api/v1/vaults",
            params={
                "name": DEMO_VAULT,
                "description": "All-in-one demo vault.",
                "public_access": "reader",
            },
            headers=hdr,
        )
        print(f"vault: {r.status_code}", flush=True)
        if r.status_code not in (200, 201, 400, 409):
            sys.exit(f"vault create failed: {r.text}")

    conn = await asyncpg.connect(**PG)
    try:
        user_id = await conn.fetchval(
            "SELECT id FROM users WHERE username=$1", DEMO_USERNAME
        )
        if user_id is None:
            sys.exit("user_id not found after register")
        th = hashlib.sha256(DEMO_PAT.encode()).hexdigest()
        tp = DEMO_PAT[:12]
        await conn.execute(
            """
            INSERT INTO tokens (user_id, name, token_hash, token_prefix, scopes)
            VALUES ($1, 'all-in-one-demo', $2, $3, ARRAY['read','write'])
            ON CONFLICT (token_hash) DO NOTHING
            """,
            user_id,
            th,
            tp,
        )
        print(f"PAT upserted (user_id={user_id})", flush=True)
    finally:
        await conn.close()
    print("seed: done", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
