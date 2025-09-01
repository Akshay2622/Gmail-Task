# send_gmail.py

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication 
import base64
from fastapi import UploadFile

# Make the function asynchronous
async def send_email_with_gmail_api(to_email: str, subject: str, body: str, attachment: UploadFile = None, creds = None):
    """Send an email using Gmail API"""
    try:
        service = build('gmail', 'v1', credentials=creds)
        
        user_info = service.users().getProfile(userId="me").execute()
        from_email = user_info['emailAddress']
        
        message = MIMEMultipart()
        message['to'] = to_email
        message['from'] = from_email
        message['subject'] = subject
        
        message.attach(MIMEText(body, 'plain'))

        # Attach the file if provided
        if attachment:
            # ✅ Use await to read the file asynchronously
            attachment_data = await attachment.read()
            
            # ✅ Use MIMEApplication for generic file attachments
            attach_part = MIMEApplication(attachment_data, Name=attachment.filename)
            
            # This header tells the email client to treat it as an attachment
            attach_part['Content-Disposition'] = f'attachment; filename="{attachment.filename}"'
            message.attach(attach_part)
        
            

        raw_message = {'raw': base64.urlsafe_b64encode(message.as_bytes()).decode()}
        sent_message = service.users().messages().send(userId="me", body=raw_message).execute()
        
        print(f'Email sent: {sent_message["id"]}')

    except HttpError as error:
        print(f'An error occurred: {error}')
        # Re-raise the exception so the FastAPI endpoint can catch it
        raise error 