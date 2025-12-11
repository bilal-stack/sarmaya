from sqlalchemy.orm import Session
from uuid import UUID
from typing import Tuple

from app.models.file import File as FileModel
from app.services.storage import save_file as storage_save_file


class FileService:
    """Service layer for file operations"""
    
    def __init__(self, db: Session):
        self.db = db
    
    def save_file(
        self,
        tenant_id: UUID,
        filename: str,
        content: bytes,
        mime_type: str,
        object_type: str,
        uploaded_by: UUID
    ) -> Tuple[FileModel, str, str]:
        """
        Save file to storage and create DB record
        
        Returns:
            (file_record, stored_path, file_hash)
        """
        # Save to storage
        stored_path, file_hash = storage_save_file(
            tenant_id=str(tenant_id),
            filename=filename,
            content=content
        )
        
        # Create DB record
        file_record = FileModel(
            tenant_id=tenant_id,
            original_filename=filename,
            stored_filename=stored_path.split("/")[-1],
            file_path=stored_path,
            mime_type=mime_type,
            file_size=len(content),
            file_hash=file_hash,
            object_type=object_type,
            storage_type="local",
            uploaded_by=uploaded_by
        )
        
        self.db.add(file_record)
        self.db.flush()
        
        return file_record, stored_path, file_hash
    
    def link_file_to_object(self, file_record: FileModel, object_id: UUID) -> None:
        """Link file to an object (e.g., invoice)"""
        file_record.object_id = object_id
        self.db.add(file_record)
        self.db.flush()
    
    def commit(self) -> None:
        """Commit transaction"""
        self.db.commit()
