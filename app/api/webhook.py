from fastapi import APIRouter,Request,HTTPException,Depends
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.email import GmailAccount
from app.services.pubsub_service import decode_pubsub
from app.services.oauth_service import decrypt_credentials
from app.services.gmail_service import gmail
from app.services.sync_service import incremental_sync
router=APIRouter(prefix="/webhooks/google")
@router.post("/pubsub")
async def pubsub(request:Request,db:Session=Depends(get_db)):
 payload=await request.json()
 try:data=decode_pubsub(payload)
 except ValueError as e: raise HTTPException(400,str(e))
 address=data.get("emailAddress");history_id=str(data.get("historyId",""));account=db.query(GmailAccount).filter_by(google_email=address).first()
 if not account:return {"status":"ignored","reason":"unknown account"}
 incremental_sync(db,account,gmail(decrypt_credentials(account.encrypted_token)),history_id)
 return {"status":"synced"}
