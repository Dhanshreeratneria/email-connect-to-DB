from datetime import datetime,timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models.email import GmailAccount,Email
from app.services.gmail_service import get_message,list_messages,history,watch
from app.services.email_parser import parse_message
def upsert(db:Session,account:GmailAccount,raw:dict):
 data=parse_message(raw);existing=db.scalar(select(Email).where(Email.account_id==account.id,Email.message_id==data["message_id"]))
 if existing:
  for k,v in data.items():setattr(existing,k,v)
 else: db.add(Email(account_id=account.id,**data))
def initial_sync(db:Session,account:GmailAccount,service):
 token=None
 while True:
  page=list_messages(service,token)
  for item in page.get("messages",[]): upsert(db,account,get_message(service,item["id"]))
  token=page.get("nextPageToken")
  if not token: break
 account.history_id=str(service.users().getProfile(userId="me").execute()["historyId"]);db.commit()
def incremental_sync(db:Session,account:GmailAccount,service,notified_history_id:str|None=None):
 if not account.history_id: return initial_sync(db,account,service)
 try: response=history(service,account.history_id)
 except Exception: return initial_sync(db,account,service)
 seen=set()
 while True:
  for item in response.get("history",[]):
   for added in item.get("messagesAdded",[]):
    mid=added["message"]["id"]
    if mid not in seen: seen.add(mid);upsert(db,account,get_message(service,mid))
  token=response.get("nextPageToken")
  if not token:break
  response=history(service,account.history_id)
 account.history_id=str(notified_history_id or response.get("historyId") or account.history_id);db.commit()
def renew_watch(db:Session,account:GmailAccount,service,topic:str):
 response=watch(service,topic);account.history_id=str(response["historyId"]);account.watch_expiration=datetime.fromtimestamp(int(response["expiration"])/1000,timezone.utc);db.commit();return response
