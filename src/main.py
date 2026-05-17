import logging
import os
from fastapi import FastAPI, Depends, HTTPException, Header
from sqlalchemy.orm import Session
import firebase_admin
from firebase_admin import credentials, messaging, exceptions

from .config import settings
from .database import get_db, init_db, UserToken
from .schemas import TokenRegistration, NotificationRequest

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Udesamigos Notifications Service")

# Initialize DB
@app.on_event("startup")
def on_startup():
    init_db()

# Initialize Firebase
firebase_app = None
if settings.FIREBASE_SERVICE_ACCOUNT_PATH and os.path.exists(settings.FIREBASE_SERVICE_ACCOUNT_PATH):
    try:
        cred = credentials.Certificate(settings.FIREBASE_SERVICE_ACCOUNT_PATH)
        firebase_app = firebase_admin.initialize_app(cred)
        logger.info("Firebase initialized successfully")
    except Exception as e:
        logger.error(f"Error initializing Firebase: {e}")
else:
    logger.warning("Firebase service account path not found or not provided. Notifications will be logged but not sent.")

# --- Middleware-like check for internal secret ---
def verify_internal_secret(x_internal_secret: str = Header(None)):
    if not x_internal_secret or x_internal_secret != settings.INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Invalid internal secret")

# --- Endpoints ---

@app.post("/tokens")
async def register_token(reg: TokenRegistration, db: Session = Depends(get_db)):
    """Registers or updates an FCM token for a user."""
    db_token = db.query(UserToken).filter(UserToken.user_id == reg.user_id).first()
    if db_token:
        db_token.fcm_token = reg.fcm_token
    else:
        db_token = UserToken(user_id=reg.user_id, fcm_token=reg.fcm_token)
        db.add(db_token)
    
    db.commit()
    logger.info(f"Token updated for user {reg.user_id}")
    return {"status": "ok", "message": "Token registered"}

@app.delete("/tokens/{user_id}", dependencies=[Depends(verify_internal_secret)])
async def delete_token(user_id: str, db: Session = Depends(get_db)):
    """Deletes the FCM token for a user."""
    db_token = db.query(UserToken).filter(UserToken.user_id == user_id).first()
    if db_token:
        db.delete(db_token)
        db.commit()
        logger.info(f"Token deleted for user {user_id}")
        return {"status": "ok", "message": "Token deleted"}
    
    return {"status": "ok", "message": "No token found"}

@app.post("/notify", dependencies=[Depends(verify_internal_secret)])
async def send_notification(req: NotificationRequest, db: Session = Depends(get_db)):
    """Sends a push notification to a user."""
    db_token = db.query(UserToken).filter(UserToken.user_id == req.user_id).first()
    if not db_token:
        logger.warning(f"No token found for user {req.user_id}. Skipping notification.")
        return {"status": "skipped", "message": "No token found for user"}

    if not firebase_app:
        logger.info(f"[MOCK PUSH] To: {req.user_id}, Title: {req.title}, Body: {req.body}, Data: {req.data}")
        return {"status": "mock_sent", "message": "Firebase not configured, logged instead"}

    try:
        message = messaging.Message(
            notification=messaging.Notification(
                title=req.title,
                body=req.body,
            ),
            data=req.data or {},
            token=db_token.fcm_token,
        )
        response = messaging.send(message)
        logger.info(f"Notification sent successfully: {response}")
        return {"status": "sent", "message_id": response}

    except (messaging.UnregisteredError, exceptions.InvalidArgumentError, ValueError) as e:
        # CA.4: Delete invalid or malformed token
        logger.warning(f"FCM token for user {req.user_id} is invalid or malformed. Deleting from DB. Reason: {e}")
        db.delete(db_token)
        db.commit()
        return {"status": "error", "message": f"Token invalid and deleted: {str(e)}"}
    
    except Exception as e:
        logger.error(f"Error sending notification: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health_check():
    return {"status": "healthy"}
