from fastapi import FastAPI, HTTPException, Depends, Request, UploadFile, File, Form, status
from sqlalchemy.orm import Session
from auth import get_credentials, flow
from gmail_utils import fetch_and_store_emails, sync_history, _get_or_create_sync_state
from database import SessionLocal, engine
from models import Base, Email, SyncState
from schemas import EmailSchema
from typing import List
from send_gmail import send_email_with_gmail_api
from googleapiclient.discovery import build
import requests, os, json, base64

Base.metadata.create_all(bind=engine)
app = FastAPI()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------- AUTH ----------------

@app.get("/login", tags=["Authentication"])
def login_url():
    authorization_url, _ = flow.authorization_url(
        prompt="consent",
        access_type="offline",
        include_granted_scopes="true"
    )
    return {"auth_url": authorization_url}


@app.get("/oauth2callback", tags=["Authentication"])
def oauth2callback(request: Request):
    code = request.query_params.get("code")
    if not code:
        raise HTTPException(400, "Missing code in callback")
    flow.fetch_token(code=code)
    creds = flow.credentials
    with open("token.json", "w") as token:
        token.write(creds.to_json())
    return {"message": "Authentication successful! You can now call /fetch-emails."}


@app.post("/logout", tags=["Authentication"])
def logout():
    creds = get_credentials()
    if not creds or not creds.token:
        raise HTTPException(status_code=401, detail="No active session found")

    try:
        revoke_url = "https://oauth2.googleapis.com/revoke"
        response = requests.post(
            revoke_url,
            params={"token": creds.token},
            headers={"content-type": "application/x-www-form-urlencoded"}
        )

        if response.status_code == 200:
            if os.path.exists("token.json"):
                os.remove("token.json")
            return {"message": "✅ Successfully logged out and token revoked."}
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to revoke token"
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Logout failed: {str(e)}")


# ---------------- MAILS ----------------

@app.get("/emails", response_model=List[EmailSchema], tags=["Mails"])
def get_all_emails(db: Session = Depends(get_db)):
    return db.query(Email).all()


@app.get("/emails/{email_id}", response_model=EmailSchema, tags=["Mails"])
def get_email_by_id(email_id: int, db: Session = Depends(get_db)):
    email = db.query(Email).filter(Email.id == email_id).first()
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")
    return email


@app.post("/send-email", tags=["Mails"])
async def send_email_api(
    to_email: str = Form(...),
    subject: str = Form(...),
    body: str = Form(...),
    attachment: UploadFile = File(None),
    db: Session = Depends(get_db)
):
    creds = get_credentials()

    try:
        await send_email_with_gmail_api(to_email, subject, body, attachment, creds)
        return {"message": "✅ Email sent successfully using Gmail API."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send email: {str(e)}")


# ---------------- SYNC ----------------

@app.get("/fetch-emails", tags=["Sync mails"])
def fetch_emails_endpoint(db: Session = Depends(get_db)):
    creds = get_credentials()
    if not creds:
        raise HTTPException(401, "Authentication required. Use /login first.")
    try:
        count = fetch_and_store_emails(creds)
        return {"message": f"Fetched and stored {count} new emails."}
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch emails: {str(e)}")


@app.post("/gmail/watch", tags=["Sync mails"])
def start_watch(db: Session = Depends(get_db)):
    creds = get_credentials()
    if not creds:
        raise HTTPException(401, "Auth required")

    service = build("gmail", "v1", credentials=creds)
    request = {
        "topicName": "projects/mail-fetcher-470411/topics/gmail-notifications",
        "labelFilterAction": "include"
    }

    res = service.users().watch(userId="me", body=request).execute()

    # Persist returned historyId
    state = _get_or_create_sync_state(db, service)
    state.last_history_id = str(res.get("historyId", state.last_history_id))
    db.commit()
    return {"message": "Watch started", "historyId": state.last_history_id}


@app.post("/gmail/pubsub", tags=["Sync mails"])
async def gmail_pubsub(request: Request, db: Session = Depends(get_db)):
    """
    Pub/Sub push endpoint.
    Handles deduplication, skips stale notifications, and ensures history continuity.
    """
    creds = get_credentials()
    if not creds:
        print("PubSub ignored: no credentials")
        return {"status": "ignored: not authenticated"}

    body = await request.body()
    if not body:
        # Subscription validation ping
        print("📭 PubSub ping (empty body) – ignoring")
        return {"status": "ok"}

    try:
        envelope = json.loads(body.decode("utf-8"))
        msg = envelope.get("message", {})
        data_b64 = msg.get("data")

        if not data_b64:
            # Could be a test notification with no payload
            print("📭 PubSub message received (no data) – ignoring")
            return {"status": "ok"}

        decoded = json.loads(base64.b64decode(data_b64).decode("utf-8"))
        notif_history_id = str(decoded.get("historyId"))
        print(f"📨 Pub/Sub notification received. historyId={notif_history_id}")

        service = build("gmail", "v1", credentials=creds)
        state = _get_or_create_sync_state(db, service)

        # Skip stale/duplicate notifications
        if state.last_history_id and int(notif_history_id) <= int(state.last_history_id):
            print(f"⏭️ Skipping stale notification (notif={notif_history_id}, last={state.last_history_id})")
            return {"status": "ok"}

        try:
            new_last = sync_history(creds, state.last_history_id or notif_history_id)

            if new_last:
                state.last_history_id = str(new_last)
                db.commit()
                print(f"✅ Sync successful. Updated last_history_id={state.last_history_id}")
            else:
                print("⚠️ sync_history returned no new historyId")

        except Exception as e:
            from googleapiclient.errors import HttpError
            if isinstance(e, HttpError) and e.resp.status == 404:
                print("⚠️ HistoryId expired. Performing full refetch.")
                fetch_and_store_emails(creds)
                profile = service.users().getProfile(userId="me").execute()
                state.last_history_id = str(profile.get("historyId"))
                db.commit()
                print(f"🔄 Rebased last_history_id={state.last_history_id}")
            else:
                raise

    except Exception as e:
        print("❌ PubSub error:", str(e))

    return {"status": "ok"}
