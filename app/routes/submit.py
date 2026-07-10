from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import r2
from app.config import get_settings
from app.web import current_user

router = APIRouter()


class PresignRequest(BaseModel):
    filename: str
    content_type: str
    size_bytes: int


@router.post("/api/presign")
def presign(req: PresignRequest, user=Depends(current_user)):
    if user is None:
        raise HTTPException(status_code=401, detail="Log in with Reddit first")
    s = get_settings()
    if req.content_type in r2.ALLOWED_IMAGE:
        kind, cap = "image", s.max_image_bytes
    elif req.content_type in r2.ALLOWED_VIDEO:
        kind, cap = "video", s.max_video_bytes
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {req.content_type}")
    if not 0 < req.size_bytes <= cap:
        raise HTTPException(
            status_code=400,
            detail=f"File too large — max {cap // (1024 * 1024)}MB for {kind}s",
        )
    key = r2.make_upload_key(req.content_type)
    return {
        "key": key,
        "upload_url": r2.presign_put(key, req.content_type, req.size_bytes),
        "public_url": r2.public_url(key),
        "kind": kind,
    }
