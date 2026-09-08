from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.email import GmailAccount
from app.services.gmail_service import gmail, profile
from app.services.oauth_service import (
    authorization_url,
    encrypt_credentials,
    exchange,
)
from app.services.sync_service import initial_sync, renew_watch


router = APIRouter(prefix="/auth/google")


@router.get("")
def begin():
    url, _ = authorization_url()
    return RedirectResponse(url)


@router.get("/callback")
def callback(code: str, db: Session = Depends(get_db)):
    credentials = exchange(code)
    service = gmail(credentials)
    email = profile(service)["emailAddress"]

    account = (
        db.query(GmailAccount)
        .filter_by(google_email=email)
        .first()
    )

    if not account:
        account = GmailAccount(
            google_email=email,
            encrypted_token=encrypt_credentials(credentials),
        )
        db.add(account)
        db.commit()
        db.refresh(account)
    else:
        account.encrypted_token = encrypt_credentials(credentials)
        db.commit()

    initial_sync(db, account, service)

    # Temporary local test only:
    # Gmail Watch/Pub/Sub is configured after OAuth and initial sync are confirmed.
    # renew_watch(db, account, service, settings.google_pubsub_topic)

    return {
        "status": "connected",
        "google_email": email,
        "initial_sync": "completed",
        "gmail_watch": "not configured yet",
    }