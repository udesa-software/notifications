import logging
import os
import httpx
import base64
import json
from typing import Optional
from fastapi import FastAPI, Depends, HTTPException, Header
from sqlalchemy.orm import Session
import firebase_admin
from firebase_admin import credentials, messaging, exceptions

from .config import settings
from .database import get_db, init_db, UserToken
from .schemas import TokenRegistration, NotificationRequest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Udesamigos Notifications Service")

@app.on_event("startup")
def on_startup():
    init_db()

# Safe initialization of Firebase Admin SDK
firebase_app = None
if settings.FIREBASE_SERVICE_ACCOUNT_PATH and os.path.exists(settings.FIREBASE_SERVICE_ACCOUNT_PATH):
    try:
        cred = credentials.Certificate(settings.FIREBASE_SERVICE_ACCOUNT_PATH)
        firebase_app = firebase_admin.initialize_app(cred)
        logger.info("Firebase initialized successfully")
    except Exception as e:
        logger.error(f"Error initializing Firebase: {e}")
else:
    logger.warning("Firebase service account path not found. Direct FCM fallback disabled (will mock direct FCM).")

# Middleware to validate the internal secret for inter-microservice communication
def verify_internal_secret(x_internal_secret: str = Header(None)):
    if not x_internal_secret or x_internal_secret != settings.INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Invalid internal secret")

# --- Auxiliary Sending Methods ---

async def send_via_expo(token: str, title: str, body: str, data: dict, db_token: UserToken, db: Session):
    """Sends a notification using the Expo Push Notification service (used for Expo Go)."""
    url = "https://exp.host/--/api/v2/push/send"
    payload = {
        "to": token,
        "sound": "default",
        "title": title,
        "body": body,
        "data": data or {}
    }
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=payload, timeout=10.0)
            res_data = response.json()
            
            # Check for HTTP status code errors
            if response.status_code != 200:
                logger.error(f"Expo API error status {response.status_code}: {response.text}")
                return {"status": "error", "message": f"Expo API status {response.status_code}"}

            # CA.4: Check for unregistered or invalid device token errors in response payload
            errors = res_data.get("data", [])
            for err in errors:
                if err.get("status") == "error":
                    details = err.get("details", {})
                    if details.get("error") in ["DeviceNotRegistered", "InvalidCredentials"]:
                        logger.warning(f"Expo token for user {db_token.user_id} is invalid ({details.get('error')}). Deleting from DB.")
                        db.delete(db_token)
                        db.commit()
                        return {"status": "error", "message": "Token invalid and deleted"}
            
            logger.info(f"Notification sent successfully via Expo to user {db_token.user_id}")
            return {"status": "sent", "provider": "expo"}
            
        except Exception as e:
            logger.error(f"HTTP error calling Expo Push API: {e}")
            return {"status": "error", "message": str(e)}

def send_via_fcm(token: str, title: str, body: str, data: dict, db_token: UserToken, db: Session):
    """Sends a notification using direct Firebase Cloud Messaging (FCM)."""
    if not firebase_app:
        logger.info(f"[MOCK PUSH - FCM] To: {db_token.user_id}, Title: {title}, Body: {body}, Data: {data}")
        return {"status": "mock_sent", "message": "Firebase not configured"}

    try:
        # Convert all dictionary values in data to strings as required by FCM
        string_data = {k: str(v) for k, v in (data or {}).items()}
        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data=string_data,
            token=token,
        )
        response = messaging.send(message)
        logger.info(f"Notification sent successfully via FCM: {response}")
        return {"status": "sent", "provider": "fcm", "message_id": response}

    except (messaging.UnregisteredError, exceptions.InvalidArgumentError, ValueError) as e:
        # CA.4: Delete the invalid or unregistered token from the database
        logger.warning(f"FCM token for user {db_token.user_id} is invalid. Deleting from DB. Reason: {e}")
        db.delete(db_token)
        db.commit()
        return {"status": "error", "message": f"Token invalid and deleted: {str(e)}"}
    except Exception as e:
        logger.error(f"Error sending direct FCM: {e}")
        return {"status": "error", "message": str(e)}

def decode_jwt_payload_manually(token: str) -> dict:
    """Decodes a JWT payload manually without signature verification (assumes Gateway checked it)."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("Invalid JWT format")
        
        payload_b64 = parts[1]
        # Pad payload_b64 to make its length a multiple of 4
        payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
        
        payload_bytes = base64.b64decode(payload_b64.replace("-", "+").replace("_", "/"))
        return json.loads(payload_bytes.decode("utf-8"))
    except Exception as e:
        logger.error(f"Error decoding JWT manually: {e}")
        return {}

# --- Endpoints ---

@app.post("/tokens")
async def register_token(
    reg: TokenRegistration, 
    authorization: Optional[str] = Header(None), 
    db: Session = Depends(get_db)
):
    """Registers or updates a push notification token for a user."""
    user_id = reg.user_id
    
    # Infer user_id from Authorization JWT if not supplied in body
    if not user_id and authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ")[1]
        payload = decode_jwt_payload_manually(token)
        user_id = payload.get("sub")
        
    if not user_id:
        raise HTTPException(
            status_code=400, 
            detail="User ID not provided in request body and could not be inferred from Authorization header"
        )
        
    db_token = db.query(UserToken).filter(UserToken.user_id == user_id).first()
    if db_token:
        db_token.fcm_token = reg.fcm_token
    else:
        db_token = UserToken(user_id=user_id, fcm_token=reg.fcm_token)
        db.add(db_token)
    
    db.commit()
    logger.info(f"Token registered/updated for user {user_id}")
    return {"status": "ok", "message": "Token registered successfully"}

@app.delete("/tokens/{user_id}", dependencies=[Depends(verify_internal_secret)])
async def delete_token(user_id: str, db: Session = Depends(get_db)):
    """Deletes a user's push token (triggered during logout, suspension, or deletion)."""
    db_token = db.query(UserToken).filter(UserToken.user_id == user_id).first()
    if db_token:
        db.delete(db_token)
        db.commit()
        logger.info(f"Token deleted for user {user_id}")
        return {"status": "ok", "message": "Token deleted"}
    return {"status": "ok", "message": "No token found"}

@app.post("/notify", dependencies=[Depends(verify_internal_secret)])
async def send_notification(req: NotificationRequest, db: Session = Depends(get_db)):
    """Sends a push notification to a user using the appropriate delivery channel."""
    db_token = db.query(UserToken).filter(UserToken.user_id == req.user_id).first()
    if not db_token:
        logger.warning(f"No push token registered for user {req.user_id}. Skipping notification.")
        return {"status": "skipped", "message": "No token found for user"}

    token = db_token.fcm_token
    
    # Route according to token format
    if token.startswith("ExponentPushToken"):
        return await send_via_expo(token, req.title, req.body, req.data, db_token, db)
    else:
        return send_via_fcm(token, req.title, req.body, req.data, db_token, db)

@app.get("/health")
async def health_check():
    return {"status": "healthy"}
