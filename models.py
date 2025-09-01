from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, LargeBinary, UniqueConstraint
from sqlalchemy.orm import relationship
from database import Base

class Email(Base):
    __tablename__ = "emails"
    id = Column(Integer, primary_key=True, index=True)
    subject = Column(String)
    from_email = Column(String)
    to_email = Column(String)
    date = Column(DateTime)
    message_id = Column(String, unique=True, index=True)
    body = Column(Text)
    is_starred = Column(Integer, default=0)
    attachments = relationship("Attachment", back_populates="email")

class Attachment(Base):
    __tablename__ = "attachments"
    id = Column(Integer, primary_key=True, index=True)
    email_id = Column(Integer, ForeignKey("emails.id"))
    filename = Column(String)
    content = Column(LargeBinary)
    filepath = Column(String)
    email = relationship("Email", back_populates="attachments")

class SyncState(Base):
    """
    Stores the last_history_id per Gmail account (email address).
    If you only support one account locally, this will have exactly one row.
    """
    __tablename__ = "sync_state"
    id = Column(Integer, primary_key=True, index=True)
    user_email = Column(String, unique=True, index=True)
    last_history_id = Column(String)
