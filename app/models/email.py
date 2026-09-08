from datetime import datetime
from sqlalchemy import Boolean,DateTime,ForeignKey,Integer,JSON,String,Text,UniqueConstraint,func
from sqlalchemy.orm import Mapped,mapped_column,relationship
from app.database import Base
class GmailAccount(Base):
 __tablename__="gmail_accounts"
 id:Mapped[int]=mapped_column(Integer,primary_key=True);google_email:Mapped[str]=mapped_column(String(320),unique=True,index=True);encrypted_token:Mapped[str]=mapped_column(Text);history_id:Mapped[str|None]=mapped_column(String(64));watch_expiration:Mapped[datetime|None]=mapped_column(DateTime(timezone=True));created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=func.now());updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=func.now(),onupdate=func.now())
 emails:Mapped[list["Email"]]=relationship(back_populates="account")
class Email(Base):
 __tablename__="emails";__table_args__=(UniqueConstraint("account_id","message_id",name="uq_account_message"),)
 id:Mapped[int]=mapped_column(Integer,primary_key=True);account_id:Mapped[int]=mapped_column(ForeignKey("gmail_accounts.id"),index=True);message_id:Mapped[str]=mapped_column(String(255));thread_id:Mapped[str]=mapped_column(String(255),index=True);sender_name:Mapped[str|None]=mapped_column(String(500));sender_email:Mapped[str]=mapped_column(String(320),index=True);recipients:Mapped[list]=mapped_column(JSON);subject:Mapped[str|None]=mapped_column(Text);body_text:Mapped[str|None]=mapped_column(Text);received_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),index=True);labels:Mapped[list]=mapped_column(JSON);has_attachments:Mapped[bool]=mapped_column(Boolean,default=False);attachments:Mapped[list]=mapped_column(JSON);created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=func.now());updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=func.now(),onupdate=func.now())
 account:Mapped[GmailAccount]=relationship(back_populates="emails")
