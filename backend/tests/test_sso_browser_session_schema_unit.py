"""Static guard for the ordinary SSO browser-session schema rollout."""

from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[1]


def test_fresh_and_migrated_schema_share_the_custody_invariants():
    init_sql = (_BACKEND / "app" / "db" / "init.sql").read_text()
    migration = (_BACKEND / "app" / "db" / "migrations" / "074_sso_browser_sessions.py").read_text()

    for source in (init_sql, migration):
        assert "CREATE TABLE IF NOT EXISTS sso_browser_sessions" in source
        assert "token_hash TEXT NOT NULL UNIQUE" in source
        assert "csrf_token_hash TEXT NOT NULL" in source
        assert "token_envelope TEXT NOT NULL" in source
        assert "FOREIGN KEY (external_identity_id, user_id)" in source
        assert "access_expires_at TIMESTAMPTZ NOT NULL" in source
        assert "refresh_expires_at TIMESTAMPTZ NOT NULL" in source
        assert "idle_expires_at TIMESTAMPTZ NOT NULL" in source
        assert "absolute_expires_at TIMESTAMPTZ NOT NULL" in source
        assert "identity_issuer, keycloak_sid" in source
        assert "identity_issuer, identity_subject" in source
        assert "CREATE TABLE IF NOT EXISTS sso_browser_logout_fences" in source
        assert "logout_issued_at TIMESTAMPTZ NOT NULL" in source
        assert "PRIMARY KEY (identity_issuer, keycloak_sid)" in source
        assert "idx_sso_browser_logout_fences_expiry" in source


def test_browser_session_migration_is_registered():
    registry = (_BACKEND / "app" / "db" / "postgres.py").read_text()

    assert '"074_sso_browser_sessions.py"' in registry
