"""Private account assets; never Vault files, Git revisions, or search input."""

from __future__ import annotations

import uuid

from app.services.s3_delete_worker import enqueue_delete

AVATAR_KEY_PREFIX = ".akb-account-assets/avatars/"


async def current(conn, user_id: uuid.UUID):
    return await conn.fetchrow("SELECT * FROM user_avatars WHERE user_id=$1", user_id)


async def publish(conn, user_id: uuid.UUID, revision: uuid.UUID, object_key: str | None,
                  size_bytes: int | None = None) -> None:
    await conn.execute("""
        INSERT INTO user_avatars(user_id,revision,object_key,content_type,size_bytes,width,height)
        VALUES($1,$2,$3,CASE WHEN $3::text IS NULL THEN NULL ELSE 'image/webp' END,
               $4,CASE WHEN $3::text IS NULL THEN NULL ELSE 512 END,
               CASE WHEN $3::text IS NULL THEN NULL ELSE 512 END)
        ON CONFLICT(user_id) DO UPDATE SET revision=EXCLUDED.revision,
            object_key=EXCLUDED.object_key,content_type=EXCLUDED.content_type,
            size_bytes=EXCLUDED.size_bytes,width=EXCLUDED.width,height=EXCLUDED.height,
            updated_at=clock_timestamp()
    """, user_id, revision, object_key, size_bytes)


async def enqueue_account_cleanup(conn, user_id: uuid.UUID) -> None:
    """Called with the users row locked, before either account deletion path."""
    row = await current(conn, user_id)
    if row is not None and row["object_key"]:
        await enqueue_delete(conn, row["object_key"])


async def consume_write_budget(conn, user_id: uuid.UUID) -> bool:
    attempts = await conn.fetchval("""
        INSERT INTO user_avatar_write_budget(user_id,window_start,attempts)
        VALUES($1,clock_timestamp(),1) ON CONFLICT(user_id) DO UPDATE SET
            window_start=CASE WHEN user_avatar_write_budget.window_start
                < clock_timestamp()-interval '1 minute' THEN clock_timestamp()
                ELSE user_avatar_write_budget.window_start END,
            attempts=CASE WHEN user_avatar_write_budget.window_start
                < clock_timestamp()-interval '1 minute' THEN 1
                ELSE user_avatar_write_budget.attempts+1 END
        RETURNING attempts
    """, user_id)
    return attempts <= 10
