import base64
from datetime import datetime,timezone
from email.utils import parseaddr,parsedate_to_datetime
def decode(value:str)->str: return base64.urlsafe_b64decode(value+"="*(-len(value)%4)).decode("utf-8",errors="replace")
def parse_message(message:dict)->dict:
 headers={h["name"].lower():h["value"] for h in message.get("payload",{}).get("headers",[])};name,address=parseaddr(headers.get("from",""));recipients=[]
 for key in ("to","cc","bcc"):
  if headers.get(key): recipients.extend([x.strip() for x in headers[key].split(",")])
 text=[];attachments=[]
 def walk(part):
  mime=part.get("mimeType","");body=part.get("body",{});filename=part.get("filename")
  if filename and body.get("attachmentId"): attachments.append({"filename":filename,"mime_type":mime,"attachment_id":body["attachmentId"],"size":body.get("size",0)})
  if mime=="text/plain" and body.get("data"): text.append(decode(body["data"]))
  for child in part.get("parts",[]): walk(child)
 walk(message.get("payload",{}));date=headers.get("date")
 try: received=parsedate_to_datetime(date).astimezone(timezone.utc) if date else datetime.fromtimestamp(int(message.get("internalDate","0"))/1000,timezone.utc)
 except Exception: received=datetime.now(timezone.utc)
 return {"message_id":message["id"],"thread_id":message["threadId"],"sender_name":name or None,"sender_email":address,"recipients":recipients,"subject":headers.get("subject"),"body_text":"\n".join(text),"received_at":received,"labels":message.get("labelIds",[]),"has_attachments":bool(attachments),"attachments":attachments}
