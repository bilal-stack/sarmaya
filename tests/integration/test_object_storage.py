"""Uploads survive the machine they were uploaded to.

Local disk is correct on a developer machine and wrong on a hosted one, where
the filesystem is ephemeral: an invoice PDF lives until the next restart and
then does not. What makes that worse than ordinary data loss is what depends
on it — an evidence pack records the SHA-256 of every attachment it
references, so files disappearing turns a sealed audit document into hashes
pointing at nothing. It still verifies its own seal. It is still useless.

These tests exercise the S3 backend against moto's in-process implementation
of the S3 API rather than a hand-written stub, because a stub would only prove
the stub agrees with itself.

The property most worth protecting is in TestSwitchingBackendsDoesNotStrand:
a deployment that moves to object storage must still be able to read what it
already wrote to disk.
"""
import hashlib
import uuid

import boto3
import pytest
from moto import mock_aws

from app.core.config import settings
from app.services import storage
from app.services.storage_backends import backend_for, reset_cache

pytestmark = pytest.mark.integration

BUCKET = "sarmaya-test-bucket"
PDF = b"%PDF-1.4 fake invoice bytes"


@pytest.fixture
def s3(monkeypatch):
    """An S3 backend pointed at an in-process bucket."""
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "s3")
    monkeypatch.setattr(settings, "STORAGE_BUCKET", BUCKET)
    monkeypatch.setattr(settings, "STORAGE_ENDPOINT_URL", "")
    monkeypatch.setattr(settings, "STORAGE_REGION", "us-east-1")
    monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", "testing")
    reset_cache()
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield backend_for("s3")
    reset_cache()


