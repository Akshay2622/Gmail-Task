from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from models import Email, SyncState
from database import SessionLocal
import os, re, base64, json
from datetime import datetime
from email.utils import parsedate_to_datetime

SAVE_ATTACHMENTS_FOLDER = "attachments"
os.makedirs(SAVE_ATTACHMENTS_FOLDER, exist_ok=True)


def clean(text):
    return re.sub(r"[^\w\s.-]", "", text).strip().replace(" ", "_")


def _parse_date(date_str: str):
    try:
        return parsedate_to_datetime(date_str)
    except Exception:
        try:
            return datetime.strptime(date_str[:31], "%a, %d %b %Y %H:%M:%S")
        except Exception:
            return datetime.utcnow()


def _get_or_create_sync_state(db, service):
    profile = service.users().getProfile(userId="me").execute()
    user_email = profile["emailAddress"]

    state = db.query(SyncState).filter_by(user_email=user_email).first()
    if not state:
        state = SyncState(user_email=user_email, last_history_id=None)
        db.add(state)
        db.commit()
        db.refresh(state)
    return state


def fetch_and_store_emails(creds):
    service = build("gmail", "v1", credentials=creds)
    db = SessionLocal()

    try:
        total_new = 0
        page_token = None
        while True:
            result = service.users().messages().list(
                userId="me",
                maxResults=100,
                pageToken=page_token,
                labelIds=["INBOX"],
                q="category:primary",
            ).execute()

            messages = result.get("messages", [])
            page_token = result.get("nextPageToken")

            for msg in messages:
                msg_data = service.users().messages().get(
                    userId="me",
                    id=msg["id"],
                    format="metadata",
                    metadataHeaders=["Subject", "From", "To", "Date"],
                ).execute()

                headers = msg_data.get("payload", {}).get("headers", [])
                subject = next((h["value"] for h in headers if h["name"] == "Subject"), "") or ""
                sender = next((h["value"] for h in headers if h["name"] == "From"), "") or ""
                to_email = next((h["value"] for h in headers if h["name"] == "To"), "") or ""
                date_str = next((h["value"] for h in headers if h["name"] == "Date"), "") or ""
                snippet = msg_data.get("snippet", "") or ""
                parsed_date = _parse_date(date_str) if date_str else datetime.utcnow()
                message_id = msg_data.get("id")
                label_ids = set(msg_data.get("labelIds", []))

                if "TRASH" in label_ids or "SPAM" in label_ids:
                    continue

                is_starred = 1 if "STARRED" in label_ids else 0

                if db.query(Email).filter(Email.message_id == message_id).first():
                    continue

                db_email = Email(
                    subject=subject,
                    from_email=sender,
                    to_email=to_email,
                    date=parsed_date,
                    message_id=message_id,
                    body=snippet,
                    is_starred=is_starred,
                )
                db.add(db_email)
                total_new += 1

            db.commit()

            if not page_token:
                break

        # Update last_history_id after full fetch
        state = _get_or_create_sync_state(db, service)
        profile = service.users().getProfile(userId="me").execute()
        state.last_history_id = profile.get("historyId", state.last_history_id)
        db.commit()

        return total_new

    finally:
        db.close()


