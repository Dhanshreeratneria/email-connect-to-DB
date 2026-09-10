from fastapi import APIRouter, Request, Depends  # ← Add Depends!
from sqlalchemy.orm import Session
from app.database import get_db
import logging
import base64
import json

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["webhook"])

@router.post("/gmail")
async def gmail_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Receives Pub/Sub push notifications from Gmail Watch.
    Google sends: {"message": {"data": "base64-encoded-string"}}
    """
    try:
        body = await request.json()
        logger.info(f"Webhook received: {body}")
        
        # Pub/Sub sends the message in this format
        if "message" in body:
            message = body["message"]
            
            # Data is base64 encoded
            if "data" in message:
                data_str = base64.b64decode(message["data"]).decode()
                data = json.loads(data_str)
                logger.info(f"Gmail notification: {data}")
                
                # Trigger email sync here
                # You can resync the mailbox
        
        # IMPORTANT: Return 200 OK so Pub/Sub knows message was received
        return {"status": "ok"}
        
    except Exception as e:
        logger.exception("Webhook error")
        return {"error": str(e)}, 400