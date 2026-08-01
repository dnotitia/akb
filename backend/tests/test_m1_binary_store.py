import hashlib
from pathlib import Path
import pytest
from app.services.m1_binary_store import FilesystemCAS, MeasurementUpload

def test_fscas_adopts_exact_bytes_and_never_overwrites(tmp_path: Path):
    store = FilesystemCAS(tmp_path); data = b"binary\x00bytes"; digest = hashlib.sha256(data).hexdigest()
    first = store.prepare_verified("tenant", data, digest, len(data)); second = store.prepare_verified("tenant", data, digest, len(data))
    assert first == second and store.open_verified("tenant", first) == data

def test_upload_is_nonvisible_until_confirm_and_classifies_failed_publish(tmp_path: Path):
    upload = MeasurementUpload(FilesystemCAS(tmp_path), "tenant")
    assert upload.confirm(None, None, lambda _: None) == ("unconfirmed_not_visible", None)
    upload.transfer(b"x")
    assert upload.confirm(None, None, lambda _: (_ for _ in ()).throw(RuntimeError())) == ("failed_db_publish_unconfirmed", None)

def test_fscas_rejects_digest_mismatch(tmp_path: Path):
    with pytest.raises(Exception): FilesystemCAS(tmp_path).prepare_verified("tenant", b"x", "0" * 64, 1)