def sync_history(creds, start_history_id: str):
    """
    Incremental Gmail sync with history.
    Returns the latest historyId or triggers full resync if expired.
    """
    service = build("gmail", "v1", credentials=creds)
    db = SessionLocal()

    latest_history_id = start_history_id
    try:
        page_token = None
        while True:
            history = service.users().history().list(
                userId="me",
                startHistoryId=start_history_id,
                historyTypes=["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"],
                pageToken=page_token,
            ).execute()

            if "history" not in history:
                print("⚠️ No history details returned. Performing full fetch.")
                fetch_and_store_emails(creds)
                profile = service.users().getProfile(userId="me").execute()
                return str(profile.get("historyId"))

            if "historyId" in history:
                latest_history_id = str(history["historyId"])

                # persist progressively
                state = _get_or_create_sync_state(db, service)
                state.last_history_id = latest_history_id
                db.commit()

            for change in history.get("history", []):
                # Deleted messages
                for item in change.get("messagesDeleted", []):
                    msg_id = item["message"]["id"]
                    db.query(Email).filter(Email.message_id == msg_id).delete()

                # Labels added
                for item in change.get("labelsAdded", []):
                    msg_id = item["message"]["id"]
                    labels = set(item.get("labelIds", []))

                    if "STARRED" in labels:
                        email = db.query(Email).filter(Email.message_id == msg_id).first()
                        if email:
                            email.is_starred = 1

                    if "INBOX" in labels:
                        if not db.query(Email).filter(Email.message_id == msg_id).first():
                            msg_data = service.users().messages().get(
                                userId="me",
                                id=msg_id,
                                format="metadata",
                                metadataHeaders=["Subject", "From", "To", "Date"],
                            ).execute()

                            headers = msg_data.get("payload", {}).get("headers", [])
                            subject = next((h["value"] for h in headers if h["name"] == "Subject"), "") or ""
                            sender = next((h["value"] for h in headers if h["name"] == "From"), "") or ""
                            to_email = next((h["value"] for h in headers if h["name"] == "To"), "") or ""
                            date_str = next((h["value"] for h in headers if h["name"] == "Date"), "") or ""
                            snippet = msg_data.get("snippet", "") or ""
                            parsed_date = _parse_date(date_str) if date_str else datetime.utcnow()
                            label_ids = set(msg_data.get("labelIds", []))
                            is_starred = 1 if "STARRED" in label_ids else 0

                            db_email = Email(
                                subject=subject,
                                from_email=sender,
                                to_email=to_email,
                                date=parsed_date,
                                message_id=msg_id,
                                body=snippet,
                                is_starred=is_starred,
                            )
                            db.add(db_email)
                            print(f"📩 Restored to INBOX: {subject} from {sender}")

                # Labels removed
                for item in change.get("labelsRemoved", []):
                    msg_id = item["message"]["id"]
                    labels = set(item.get("labelIds", []))

                    if "STARRED" in labels:
                        email = db.query(Email).filter(Email.message_id == msg_id).first()
                        if email:
                            email.is_starred = 0

                    if "INBOX" in labels:
                        db.query(Email).filter(Email.message_id == msg_id).delete()

                # New messages
                for item in change.get("messagesAdded", []):
                    msg_id = item["message"]["id"]

                    if db.query(Email).filter(Email.message_id == msg_id).first():
                        continue

                    msg_data = service.users().messages().get(
                        userId="me",
                        id=msg_id,
                        format="metadata",
                        metadataHeaders=["Subject", "From", "To", "Date"],
                    ).execute()

                    headers = msg_data.get("payload", {}).get("headers", [])
                    subject = next((h["value"] for h in headers if h["name"] == "Subject"), "") or ""
                    sender = next((h["value"] for h in headers if h["name"] == "From"), "") or ""
                    to_email = next((h["value"] for h in headers if h["name"] == "To"), "") or ""
                    date_str = next((h["value"] for h in headers if h["name"] == "Date"), "") or ""
                    snippet = msg_data.get("snippet", "") or ""
                    parsed_date = _parse_date(date_str) if date_str else datetime.utcnow()
                    label_ids = set(msg_data.get("labelIds", []))

                    if "TRASH" in label_ids or "SPAM" in label_ids:
                        continue

                    is_starred = 1 if "STARRED" in label_ids else 0

                    db_email = Email(
                        subject=subject,
                        from_email=sender,
                        to_email=to_email,
                        date=parsed_date,
                        message_id=msg_id,
                        body=snippet,
                        is_starred=is_starred,
                    )
                    db.add(db_email)
                    print(f"📥 New inbox message: {subject} from {sender}")

            db.commit()

            page_token = history.get("nextPageToken")
            if not page_token:
                break

        return latest_history_id

    except HttpError as e:
        if e.resp.status == 404:
            print("⚠️ History expired. Doing full resync.")
            fetch_and_store_emails(creds)
            profile = service.users().getProfile(userId="me").execute()
            return str(profile.get("historyId"))
        else:
            raise
    finally:
        db.close()
