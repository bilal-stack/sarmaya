from sqlalchemy.orm import Session
from typing import List, Optional
from uuid import UUID

from app.models.conversation import Conversation, ConversationMessage
from app.schemas.conversation import ConversationCreate, ConversationMessageCreate
from app.utils.datetime_helpers import utc_now


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
            title=title or f"Chat {utc_now().strftime('%Y-%m-%d %H:%M')}"
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
        conversation = self.get_conversation(conversation_id)
        if not conversation:
            raise ValueError("Conversation not found")

        message = ConversationMessage(
            tenant_id=conversation.tenant_id,
            conversation_id=conversation_id,
            role=role,
            content=content,
            meta_data=metadata
        )
        self.db.add(message)

        # Update conversation timestamp
        conversation.updated_at = utc_now()

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
    
    def update_conversation_title(self, conversation_id: UUID, title: str):
        """Update conversation title"""
        conversation = self.get_conversation(conversation_id)
        if conversation:
            conversation.title = title
            self.db.commit()
    
    def generate_title_from_message(self, message: str, ai_provider=None) -> str:
        """Generate a concise title from message content"""
        # If AI provider available, use it for smart title generation
        if ai_provider:
            try:
                prompt = f"Generate a short, concise title (max 6 words) for this conversation based on the user's message. Return ONLY the title, no quotes or extra text:\n\n{message}"
                title = ai_provider.chat(
                    messages=[{"role": "user", "content": prompt}],
                    context={"task": "title_generation"}
                )
                # Clean up the response (remove quotes, trim)
                title = title.strip().strip('"').strip("'")
                if len(title) > 60:
                    title = title[:57] + "..."
                return title
            except:
                pass
        
        # Fallback: Use first 6 words of message
        words = message.split()[:6]
        title = " ".join(words)
        if len(message.split()) > 6:
            title += "..."
        return title
