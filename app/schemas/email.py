from datetime import datetime
from pydantic import BaseModel
class EmailOut(BaseModel):
 message_id:str;thread_id:str;sender_name:str|None;sender_email:str;recipients:list;subject:str|None;body_text:str|None;received_at:datetime;labels:list;has_attachments:bool;attachments:list
 class Config: from_attributes=True