@pytest.fixture
def local(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    reset_cache()
    yield backend_for("local")
    reset_cache()


class TestTheBytesComeBackUnchanged:
    def test_round_trip(self, s3):
        key, digest = s3.save("tenant-a", "invoice.pdf", PDF)

        assert s3.read(key) == PDF
        assert digest == hashlib.sha256(PDF).hexdigest()

    def test_the_hash_is_of_the_content_not_the_storage(self, s3, local):
        """An evidence pack's attachment hash must mean the same thing
        wherever the file was put, or moving backends silently invalidates
        every pack that referenced one."""
        _, from_s3 = s3.save("tenant-a", "invoice.pdf", PDF)
        _, from_local = local.save("tenant-a", "invoice.pdf", PDF)

        assert from_s3 == from_local == hashlib.sha256(PDF).hexdigest()

    def test_the_hash_is_stored_with_the_object(self, s3):
        """So the two copies of the claim can be compared later, rather than
        each asserting the other is right."""
        key, digest = s3.save("tenant-a", "invoice.pdf", PDF)

        head = boto3.client("s3", region_name="us-east-1").head_object(
            Bucket=BUCKET, Key=key
        )
        assert head["Metadata"]["sha256"] == digest


class TestKeys:
    def test_the_tenant_is_the_prefix(self, s3):
        """A natural boundary for a bucket policy, a lifecycle rule, or an
        export of one customer's documents."""
        key, _ = s3.save("tenant-a", "invoice.pdf", PDF)

        assert key.startswith("tenant-a/")

    def test_a_hostile_filename_cannot_escape_the_prefix(self, s3):
        """The filename comes from whoever uploaded the file."""
        key, _ = s3.save("tenant-a", "../../../etc/passwd", PDF)

        assert key.startswith("tenant-a/")
        assert ".." not in key

    def test_control_characters_are_stripped(self, s3):
        """A key travels through URLs, logs and CLI arguments."""
        key, _ = s3.save("tenant-a", "in\nvoice \"';.pdf", PDF)

        assert "\n" not in key and '"' not in key and "'" not in key

    def test_two_tenants_uploading_the_same_file_do_not_collide(self, s3):
        key_a, _ = s3.save("tenant-a", "invoice.pdf", PDF)
        key_b, _ = s3.save("tenant-b", "invoice.pdf", PDF)

        assert key_a != key_b

    def test_a_filename_that_sanitises_to_nothing_still_gets_a_key(self, s3):
        key, _ = s3.save("tenant-a", "...", PDF)

        assert key.startswith("tenant-a/") and key.rsplit("/", 1)[1]


class TestLocalPath:
    """Every OCR provider takes a path and opens it, so backends promise one
    for the duration of a block rather than the providers being rewritten."""

    def test_object_storage_materialises_a_readable_file(self, s3):
        key, _ = s3.save("tenant-a", "invoice.pdf", PDF)

        with s3.local_path(key) as path:
            with open(path, "rb") as f:
                assert f.read() == PDF

    def test_the_temporary_file_is_removed_afterwards(self, s3):
        import os

        key, _ = s3.save("tenant-a", "invoice.pdf", PDF)
        with s3.local_path(key) as path:
            captured = path

        assert not os.path.exists(captured)

    def test_it_keeps_the_extension(self, s3):
        """Some OCR providers branch on the file extension."""
        key, _ = s3.save("tenant-a", "scan.pdf", PDF)

        with s3.local_path(key) as path:
            assert path.endswith(".pdf")

    def test_local_storage_copies_nothing(self, local):
        """The file is already on disk; yielding a copy would be waste."""
        key, _ = local.save("tenant-a", "invoice.pdf", PDF)

        with local.local_path(key) as path:
            assert path == key


class TestSwitchingBackendsDoesNotStrand:
    """The property that makes this a migration rather than data loss."""

    def test_a_local_file_is_still_readable_once_the_default_is_s3(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
        monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
        reset_cache()
        key, digest, backend_name = storage.save_file("tenant-a", "old.pdf", PDF)
        assert backend_name == "local"

        # The deployment switches. Existing rows still say "local".
        monkeypatch.setattr(settings, "STORAGE_BACKEND", "s3")
        monkeypatch.setattr(settings, "STORAGE_BUCKET", BUCKET)

        assert storage.read_file(key, "local") == PDF
        assert hashlib.sha256(storage.read_file(key, "local")).hexdigest() == digest
        reset_cache()

    def test_save_reports_which_backend_wrote_it(self, s3):
        """Returned rather than assumed — it is what a later read dispatches
        on, and it is recorded on the file row."""
        _, _, backend_name = storage.save_file("tenant-a", "invoice.pdf", PDF)

        assert backend_name == "s3"


class TestDeleting:
    def test_it_removes_the_object(self, s3):
        key, _ = s3.save("tenant-a", "invoice.pdf", PDF)
        s3.delete(key)

        with pytest.raises(FileNotFoundError):
            s3.read(key)

    def test_deleting_something_absent_is_not_an_error(self, s3):
        """Delete is called to clean up after a failed upload. A cleanup that
        raises because the thing was never written is noise, and would mask
        the original failure it was cleaning up after."""
        s3.delete(f"tenant-a/{uuid.uuid4().hex}_never-written.pdf")

    def test_a_missing_key_reads_as_file_not_found(self, s3):
        """Not a boto ClientError — callers should not have to know which
        backend they are talking to in order to handle a missing file."""
        with pytest.raises(FileNotFoundError):
            s3.read("tenant-a/does-not-exist.pdf")


class TestConfiguration:
    def test_an_unknown_backend_is_refused_by_name(self):
        with pytest.raises(ValueError, match="not a storage backend"):
            backend_for("dropbox")

    def test_s3_without_a_bucket_refuses_to_start(self, monkeypatch):
        monkeypatch.setattr(settings, "STORAGE_BUCKET", "")
        reset_cache()
        with pytest.raises(ValueError, match="STORAGE_BUCKET"):
            backend_for("s3")
        reset_cache()

    def test_local_is_the_default(self, monkeypatch):
        """`docker compose up` must need nothing configured."""
        reset_cache()
        assert storage.backend_for(settings.STORAGE_BACKEND).name == "local"
