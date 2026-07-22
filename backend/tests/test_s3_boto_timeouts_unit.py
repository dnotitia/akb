"""Regression: every S3 client must carry bounded connect/read timeouts.

boto3/botocore default to a 60 s connect AND 60 s read timeout with NO retries.
Several S3 primitives run on the single event loop (public raw-file read,
snapshot put/read, HEAD confirm, bucket cold-start), so a stalled MinIO/S3
would block the loop for up to 60 s → /livez probe timeout → 503. `make_client`
now stamps `settings.s3_*` timeouts + a short retry onto the boto config; this
test pins that wiring so a future refactor can't silently drop back to 60 s.

Network-free: `boto3.client(...)` resolves config offline (no call is made).
DB-free. Runs in `pytest -k 'not _e2e'`.
"""

from app.config import settings
from app.services.adapters import s3_adapter


def test_make_client_applies_bounded_timeouts_from_settings():
    c = s3_adapter.make_client("http://minio:9000", "ak", "sk", region="us-east-1")
    cfg = c._client_config
    assert cfg.connect_timeout == settings.s3_connect_timeout_secs
    assert cfg.read_timeout == settings.s3_read_timeout_secs
    # botocore's `max_attempts` is the retry count → total attempts = +1.
    assert cfg.retries["mode"] == "standard"
    assert cfg.retries["total_max_attempts"] == settings.s3_max_attempts + 1


def test_make_client_timeouts_are_well_under_boto_default():
    # The whole point is to escape the 60 s boto default. Guard the ceiling so a
    # careless settings bump can't reintroduce a minute-long on-loop stall.
    c = s3_adapter.make_client("http://minio:9000", "ak", "sk")
    cfg = c._client_config
    assert 0 < cfg.connect_timeout <= 10
    assert 0 < cfg.read_timeout <= 30
    # Worst-case read hang = total attempts × read_timeout must stay bounded.
    total_attempts = cfg.retries["total_max_attempts"]
    assert total_attempts * cfg.read_timeout <= 60


def test_make_client_reads_settings_live(monkeypatch):
    # Proves the timeouts come from settings (not a hardcode) — override and
    # confirm a freshly built client reflects the new values.
    monkeypatch.setattr(settings, "s3_connect_timeout_secs", 1.5)
    monkeypatch.setattr(settings, "s3_read_timeout_secs", 4.0)
    monkeypatch.setattr(settings, "s3_max_attempts", 1)
    c = s3_adapter.make_client("http://minio:9000", "ak", "sk")
    cfg = c._client_config
    assert cfg.connect_timeout == 1.5
    assert cfg.read_timeout == 4.0
    assert cfg.retries["total_max_attempts"] == 2
