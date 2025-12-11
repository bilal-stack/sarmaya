from fastapi import APIRouter, Depends, HTTPException, status, Body
from sqlalchemy.orm import Session
from typing import List, Dict, Any, Optional
from uuid import UUID

from app.api.deps import get_current_user, get_db_session
from app.services.ai import get_ai_provider
from app.services.conversation_service import ConversationService
from app.schemas.conversation import (
    ChatRequest, 
    ChatResponse, 
    ConversationOut, 
    ConversationDetail
)
from app.models.invoice import Invoice
from app.models.vendor import Vendor
from datetime import date

router = APIRouter(prefix="/ai", tags=["AI"])


# ============================================
# CONVERSATIONS
# ============================================

@router.get("/conversations", response_model=List[ConversationOut])
def list_conversations(
    limit: int = 50,
    offset: int = 0,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """List all conversations for current user"""
    service = ConversationService(db)
    conversations, total = service.list_user_conversations(
        user_id=current_user["sub"],
        limit=limit,
        offset=offset
    )
    
    # Add message count
    result = []
    for conv in conversations:
        result.append({
            "id": conv.id,
            "title": conv.title,
            "created_at": conv.created_at,
            "updated_at": conv.updated_at,
            "message_count": len(conv.messages)
        })
    
    return result


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
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
    
    if str(conversation.user_id) != str(current_user["sub"]):
        raise HTTPException(status_code=403, detail="Access denied")
    
    return conversation


@router.delete("/conversations/{conversation_id}")
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
    
    if str(conversation.user_id) != str(current_user["sub"]):
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
            if str(conversation.user_id) != str(current_user["sub"]):
                raise HTTPException(status_code=403, detail="Access denied")
        else:
            conversation = conv_service.create_conversation(
                user_id=current_user["sub"],
                tenant_id=current_user["tenant_id"]
            )
        
        # Get conversation history
        history = conv_service.get_conversation_history(conversation.id)
        messages = [
            {"role": msg.role, "content": msg.content}
            for msg in history
        ]
        
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
        
        return ChatResponse(
            conversation_id=conversation.id,
            message=ai_response,
            role="assistant"
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"AI chat failed: {str(e)}"
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
    Natural language query for invoices/vendors
    
    Examples:
    - "Show me all pending invoices"
    - "Which invoices are over 100k?"
    - "List vendors with status active"
    """
    try:
        ai = get_ai_provider()
        
        # Gather data summary for context
        pending_count = db.query(Invoice).filter(Invoice.current_state == "pending_approval").count()
        total_invoices = db.query(Invoice).count()
        vendor_count = db.query(Vendor).count()
        
        context = {
            "tenant_id": str(current_user["tenant_id"]),
            "role": current_user["role"],
            "data_summary": f"{total_invoices} invoices, {pending_count} pending, {vendor_count} vendors"
        }
        
        # Get AI interpretation
        ai_response = ai.query_system(query, context)
        
        # Simple query execution (expand later with SQL generation)
        data = None
        if "pending" in query.lower():
            invoices = db.query(Invoice).filter(
                Invoice.current_state == "pending_approval"
            ).limit(10).all()
            data = [
                {
                    "id": str(inv.id),
                    "invoice_number": inv.invoice_number,
                    "vendor_name": inv.vendor_name,
                    "total_amount": float(inv.total_amount or 0),
                    "invoice_date": str(inv.invoice_date)
                }
                for inv in invoices
            ]
        
        return {
            "query": query,
            "ai_response": ai_response,
            "data": data
        }
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Query failed: {str(e)}"
        )


# ============================================
# DUPLICATE DETECTION
# ============================================

@router.post("/detect-duplicate")
def detect_duplicate(
    invoice_data: Dict[str, Any] = Body(...),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """
    AI-powered duplicate detection
    
    Body:
    {
        "vendor_name": "ABC Corp",
        "invoice_number": "INV-123",
        "invoice_date": "2024-01-15",
        "total_amount": 50000.0
    }
    """
    try:
        ai = get_ai_provider()
        
        # Find similar invoices (same vendor, close date/amount)
        from datetime import timedelta
        inv_date = date.fromisoformat(invoice_data.get("invoice_date", str(date.today())))
        amount = float(invoice_data.get("total_amount", 0))
        
        similar = db.query(Invoice).filter(
            Invoice.vendor_name == invoice_data.get("vendor_name"),
            Invoice.invoice_date.between(
                inv_date - timedelta(days=30),
                inv_date + timedelta(days=30)
            ),
            Invoice.total_amount.between(amount * 0.9, amount * 1.1)
        ).limit(5).all()
        
        candidates = [
            {
                "id": str(inv.id),
                "invoice_number": inv.invoice_number,
                "invoice_date": str(inv.invoice_date),
                "total_amount": float(inv.total_amount or 0)
            }
            for inv in similar
        ]
        
        # AI analysis
        result = ai.detect_duplicate_invoices(invoice_data, candidates)
        
        return result
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Duplicate detection failed: {str(e)}"
        )
