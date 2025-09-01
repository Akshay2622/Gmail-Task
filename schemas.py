from typing import List, Optional
from datetime import datetime
from pydantic import BaseModel

class AttachmentSchema(BaseModel):
    id: int
    filename: str
    filepath: Optional[str] = None
    class Config: orm_mode = True

class EmailSchema(BaseModel):
    id: int
    subject: str
    from_email: str
    to_email: str
    date: datetime
    message_id: str
    body: str
    is_starred: int  
    attachments: List[AttachmentSchema] = []
    class Config: orm_mode = True
