"""The database URL survives credentials that contain URL delimiters.

`asyncpg_dsn` (and `database_url`, the same URL) is what every connection is
opened with, the main pool included. It used to put the password in verbatim,
so a generated password with a "/" moved part of it into the host, and the
backend looked up a host that does not exist. These tests parse the URL with
the parser that consumes it, asyncpg's, which is pinned in pyproject.toml.
"""

from __future__ import annotations

import pytest
from asyncpg import connect_utils

from app.config import Settings

# One password per delimiter, plus the characters base64 and people add.
PASSWORDS = [
    "ab/cd",
    "ab@cd",
    "ab:cd",
    "ab?cd",
    "ab#cd",
    "ab%2Fcd",
    "ab+cd=",
    "ab cd",
    "a/b@c:d?e#f%g+h=i j",
]


def _parse(dsn: str):
    """Read a DSN exactly as asyncpg.connect does."""
    return connect_utils._parse_connect_dsn_and_args(
        dsn=dsn,
        host=None,
        port=None,
        user=None,
        password=None,
        passfile=None,
        database=None,
        ssl=None,
        service=None,
        servicefile=None,
        direct_tls=False,
        server_settings=None,
        target_session_attrs=None,
        krbsrvname=None,
        gsslib=None,
    )


def _settings(**overrides) -> Settings:
    values = {"db_host": "postgres", "db_port": 5432, "db_user": "akbuser", "db_name": "akb"}
    values.update(overrides)
    return Settings(document_revision_backend="bare_git", **values)


@pytest.mark.parametrize("password", PASSWORDS)
def test_asyncpg_reads_back_the_exact_password_and_the_real_host(password: str) -> None:
    addresses, params = _parse(_settings(db_password=password).asyncpg_dsn)

    assert addresses == [("postgres", 5432)]
    assert params.password == password
    assert params.user == "akbuser"
    assert params.database == "akb"


def test_user_and_database_are_encoded_the_same_way() -> None:
    configured = _settings(db_user="akb:user@x", db_name="akb/dev", db_password="pw")
    addresses, params = _parse(configured.asyncpg_dsn)

    assert addresses == [("postgres", 5432)]
    assert (params.user, params.database, params.password) == ("akb:user@x", "akb/dev", "pw")


def test_database_url_is_the_same_url() -> None:
    configured = _settings(db_password="ab/cd")

    assert configured.database_url == configured.asyncpg_dsn


def test_a_plain_password_keeps_the_url_it_always_had() -> None:
    configured = _settings(db_password="akb-secure-password-change-me")

    assert configured.asyncpg_dsn == (
        "postgresql://akbuser:akb-secure-password-change-me@postgres:5432/akb"  # pragma: allowlist secret
    )
