"""S3-compatible object storage.

Written against the S3 API rather than one vendor's SDK, because every option
worth using speaks it: Neon Object Storage, Cloudflare R2, MinIO, and S3
itself. The only thing that changes between them is the endpoint, so that is a
setting rather than a code path — the same reasoning the finance connectors
follow, where the provider is a value and not a branch.

boto3 is already a dependency (it was pulled in for AWS Textract), so this
adds no new package.
"""
import hashlib
import logging
import os
import re
import tempfile
from contextlib import contextmanager
from typing import Iterator, Tuple

from app.core.config import settings
from app.services.storage_backends.base import StorageBackend

logger = logging.getLogger(__name__)

#: Anything outside this is replaced. A key travels through URLs, logs and CLI
#: arguments, and an uploaded filename is attacker-controlled — "../../etc" and
#: a filename containing a newline are both things a user can send.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _safe(filename: str) -> str:
    cleaned = _UNSAFE.sub("_", os.path.basename(filename or "")).strip("._")
    return cleaned[:120] or "upload"


class S3Storage(StorageBackend):
    name = "s3"

    def __init__(self):
        import boto3
        from botocore.config import Config

        self._bucket = settings.STORAGE_BUCKET
        if not self._bucket:
            raise ValueError(
                "STORAGE_BUCKET must be set when STORAGE_BACKEND is 's3'."
            )

        self._client = boto3.client(
            "s3",
            # Empty means real AWS. Anything else — Neon, R2, MinIO — supplies
            # its own endpoint.
            endpoint_url=settings.STORAGE_ENDPOINT_URL or None,
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID or None,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY or None,
            region_name=settings.STORAGE_REGION or settings.AWS_REGION,
            config=Config(
                signature_version="s3v4",
                # Bounded on purpose. An upload runs inside the request that
                # made it, so a storage endpoint that stops answering must
                # fail rather than hold a worker — the same lesson the OCR
                # call taught, where a missing timeout could pin a worker
                # until the process restarted.
                connect_timeout=5,
                read_timeout=60,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    def save(self, tenant_id: str, filename: str, content: bytes) -> Tuple[str, str]:
        file_hash = hashlib.sha256(content).hexdigest()
        # Tenant first, so the prefix is a natural boundary for a bucket
        # policy, a lifecycle rule, or an export of one customer's documents.
        key = f"{tenant_id}/{file_hash[:16]}_{_safe(filename)}"

        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=content,
            # The integrity claim, stored with the object rather than only in
            # our database — so the two can be compared later instead of each
            # asserting the other is right.
            Metadata={"sha256": file_hash},
        )
        return key, file_hash

    def read(self, key: str) -> bytes:
        from botocore.exceptions import ClientError

        try:
            return self._client.get_object(
                Bucket=self._bucket, Key=key
            )["Body"].read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise FileNotFoundError(key) from exc
            raise

    def delete(self, key: str) -> None:
        from botocore.exceptions import ClientError

        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except ClientError:
            # Cleanup after a failed upload. Never the reason a request fails.
            logger.warning("Could not delete stored object", extra={"key": key})

    @contextmanager
    def local_path(self, key: str) -> Iterator[str]:
        """Materialise the object so a path-taking caller (OCR) can read it.

        `delete=False` plus an explicit remove, because Windows will not let a
        second process open a NamedTemporaryFile that is still open here — and
        the callers are libraries that open the path themselves.
        """
        suffix = os.path.splitext(key)[1]
        handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        try:
            handle.write(self.read(key))
            handle.close()
            yield handle.name
        finally:
            handle.close()
            try:
                os.remove(handle.name)
            except OSError:
                logger.warning(
                    "Could not remove temporary file", extra={"path": handle.name}
                )
