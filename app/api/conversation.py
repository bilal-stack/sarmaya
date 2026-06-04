from fastapi import APIRouter, Depends, HTTPException, status, Body
from sqlalchemy.orm import Session
from typing import List, Dict, Any
from uuid import UUID
import logging

from app.api.deps import get_current_user, get_db_session
from app.core.roles import has_permission, PERM_VIEW_INVOICE
from app.services.ai import get_ai_provider
from app.services.conversation_service import ConversationService
from app.schemas.conversation import (
    ChatRequest, 
    ChatResponse, 
    ConversationOut, 
    ConversationDetail,
    PaginatedConversationsOut
)
from app.models.invoice import Invoice
from app.models.vendor import Vendor
from datetime import date
from app.agents.duplicate_agent import DuplicateDetectionAgent
from app.agents.query_agent import QueryAgent

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/conversation", tags=["Conversation"])


# ============================================
# CONVERSATIONS
# ============================================

@router.get("/list", response_model=PaginatedConversationsOut)
def list_conversations(
    limit: int = 50,
    offset: int = 0,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    
    """List all conversations for current user"""
    service = ConversationService(db)
    conversations, total = service.list_user_conversations(
        user_id=current_user["id"],
        limit=limit,
        offset=offset
    )
    
    # Add message count (single grouped query, not a lazy load per conversation)
    counts = service.message_counts([conv.id for conv in conversations])
    result = []
    for conv in conversations:
        result.append({
            "id": conv.id,
            "title": conv.title,
            "created_at": conv.created_at,
            "updated_at": conv.updated_at,
            "message_count": counts.get(conv.id, 0)
        })
    
    return {
        "conversations": result,
        "total": total,
        "limit": limit,
        "offset": offset
    }


@router.get("/messages/{conversation_id}", response_model=ConversationDetail)
def get_conversation(
    conversation_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Get conversation with full message history"""
    service = ConversationService(db)
    conversation = service.get_conversation(conversation_id)
    
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    if str(conversation.user_id) != str(current_user["id"]):
        raise HTTPException(status_code=403, detail="Access denied")
    
    return conversation


@router.delete("/delete/{conversation_id}")
def delete_conversation(
    conversation_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Delete a conversation"""
    service = ConversationService(db)
    conversation = service.get_conversation(conversation_id)
    
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    if str(conversation.user_id) != str(current_user["id"]):
        raise HTTPException(status_code=403, detail="Access denied")
    
    service.delete_conversation(conversation_id)
    return {"message": "Conversation deleted"}


# ============================================
# CHAT WITH CONTEXT
# ============================================

@router.post("/chat", response_model=ChatResponse)
def chat(
    request: ChatRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """
    Chat with AI assistant with persistent conversation history
    
    - If conversation_id is provided, continue existing conversation
    - If not provided, create new conversation
    """
    try:
        ai = get_ai_provider()
        conv_service = ConversationService(db)
        
        # Get or create conversation
        if request.conversation_id:
            conversation = conv_service.get_conversation(request.conversation_id)
            if not conversation:
                raise HTTPException(status_code=404, detail="Conversation not found")
            if str(conversation.user_id) != str(current_user["id"]):
                raise HTTPException(status_code=403, detail="Access denied")
        else:
            # Create with temporary title
            conversation = conv_service.create_conversation(
                user_id=current_user["id"],
                tenant_id=current_user["tenant_id"],
                title="New Chat"
            )
        
        # Get conversation history
        history = conv_service.get_conversation_history(conversation.id)
        messages = [
            {"role": msg.role, "content": msg.content}
            for msg in history
        ]
        
        # Check if this is the first user message (to generate title)
        is_first_message = len([m for m in history if m.role == "user"]) == 0
        
        # Add current user message
        messages.append({"role": "user", "content": request.message})
        
        # Build context
        context = {
            "tenant_id": str(current_user["tenant_id"]),
            "role": current_user["role"],
            "user_email": current_user["email"]
        }
        
        # Get AI response
        ai_response = ai.chat(messages, context)
        
        # Save messages to database
        conv_service.add_message(
            conversation_id=conversation.id,
            role="user",
            content=request.message
        )
        conv_service.add_message(
            conversation_id=conversation.id,
            role="assistant",
            content=ai_response
        )
        
        # Generate title from first message
        if is_first_message:
            title = conv_service.generate_title_from_message(request.message, ai)
            conv_service.update_conversation_title(conversation.id, title)
        
        return ChatResponse(
            conversation_id=conversation.id,
            message=ai_response,
            role="assistant"
        )
    
    except HTTPException:
        raise
    except Exception:
        logger.exception("AI chat failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The assistant could not process your message. Please try again."
        )


# ============================================
# QUERY AGENT
# ============================================

@router.post("/query")
def query_invoices(
    query: str = Body(..., embed=True),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """
    Natural language query with SQL agent
    
    Examples:
    - "Show me pending invoices"
    - "Invoices below 25000"
    - "Top 5 vendors"
    """
    if not has_permission(current_user["role"], PERM_VIEW_INVOICE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to query invoices"
        )
    try:
        # Use Query Agent
        agent = QueryAgent(
            db=db,
            tenant_id=current_user["tenant_id"],
            user_context={
                "tenant_id": current_user["tenant_id"],
                "role": current_user["role"],
                "email": current_user["email"]
            }
        )

        result = agent.query(query)
        return result

    except Exception:
        logger.exception("Invoice query agent failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The query could not be processed. Please try again."
        )


@router.post("/detect-duplicate")
def detect_duplicate(
    invoice_data: Dict[str, Any] = Body(...),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """
    AI-powered duplicate detection with multi-strategy agent
    
    Body:
    {
        "vendor_name": "ABC Corp",
        "invoice_number": "INV-123",
        "invoice_date": "2024-01-15",
        "total_amount": 50000.0,
        "line_items": [...]
    }
    """
    if not has_permission(current_user["role"], PERM_VIEW_INVOICE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to run duplicate detection"
        )
    try:
        # Use Duplicate Detection Agent
        agent = DuplicateDetectionAgent(
            db=db,
            tenant_id=current_user["tenant_id"]
        )

        result = agent.detect(
            vendor_name=invoice_data.get("vendor_name"),
            invoice_number=invoice_data.get("invoice_number"),
            invoice_date=invoice_data.get("invoice_date"),
            total_amount=invoice_data.get("total_amount"),
            line_items=invoice_data.get("line_items")
        )

        return result

    except Exception:
        logger.exception("Duplicate detection failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Duplicate detection could not be completed. Please try again."
        )