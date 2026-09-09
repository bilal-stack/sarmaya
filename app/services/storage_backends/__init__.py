"""Choosing where uploads go.

Two resolvers, and the difference between them matters:

  * `get_storage_backend()` — where a NEW upload should be written. Reads the
    deployment's setting.
  * `backend_for(name)` — where an EXISTING file already is. Reads what was
    recorded on the file row when it was written.

Reads and deletes must use the second. A deployment that switches to object
storage still has rows pointing at local paths, and resolving those through
the current setting would look for them in a bucket they were never put in —
turning a configuration change into silent data loss. Same reasoning as the
`storage_type` column, which has been on the file model since before there
was a second backend to record.
"""
from typing import Dict

from app.core.config import settings
from app.services.storage_backends.base import StorageBackend
from app.services.storage_backends.local import LocalStorage

#: Instantiated lazily and cached: constructing the S3 client opens no
#: connection, but it does validate configuration, and a deployment running on
#: local disk should not need object-storage settings to exist.
_CACHE: Dict[str, StorageBackend] = {}


def backend_for(name: str) -> StorageBackend:
    """The backend that wrote a file, by its recorded name."""
    key = (name or "local").lower()
    if key not in _CACHE:
        if key == "local":
            _CACHE[key] = LocalStorage()
        elif key == "s3":
            from app.services.storage_backends.s3 import S3Storage
            _CACHE[key] = S3Storage()
        else:
            raise ValueError(
                f"{name!r} is not a storage backend this build knows. "
                "One of: local, s3."
            )
    return _CACHE[key]


def get_storage_backend() -> StorageBackend:
    """Where new uploads go."""
    return backend_for(settings.STORAGE_BACKEND)


def reset_cache() -> None:
    """Drop cached backends. For tests that change the setting between cases."""
    _CACHE.clear()
