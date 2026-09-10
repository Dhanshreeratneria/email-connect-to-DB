from fastapi import APIRouter, Request
from app.services.gmail_service import gmail
from app.database import get_db
from sqlalchemy.orm import Session
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["webhook"])

@router.post("/gmail")
async def gmail_webhook(request: Request, db: Session = Depends(get_db)):
    """Receives Gmail Pub/Sub notifications"""
    try:
        body = await request.json()
        
        # Pub/Sub sends message in this format
        message = body.get("message", {})
        data = message.get("data", "")
        
        logger.info(f"Gmail webhook received: {data}")
        
        # Trigger email sync here
        # You can sync the latest emails
        
        return {"status": "received"}
    except Exception as e:
        logger.exception("Webhook error")
        return {"error": str(e)}, 400