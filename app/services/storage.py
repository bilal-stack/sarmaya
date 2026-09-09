"""Saving and removing uploaded files.

This module kept its original two-function shape after uploads moved behind a
backend abstraction, so every existing caller is unchanged. What is different
is that the destination is now configurable — see
`app/services/storage_backends/` for why that matters and how an existing
local file stays readable after a deployment switches to object storage.
"""
from typing import Tuple

from app.services.storage_backends import backend_for, get_storage_backend


def save_file(tenant_id: str, filename: str, content: bytes) -> Tuple[str, str, str]:
    """Store the bytes.

    Returns (key, sha256, backend_name). The backend name is returned rather
    than assumed, because it is what a later read has to dispatch on — the
    caller records it on the file row.
    """
    backend = get_storage_backend()
    key, file_hash = backend.save(tenant_id, filename, content)
    return key, file_hash, backend.name


def read_file(key: str, storage_type: str = "local") -> bytes:
    """The bytes back, from wherever that file was actually written."""
    return backend_for(storage_type).read(key)


def delete_file(key: str, storage_type: str = "local") -> None:
    """Remove a stored file. A key that is already gone is not an error."""
    backend_for(storage_type).delete(key)


def local_path(key: str, storage_type: str = "local"):
    """A real filesystem path for `key`, valid inside the `with` block.

    For callers that must hand a path to something else — every OCR provider
    takes one and opens it. Local storage yields the file it already has;
    object storage downloads to a temporary file and removes it afterwards.
    """
    return backend_for(storage_type).local_path(key)
