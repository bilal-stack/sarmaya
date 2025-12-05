from fastapi import APIRouter, UploadFile, File

router = APIRouter(prefix="/files", tags=["files"])

@router.post("/upload")
async def upload(file: UploadFile = File(...)):
    content = await file.read()
    return {"filename": file.filename, "size": len(content)}
