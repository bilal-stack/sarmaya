"""Local disk. The default, and the right answer on a developer machine.

Behaviour is unchanged from the original `storage.py`, deliberately: files
written before this abstraction existed are still readable through it, because
the key is the same path string those rows already hold.
"""
import hashlib
import os
from contextlib import contextmanager
from typing import Iterator, Tuple

from app.core.config import settings
from app.services.storage_backends.base import StorageBackend


class LocalStorage(StorageBackend):
    name = "local"

    def save(self, tenant_id: str, filename: str, content: bytes) -> Tuple[str, str]:
        tenant_dir = os.path.join(settings.UPLOAD_DIR, tenant_id)
        os.makedirs(tenant_dir, exist_ok=True)

        file_hash = hashlib.sha256(content).hexdigest()
        # Hash-prefixed so two uploads of the same name do not collide, and so
        # the stored name is traceable back to the content it claims to be.
        stored_filename = f"{file_hash[:16]}_{filename}"
        stored_path = os.path.join(tenant_dir, stored_filename)

        with open(stored_path, "wb") as f:
            f.write(content)

        return stored_path, file_hash

    def read(self, key: str) -> bytes:
        with open(key, "rb") as f:
            return f.read()

    def delete(self, key: str) -> None:
        try:
            os.remove(key)
        except FileNotFoundError:
            pass

    @contextmanager
    def local_path(self, key: str) -> Iterator[str]:
        # Already local. No copy, no temporary file, nothing to clean up.
        yield key
