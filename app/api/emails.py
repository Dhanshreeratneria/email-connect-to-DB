from datetime import datetime
from fastapi import APIRouter,Depends,HTTPException,Query
from sqlalchemy import or_,select
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.email import Email
from app.schemas.email import EmailOut
router=APIRouter()
@router.get("/emails",response_model=list[EmailOut])
def emails(q:str|None=None,sender:str|None=None,subject:str|None=None,limit:int=Query(50,le=200),db:Session=Depends(get_db)):
 stmt=select(Email).order_by(Email.received_at.desc()).limit(limit)
 if q:stmt=stmt.where(or_(Email.subject.ilike(f"%{q}%"),Email.body_text.ilike(f"%{q}%"),Email.sender_email.ilike(f"%{q}%")))
 if sender:stmt=stmt.where(Email.sender_email.ilike(f"%{sender}%"))
 if subject:stmt=stmt.where(Email.subject.ilike(f"%{subject}%"))
 return db.scalars(stmt).all()
@router.get("/emails/{message_id}",response_model=EmailOut)
def email(message_id:str,db:Session=Depends(get_db)):
 value=db.scalar(select(Email).where(Email.message_id==message_id))
 if not value:raise HTTPException(404,"Email not found")
 return value
