from sqlalchemy.orm import Session
from typing import List, Optional
from uuid import UUID
from datetime import datetime

from app.models.conversation import Conversation, ConversationMessage
from app.schemas.conversation import ConversationCreate, ConversationMessageCreate


class ConversationService:
    def __init__(self, db: Session):
        self.db = db
    
    def create_conversation(
        self, 
        user_id: UUID, 
        tenant_id: UUID, 
        title: Optional[str] = None
    ) -> Conversation:
        """Create new conversation"""
        conversation = Conversation(
            user_id=user_id,
            tenant_id=tenant_id,
            title=title or f"Chat {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"
        )
        self.db.add(conversation)
        self.db.commit()
        self.db.refresh(conversation)
        return conversation
    
    def get_conversation(self, conversation_id: UUID) -> Optional[Conversation]:
        """Get conversation by ID"""
        return self.db.query(Conversation).filter(
            Conversation.id == conversation_id
        ).first()
    
    def list_user_conversations(
        self, 
        user_id: UUID, 
        limit: int = 50, 
        offset: int = 0
    ) -> tuple[List[Conversation], int]:
        """List all conversations for a user"""
        query = self.db.query(Conversation).filter(
            Conversation.user_id == user_id
        ).order_by(Conversation.updated_at.desc())
        
        total = query.count()
        conversations = query.offset(offset).limit(limit).all()
        return conversations, total
    
    def add_message(
        self, 
        conversation_id: UUID, 
        role: str, 
        content: str,
        metadata: Optional[dict] = None
    ) -> ConversationMessage:
        """Add message to conversation"""
        message = ConversationMessage(
            conversation_id=conversation_id,
            role=role,
            content=content,
            metadata=metadata
        )
        self.db.add(message)
        
        # Update conversation timestamp
        conversation = self.get_conversation(conversation_id)
        if conversation:
            conversation.updated_at = datetime.utcnow()
        
        self.db.commit()
        self.db.refresh(message)
        return message
    
    def get_conversation_history(
        self, 
        conversation_id: UUID, 
        limit: int = 50
    ) -> List[ConversationMessage]:
        """Get message history for a conversation"""
        return self.db.query(ConversationMessage).filter(
            ConversationMessage.conversation_id == conversation_id
        ).order_by(ConversationMessage.created_at.asc()).limit(limit).all()
    
    def delete_conversation(self, conversation_id: UUID):
        """Delete conversation and all messages"""
        conversation = self.get_conversation(conversation_id)
        if conversation:
            self.db.delete(conversation)
            self.db.commit()
