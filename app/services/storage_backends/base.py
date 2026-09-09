"""Where an uploaded file actually lives.

Uploads used to go straight to local disk. That is correct on a developer
machine and wrong almost everywhere else: hosted platforms give an ephemeral
filesystem, so an invoice PDF survives until the next restart or redeploy and
then does not. What makes that worse than an ordinary data-loss bug is what
depends on it — an evidence pack records the SHA-256 of every attachment it
references, so files disappearing turns a sealed audit document into a set of
hashes pointing at nothing. The pack still verifies its own seal and is still
useless.

So the destination is a choice, made once, behind this interface. Local disk
stays the default because `docker compose up` should need nothing configured;
object storage is what a deployment uses.

**The key, not the path.** `save` returns an opaque key, stored on the file
record along with the backend that produced it. Reads and deletes dispatch on
that recorded backend rather than on the current setting, so switching a
deployment to object storage does not orphan every file already written to
disk. That is the difference between a migration and a data loss.

**`local_path` exists because OCR needs a real file.** Every OCR provider takes
a path and opens it — Textract, Document AI and OCR.space all do. Rewriting
three providers to take bytes would be the larger change and would gain
nothing, so backends instead promise a path that is valid for the duration of
a `with` block. The local backend yields the file it already has and copies
nothing; the object-storage backend materialises a temporary file and removes
it afterwards.
"""
from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Iterator, Tuple


class StorageBackend(ABC):
    """One place uploaded files can live."""

    #: Recorded on the file row, so a later read knows who wrote it.
    name: str

    @abstractmethod
    def save(self, tenant_id: str, filename: str, content: bytes) -> Tuple[str, str]:
        """Store the bytes. Returns (key, sha256).

        The hash is computed over the bytes as given, before any backend
        touches them, so it means the same thing everywhere and an evidence
        pack's attachment hash does not depend on where the file was put.
        """

    @abstractmethod
    def read(self, key: str) -> bytes:
        """The bytes back. Raises FileNotFoundError if the key is gone."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove it. A key that is already absent is not an error — this is
        called to clean up after a failed upload, and a cleanup that raises
        because the thing was never written is noise."""

    @contextmanager
    @abstractmethod
    def local_path(self, key: str) -> Iterator[str]:
        """A filesystem path valid for the life of the block. See the module
        docstring for why this exists rather than an OCR rewrite."""
