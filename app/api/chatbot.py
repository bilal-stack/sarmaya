from fastapi import APIRouter

router = APIRouter(prefix="/chatbot", tags=["chatbot"])

@router.post("/query")
async def query_bot(prompt: str):
    return {"reply": "This is a placeholder reply."}
