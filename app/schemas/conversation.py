from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List
from uuid import UUID


class ConversationMessageBase(BaseModel):
    role: str = Field(..., description="Message role: user, assistant, system")
    content: str = Field(..., description="Message content")


class ConversationMessageCreate(ConversationMessageBase):
    metadata: Optional[dict] = None


class ConversationMessageOut(ConversationMessageBase):
    id: UUID
    conversation_id: UUID
    created_at: datetime
    
    class Config:
        from_attributes = True


class ConversationCreate(BaseModel):
    title: Optional[str] = None


class ConversationOut(BaseModel):
    id: UUID
    title: Optional[str]
    created_at: datetime
    updated_at: Optional[datetime]
    message_count: int = 0
    total: Optional[int] = None  # For pagination metadata
    limit: Optional[int] = None
    offset: Optional[int] = None

    class Config:
        from_attributes = True


class ConversationDetail(ConversationOut):
    messages: List[ConversationMessageOut] = []


class ChatRequest(BaseModel):
    message: str = Field(..., description="User message")
    conversation_id: Optional[UUID] = Field(None, description="Existing conversation ID (optional)")


class ChatResponse(BaseModel):
    conversation_id: UUID
    message: str
    role: str = "assistant"

# Schema for paginated list response
class PaginatedConversationsOut(BaseModel):
    conversations: List[ConversationOut]
    total: int
    limit: int
    offset: int
