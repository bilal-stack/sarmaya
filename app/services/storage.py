import os
import hashlib
from app.core.config import settings


def save_file(tenant_id: str, filename: str, content: bytes) -> tuple[str, str]:
    """
    Save file to local storage and return (path, hash)
    
    Returns:
        (stored_path, file_hash)
    """
    # Create tenant directory
    tenant_dir = os.path.join(settings.UPLOAD_DIR, tenant_id)
    os.makedirs(tenant_dir, exist_ok=True)
    
    # Generate unique filename
    file_hash = hashlib.sha256(content).hexdigest()
    stored_filename = f"{file_hash[:16]}_{filename}"
    stored_path = os.path.join(tenant_dir, stored_filename)
    
    # Write file
    with open(stored_path, "wb") as f:
        f.write(content)

    return stored_path, file_hash


def delete_file(stored_path: str) -> None:
    """Delete a stored file. No-op if it does not exist."""
    try:
        os.remove(stored_path)
    except FileNotFoundError:
        pass
